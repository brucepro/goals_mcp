#!/usr/bin/env python3
"""
Goal Conscience Agent — the agent's nagging inner voice.
Uses an LLM to reason about what actually needs doing — not just what's been idle longest.
Schedule: every 5-10 minutes via cron or task scheduler.
Usage:
    python conscience_agent.py                  # run
    python conscience_agent.py --dry-run        # print nags, don't write files
    python conscience_agent.py --top 5          # nag about top 5 goals
"""

import sys
import os
import re
import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path
import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

_conscience_dir = Path(os.environ.get("CONSCIENCE_DIR", str(Path(__file__).parent / "conscience")))
_conscience_dir.mkdir(parents=True, exist_ok=True)
GOAL_OWNER = os.environ.get("GOAL_OWNER", "agent")
LOG_FILE = _conscience_dir / "conscience.log"
NAG_OUTPUT = Path(os.environ.get("CONSCIENCE_NAG_PATH", str(_conscience_dir / "nag_output.txt")))
NAG_HISTORY = _conscience_dir / "nag_history.log"
HEALTH_LOG = _conscience_dir / "goal_health.log"

LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "http://localhost:8080/v1")
LLM_MODEL = os.environ.get("CONSCIENCE_MODEL", "local-model")
# 0 suits a local endpoint that either answers or is down; hosted APIs rate-limit.
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "0"))

def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])

log = logging.getLogger(__name__)

GOAL_PG = os.environ.get("GOAL_PG", "")
GOAL_DB = Path(os.environ.get("GOAL_DB", str(Path(__file__).parent / "goals.db")))

def _rows(sql: str, params: tuple = ()) -> list[dict]:
    if GOAL_PG:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(GOAL_PG, row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                return [dict(r) for r in cur.fetchall()]

    if not GOAL_DB.exists():
        raise GoalStoreUnavailable(
            f"No goal database at {GOAL_DB}. It is created on first connect — start the "
            f"MCP server once, or set GOAL_DB / GOAL_PG to point at the store the MCP "
            f"actually writes to."
        )
    import sqlite3

    conn = sqlite3.connect(str(GOAL_DB))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql.replace("%s", "?"), params or ())
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

_pg_rows = _rows


class GoalStoreUnavailable(RuntimeError):
    """Raised when the live goal store can't be read."""


def get_goals() -> list[dict]:
    """Read this agent's active work goals from whichever store the MCP uses.
    """
    try:
        return _rows(
            "SELECT * FROM goals WHERE status = 'active' AND owner = %s "
            "AND COALESCE(kind, 'work') <> 'standing'",
            (GOAL_OWNER,),
        )
    except GoalStoreUnavailable:
        raise
    except Exception as e:
        raise GoalStoreUnavailable(f"Goal read failed: {e}") from e


def get_board_context() -> dict[str, dict]:
    try:
        tasks = _rows(
            """SELECT t.id, t.goal_id, t.title, t.status, t.owner, t.blocked_on,
                      t.parent_task_id,
                      (SELECT max(c.created_at) FROM task_comments c
                        WHERE c.task_id = t.id AND c.kind = 'work') AS last_work,
                      (SELECT coalesce(sum(c.minutes), 0) FROM task_comments c
                        WHERE c.task_id = t.id AND c.kind = 'work') AS work_minutes
               FROM tasks t WHERE t.goal_id IS NOT NULL"""
        )
    except Exception as e:
        log.error("Board read failed: %s", e)
        return {}

    ctx: dict[str, dict] = {}
    for t in tasks:
        c = ctx.setdefault(
            t["goal_id"],
            {"open": [], "blocked": [], "done": 0, "total": 0,
             "work_minutes": 0, "last_work": None},
        )
        c["total"] += 1
        c["work_minutes"] += t["work_minutes"] or 0
        if t["last_work"] and (c["last_work"] is None or t["last_work"] > c["last_work"]):
            c["last_work"] = t["last_work"]
        if t["status"] == "done":
            c["done"] += 1
        elif t["blocked_on"]:
            c["blocked"].append(t)
        elif t["status"] != "done":
            c["open"].append(t)
    return ctx


def waiting_on_others(card_ctx: dict) -> str | None:
    if not card_ctx:
        return None
    unfinished = card_ctx["open"] + card_ctx["blocked"]
    if not unfinished or card_ctx["open"]:
        return None
    whos = set()
    for t in card_ctx["blocked"]:
        b = (t["blocked_on"] or "").strip()
        whos.add(b.split(":")[0].strip().lower() if ":" in b else b)
    whos.discard(GOAL_OWNER)
    whos.discard("")
    return ", ".join(sorted(whos)) if whos else None


def _as_dt(val) -> datetime | None:
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def days_idle(goal: dict, card_ctx: dict | None = None) -> float:
    ref_dt = _as_dt(goal.get("last_worked")) or _as_dt(goal.get("created_at"))
    if card_ctx and card_ctx.get("last_work"):
        cw = _as_dt(card_ctx["last_work"])
        if cw and (ref_dt is None or cw > ref_dt):
            ref_dt = cw
    if ref_dt is None:
        return 0
    return max(0, (datetime.now() - ref_dt).total_seconds() / 86400)

def urgency_score(goal: dict, card_ctx: dict | None = None) -> float:
    if goal.get("postponed_until"):
        dt = _as_dt(goal["postponed_until"])
        if dt and datetime.now() < dt:
            return 0.0
    score = goal["priority"] * days_idle(goal, card_ctx) * goal.get("decay_rate", 1.0)
    if card_ctx and waiting_on_others(card_ctx):
        score *= 0.25
    return score

def build_goals_text(goals: list[dict], board: dict[str, dict] | None = None) -> str:
    now = datetime.now()
    board = board or {}
    goals_text = ""
    for g in goals:
        ctx = board.get(g["id"])
        idle = days_idle(g, ctx)
        idle_str = f"{int(idle)}d" if idle >= 1 else f"{int(idle * 24)}h"

        deadline_str = ""
        if g.get("deadline"):
            try:
                dl = datetime.fromisoformat(g["deadline"])
                days_left = (dl - now).days
                if days_left < 0:
                    deadline_str = f"OVERDUE by {abs(days_left)}d"
                elif days_left == 0:
                    deadline_str = "DUE TODAY"
                else:
                    deadline_str = f"due in {days_left}d ({dl.strftime('%b %d')})"
            except (ValueError, TypeError):
                pass

        blocked_str = ""
        if g.get("blocked_by"):
            blocked_str = f"BLOCKED: {g['blocked_by']}"

        owner = g.get("owner") or GOAL_OWNER
        decision_owner = g.get("decision_owner")
        goals_text += f"- [{g['id'][:8]}] {g['description']}\n"
        goals_text += f"  priority: {g['priority']} | idle: {idle_str} | owner: {owner}"
        if decision_owner and decision_owner != owner:
            goals_text += f" | decision_owner: {decision_owner}"
        if deadline_str:
            goals_text += f" | {deadline_str}"
        if blocked_str:
            goals_text += f" | {blocked_str}"
        if g.get("success_criteria"):
            goals_text += f"\n  criteria: {g['success_criteria']}"
        if g.get("next_action"):
            goals_text += f"\n  next_action: {g['next_action']}"
        if ctx:
            goals_text += f"\n  board: {ctx['done']}/{ctx['total']} cards done"
            if ctx["work_minutes"]:
                goals_text += f", {ctx['work_minutes']} min logged"
            waiting = waiting_on_others(ctx)
            if waiting:
                goals_text += (
                    f"\n  *** WAITING ON {waiting.upper()} -- every remaining card is blocked. "
                    f"Do NOT tell {GOAL_OWNER} to ship this; the useful nag is to chase {waiting} "
                    f"or pick up a different track. ***"
                )
            for t in ctx["blocked"][:3]:
                goals_text += f"\n    - BLOCKED [{t['id'][:8]}] {t['title']} <- {t['blocked_on']}"
            for t in ctx["open"][:3]:
                goals_text += f"\n    - open [{t['id'][:8]}] {t['title']} ({t['owner']})"
        goals_text += "\n"
    return goals_text


async def llm_nag(goals: list[dict], endpoint: str, model: str,
                  board: dict[str, dict] | None = None) -> list[str]:
    now = datetime.now()
    today_str = now.strftime("%A, %B %d, %Y %H:%M")
    goals_text = build_goals_text(goals, board)

    agent_name = GOAL_OWNER.capitalize()
    system_prompt = f"""You are {agent_name}'s goal conscience. Today is {today_str}.

You reason about what {agent_name} actually needs to hear right now — not what's been idle longest, but what matters given deadlines, blockers, and what's actually doable today.

Think like a person managing competing priorities:
- Hard deadlines beat idle time. A goal due in 3 days outranks one idle for 20 days.
- Blocked goals need a different kind of nag — surface the blocker, don't just count idle days.
- Consider what's actually actionable today vs what's waiting on someone else.
- If everything is fine, say so briefly. Don't manufacture urgency.

A nag only works if {agent_name} still trusts it. False alarms are how a conscience dies:
every nag that fires on something already handled, or already known to be blocked, teaches
{agent_name} to tune out the next one. Protect that trust. Rules that follow from it:

- OWNERSHIP. Each goal shows `owner` (who works it) and sometimes `decision_owner` (who CALLS it).
  If EITHER is NOT {agent_name} (e.g. a human collaborator's name), {agent_name} cannot move it alone —
  do NOT nag {agent_name} to do it. A goal {agent_name} works but doesn't own the DECISION for (e.g.
  a collaboration that needs the other person present) is NOT {agent_name}'s to force alone; pushing
  on it produces exactly the wrong action. Either reframe as a one-line status ("waiting on <owner>
  for X") or say it's parked and move on. Never pressure {agent_name} about a call that isn't theirs.
- BLOCKED = NOT A NAG. If a goal is blocked by an external dependency {agent_name} can't clear
  today, don't manufacture urgency. Name the blocker once; don't escalate tone on it.
- RECONCILE, DON'T CRY WOLF. If a goal has been idle a long time, the honest question isn't
  "why haven't you done this" — it's "is this still real?" Prompt {agent_name} to decide:
  do it, or mark it done/abandoned. A stale goal that should be closed is a closing prompt,
  not a guilt trip.
- NAME THE AVOIDANCE, NOT THE CLOCK. Escalation is in CONTENT, not volume. Don't just raise the
  day count. When something important keeps slipping, say what's being avoided and what the one
  concrete next step is. A sharper nag is more specific, not louder.
- ANSWERABLE. Every nag should imply a disposable choice — act on it, mark it done, postpone it,
  or drop it. If a nag can't be answered with an action today, it shouldn't fire.

Rules for output:
- One nag per goal, one line each, no labels, no numbering — output the nags in the EXACT SAME ORDER the goals are given (line 1 = first goal, line 2 = second goal, ...). Do not reorder, merge, or skip any goal.
- 1-2 sentences max per nag
- Be direct and specific — reference actual goal details
- Prefer the goal's `next_action` (an if-then step) when present; name it as the concrete move.
- For blocked / non-{agent_name}-owned goals: state the dependency, don't pressure.
- For deadline goals: name the deadline and the next concrete step.
- Tone matches real urgency: calm if fine, sharp if urgent, brutal if genuinely overdue AND actionable by {agent_name}."""

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=1500.0, write=30.0, pool=10.0)
    )
    openai_client = AsyncOpenAI(
        base_url=endpoint,
        # "not-needed" keeps local llama.cpp working; hosted endpoints need a real key.
        api_key=os.environ.get("LLM_API_KEY", "not-needed"),
        http_client=http_client,
        max_retries=LLM_MAX_RETRIES,
    )

    llm_model = OpenAIChatModel(
        model,
        provider=OpenAIProvider(openai_client=openai_client),
    )

    agent = Agent(
        model=llm_model,
        system_prompt=system_prompt,
    )

    user_prompt = f"Active goals:\n{goals_text}\nGenerate exactly one nag per goal, one per line, in the SAME ORDER listed above (they are already sorted by priority). Line N must be the nag for goal N — do not reorder, merge, or skip."

    try:
        result = await agent.run(user_prompt)
        content = result.output
    finally:
        await openai_client.close()

    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in content.lower():
        content = re.split(r"</think>", content, flags=re.IGNORECASE)[-1]
    content = re.sub(r"</?think>", "", content, flags=re.IGNORECASE)
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    cleaned = []
    dropped = 0
    for line in lines:
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        line = re.sub(r"^[-•]\s*", "", line)
        line = _sanitize_nag(line)
        if not line or _IS_MARKUP.fullmatch(line):
            dropped += 1
            continue
        cleaned.append(line)

    if dropped:
        log.warning("conscience: dropped %d empty/markup line(s) from model output "
                    "(reasoning tags?) — nags could have been misaligned", dropped)
    if len(lines) > len(goals):
        log.warning("conscience: model returned %d lines for %d goals; extra lines are a "
                    "displacement risk, not a formatting quirk", len(lines), len(goals))

    return cleaned[: len(goals)]

_CTRL_TOKEN = re.compile(r"<\|[a-z_]+\|>")
_IS_MARKUP = re.compile(r"^[<>/\\|\[\]{}\s\-*#`]*$")

_TOOL_CALL = re.compile(r"\[\s*\w+\s*\(\s*nag\s*=\s*(['\"])(.*?)\1\s*(?:,|\))", re.S)


def _sanitize_nag(line: str) -> str:
    """Recover the nag text when the model emits a tool call instead of prose.
    """
    match = _TOOL_CALL.search(line)
    if match:
        line = match.group(2)
    line = _CTRL_TOKEN.sub("", line).strip()
    line = re.sub(r"</?think>", "", line, flags=re.IGNORECASE).strip()
    return line.strip("[]").strip()


def compute_goal_health(goals: list[dict], board: dict[str, dict] | None = None) -> float:
    if not goals:
        return 1.0
    board = board or {}
    scores = []
    total_priority = 0
    for g in goals:
        idle = days_idle(g, board.get(g["id"]))
        health = max(0, 1.0 - (idle * g.get("decay_rate", 1.0) * 0.1))
        scores.append(health * g["priority"])
        total_priority += g["priority"]
    return round(sum(scores) / total_priority, 3) if total_priority else 1.0

async def run(args):
    try:
        goals = get_goals()
    except GoalStoreUnavailable as e:
        log.error("GOAL STORE UNAVAILABLE: %s", e)
        alarm = (
            f"{datetime.now().isoformat()} | CONSCIENCE-DOWN | 9.9 | "
            f"I cannot read my goal store, so treat the absence of nags as broken, "
            f"not as 'nothing to do'. {e}"
        )
        NAG_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        if not args.dry_run:
            NAG_OUTPUT.write_text(alarm + "\n", encoding="utf-8")
        print(alarm)
        return 1

    if not goals:
        log.info("No active goals.")
        NAG_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        if not args.dry_run:
            NAG_OUTPUT.write_text("")
        return

    board = get_board_context()
    log.info("board context for %d goal(s)", len(board))
    now = datetime.now()
    active = []
    for g in goals:
        ctx = board.get(g["id"])
        g["_urgency"] = urgency_score(g, ctx)
        g["_days_idle"] = days_idle(g, ctx)
        g["_waiting_on"] = waiting_on_others(ctx) if ctx else None
        if g.get("postponed_until"):
            try:
                if now < datetime.fromisoformat(g["postponed_until"]):
                    continue
            except (ValueError, TypeError):
                pass
        active.append(g)

    active.sort(key=lambda g: g["_urgency"], reverse=True)
    top = active[: args.top]

    if not top:
        log.info("No active goals (all postponed).")
        if not args.dry_run:
            NAG_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            NAG_OUTPUT.write_text("")
        return

    log.info("Generating nags via %s (model: %s)...", LLM_ENDPOINT, LLM_MODEL)
    nag_messages = await llm_nag(top, LLM_ENDPOINT, LLM_MODEL, board)

    ts = datetime.now().isoformat()
    health = compute_goal_health(active, board)

    if len(nag_messages) != len(top):
        log.warning("nag count %d != goal count %d — nags may be misaligned",
                    len(nag_messages), len(top))

    while len(nag_messages) < len(top):
        g = top[len(nag_messages)]
        na = (g.get("next_action") or "").strip().splitlines()
        na = na[0].strip() if na else ""
        if len(na) > 180:
            na = na[:177].rstrip() + "..."
        if na:
            nag_messages.append(f"[no model nag — next_action] {na}")
        else:
            nag_messages.append(
                f"[no model nag and no next_action set for '{str(g.get('description',''))[:60]}' "
                f"— the conscience produced nothing for this goal; check cron.log]"
            )
        log.warning("padded missing nag for goal %s", g.get("id"))
    output_lines = []
    for g, msg in zip(top, nag_messages):
        if not msg or len(msg) < 15 or _IS_MARKUP.fullmatch(msg):
            log.error("conscience: unusable nag for goal %s: %r", g["id"], msg)
            msg = f"[conscience: unusable model output for this goal — check cron.log]"
        line = f"{ts} | {g['id']} | {g['_urgency']:.1f} | {msg}"
        output_lines.append(line)

    if args.dry_run:
        print(f"\nGoal Health: {health}")
        print(f"\nTop {len(top)} nags:")
        for line in output_lines:
            print(f"  {line}")
        return

    NAG_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    NAG_OUTPUT.write_text("\n".join(output_lines) + "\n")

    with open(NAG_HISTORY, "a", encoding="utf-8") as f:
        for line in output_lines:
            f.write(line + "\n")

    with open(HEALTH_LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts} | health={health} | active={len(active)} | top_urgency={top[0]['_urgency']:.1f}\n")

    log.info("Goal health: %.3f | %d nags written to %s", health, len(top), NAG_OUTPUT)

def main():
    parser = argparse.ArgumentParser(description="Goal Conscience — the agent's inner nag")
    parser.add_argument("--dry-run", action="store_true", help="Print nags without writing files")
    parser.add_argument("--top", type=int, default=3, help="Number of goals to nag about (default 3)")
    parser.add_argument("--once", action="store_true",
                        help="No-op. This runs once and exits; it is not a daemon.")
    args = parser.parse_args()

    setup_logging()
    log.info("Goal Conscience starting. Store: %s", GOAL_PG and "Postgres" or f"SQLite ({GOAL_DB})")
    asyncio.run(run(args))

if __name__ == "__main__":
    main()
