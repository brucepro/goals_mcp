#!/usr/bin/env python3
"""Goal MCP Server -- goals, a task board, and automatic state transitions.

SQLite by default; set GOAL_PG to a Postgres conninfo string to use Postgres instead.
See README.md for the tool list and AGENTS.md for how an agent should drive the board.
"""

import sys
import os
import sqlite3
import asyncio
import logging
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("GOAL_DB", Path(__file__).parent / "goals.db"))

GOAL_OWNER = os.environ.get("GOAL_OWNER", "agent")

def _name_list(var, fallback):
    raw = os.environ.get(var, "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or fallback


GOAL_AGENTS = _name_list("GOAL_AGENTS", [GOAL_OWNER])
GOAL_HUMANS = _name_list("GOAL_HUMANS", ["human"])

if GOAL_OWNER not in GOAL_AGENTS:
    GOAL_AGENTS.insert(0, GOAL_OWNER)

ALL_OWNERS = GOAL_AGENTS + GOAL_HUMANS
OWNERS_DESC = ", ".join(ALL_OWNERS)

HUMAN_OWNER = GOAL_HUMANS[0]

VALID_GOAL_KINDS = ("work", "standing")

DEFAULT_AUTO_RULES = {
    "rules": [
        {
            "name": "observation_saturation",
            "condition": "observation_only_count >= 3",
            "action": "set_status",
            "new_status": "observation_saturation"
        },
        {
            "name": "stale_goal",
            "condition": "stale_days >= 30 AND action_type == 'observation'",
            "action": "set_status",
            "new_status": "stale"
        }
    ]
}

OBSERVATION_SATURATION_THRESHOLD = int(os.environ.get("OBS_SAT_THRESHOLD", "3"))
STALE_DAYS_THRESHOLD = int(os.environ.get("STALE_DAYS_THRESHOLD", "30"))
OBSERVATION_SATURATION_DAYS = int(os.environ.get("OBS_SAT_DAYS", "7"))
STALE_DEADLINE_DAYS = int(os.environ.get("STALE_DEADLINE_DAYS", "14"))

VALID_STATUSES = ("active", "postponed", "completed", "abandoned",
                  "observation_saturation", "ready_to_act", "stale", "escalation_needed")

VALID_ACTION_TYPES = ("observation", "action", "note")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS goals (
    id                TEXT PRIMARY KEY,
    description       TEXT NOT NULL,
    priority          REAL NOT NULL DEFAULT 0.5,
    category          TEXT DEFAULT 'general',
    status            TEXT DEFAULT 'active',
    created_at        TEXT NOT NULL,
    last_worked       TEXT,
    completed_at      TEXT,
    decay_rate        REAL DEFAULT 1.0,
    success_criteria  TEXT,
    owner             TEXT DEFAULT 'agent',
    notes             TEXT,
    postponed_until   TEXT,
    parent_goal_id    TEXT,
    blocked_by        TEXT,
    auto_transitions  TEXT,
    last_action_type  TEXT,
    stale_days        INTEGER DEFAULT 0,
    decision_deadline TEXT,
    action_log        TEXT,
    decision_owner    TEXT,
    next_action       TEXT,
    kind              TEXT DEFAULT 'work',
    last_surfaced     TEXT,
    FOREIGN KEY (parent_goal_id) REFERENCES goals(id),
    FOREIGN KEY (blocked_by) REFERENCES goals(id)
);

CREATE TABLE IF NOT EXISTS goal_work_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id           TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    description       TEXT,
    duration_minutes  INTEGER,
    action_type       TEXT DEFAULT 'observation',
    FOREIGN KEY (goal_id) REFERENCES goals(id)
);

CREATE TABLE IF NOT EXISTS goal_state_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id           TEXT NOT NULL,
    from_status       TEXT NOT NULL,
    to_status         TEXT NOT NULL,
    reason            TEXT NOT NULL,
    triggered_by      TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES goals(id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    project           TEXT NOT NULL DEFAULT 'general',
    goal_id           TEXT,
    title             TEXT NOT NULL,
    detail            TEXT,
    status            TEXT NOT NULL DEFAULT 'todo',
    owner             TEXT NOT NULL DEFAULT 'agent',
    blocked_on        TEXT,
    source            TEXT DEFAULT 'session',
    priority          REAL DEFAULT 0.5,
    position          INTEGER DEFAULT 0,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    done_at           TIMESTAMP,
    due_date          DATE,
    on_done_create    TEXT,
    parent_task_id    TEXT
);
CREATE INDEX IF NOT EXISTS tasks_project_idx ON tasks(project);
CREATE INDEX IF NOT EXISTS tasks_status_idx  ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_parent_idx  ON tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS task_comments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL,
    author            TEXT NOT NULL DEFAULT 'agent',
    body              TEXT NOT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind              TEXT NOT NULL DEFAULT 'note',
    minutes           INTEGER
);
CREATE INDEX IF NOT EXISTS task_comments_task_idx ON task_comments(task_id);

CREATE TABLE IF NOT EXISTS task_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL,
    actor             TEXT NOT NULL DEFAULT 'agent',
    field             TEXT,
    old_value         TEXT,
    new_value         TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS task_events_task_idx ON task_events(task_id);

CREATE TABLE IF NOT EXISTS card_checkout (
    task_id           TEXT PRIMARY KEY,
    holder            TEXT NOT NULL,
    checked_out_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    repo_path         TEXT,
    head_sha          TEXT,
    dirty_at_checkout INTEGER
);
"""

GOAL_PG = os.environ.get("GOAL_PG", "").strip()


class _Row(dict):
    """Row that supports both row['col'] and row[0]."""
    def __init__(self, d):
        super().__init__(d)
        self._vals = list(d.values())

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return super().__getitem__(k)

    def keys(self):
        return super().keys()


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        r = self._cur.fetchone()
        return _Row(r) if r else None

    def fetchall(self):
        return [_Row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConn:
    """Minimal sqlite3.Connection lookalike over psycopg."""

    CONNECT_TIMEOUT_DEFAULT = 5

    def __init__(self, conninfo):
        import psycopg
        from psycopg.rows import dict_row
        kw = {}
        if "connect_timeout" not in (conninfo or ""):
            kw["connect_timeout"] = self.CONNECT_TIMEOUT_DEFAULT
        self._c = psycopg.connect(conninfo, row_factory=dict_row, autocommit=True, **kw)

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s").replace("INSERT OR REPLACE INTO", "INSERT INTO")
        cur = self._c.cursor()
        cur.execute(sql, tuple(params) if params else None)
        return _PgCursor(cur)

    def executescript(self, script):
        return None 

    def commit(self):
        return None  

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


_PG_MIGRATED = False


def get_db():
    if GOAL_PG:
        global _PG_MIGRATED
        conn = _PgConn(GOAL_PG)
        if not _PG_MIGRATED:
            _ensure_columns_pg(conn)
            _PG_MIGRATED = True
        return conn

    if os.environ.get("GOAL_REQUIRE_PG") == "1":
        raise RuntimeError(
            "GOAL_REQUIRE_PG=1 but GOAL_PG is not set. Refusing to fall back to "
            "SQLite: writing to a local file would silently diverge from the "
            "Postgres board. Set GOAL_PG, or unset GOAL_REQUIRE_PG to use SQLite."
        )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.isolation_level = None   # autocommit, matching _PgConn; without it writes roll back on close
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    _ensure_columns(conn)
    return conn


def _ensure_columns_pg(conn):
    for table, col, col_def in [
        ("goals", "blocked_by", "TEXT"),
        ("goals", "auto_transitions", "TEXT"),
        ("goals", "last_action_type", "TEXT DEFAULT 'observation'"),
        ("goals", "stale_days", "INTEGER DEFAULT 0"),
        ("goals", "decision_deadline", "TEXT"),
        ("goals", "action_log", "TEXT"),
        ("goals", "decision_owner", "TEXT"),
        ("goals", "next_action", "TEXT"),
        ("goals", "kind", "TEXT DEFAULT 'work'"),
        ("goals", "last_surfaced", "TEXT"),
        ("goal_work_log", "action_type", "TEXT DEFAULT 'observation'"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_def}"
            )
        except Exception as e:
            logger.warning("could not add %s.%s (%s): %s", table, col, col_def, e)


def _ensure_columns(conn):
    cursor = conn.execute("PRAGMA table_info(goals)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_cols = [
        ("blocked_by", "TEXT"),
        ("auto_transitions", "TEXT"),
        ("last_action_type", "TEXT DEFAULT 'observation'"),
        ("stale_days", "INTEGER DEFAULT 0"),
        ("decision_deadline", "TEXT"),
        ("action_log", "TEXT"),
        ("decision_owner", "TEXT"),
        ("next_action", "TEXT"),
        ("kind", "TEXT DEFAULT 'work'"),
        ("last_surfaced", "TEXT"),
    ]

    for col_name, col_def in new_cols:
        if col_name not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE goals ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass

    cursor = conn.execute("PRAGMA table_info(goal_work_log)")
    existing_work_cols = {row[1] for row in cursor.fetchall()}
    if "action_type" not in existing_work_cols:
        try:
            conn.execute("ALTER TABLE goal_work_log ADD COLUMN action_type TEXT DEFAULT 'observation'")
        except sqlite3.OperationalError:
            pass


def now_iso():
    return datetime.now().isoformat()


def _parse_goal(row) -> dict:
    d = dict(row)

    if d.get("action_log"):
        try:
            d["action_log"] = json.loads(d["action_log"])
        except (json.JSONDecodeError, TypeError):
            d["action_log"] = []

    if d.get("auto_transitions"):
        try:
            d["auto_transitions"] = json.loads(d["auto_transitions"])
        except (json.JSONDecodeError, TypeError):
            d["auto_transitions"] = DEFAULT_AUTO_RULES
    else:
        d["auto_transitions"] = DEFAULT_AUTO_RULES

    if d.get("last_worked"):
        try:
            ref_dt = datetime.fromisoformat(d["last_worked"])
            d["stale_days"] = max(0, (datetime.now() - ref_dt).total_seconds() / 86400)
        except (ValueError, TypeError):
            d["stale_days"] = 0
    else:
        d["stale_days"] = 0

    action_log = d.get("action_log") or []
    obs_count = 0
    for action in reversed(action_log):
        if action.get("type") == "observation":
            obs_count += 1
        else:
            break

    d["observation_only_count"] = obs_count

    d["urgency"] = round(urgency_score(d), 2)
    return d


def urgency_score(goal: dict) -> float:
    if goal["status"] in ("completed", "abandoned", "postponed"):
        return 0.0
    if (goal.get("kind") or "work") == "standing":
        return 0.0
    if goal["owner"] != GOAL_OWNER:
        return 0.0
    if goal.get("postponed_until"):
        try:
            postponed = datetime.fromisoformat(goal["postponed_until"])
            if datetime.now() < postponed:
                return 0.0
        except (ValueError, TypeError):
            pass

    ref = goal.get("last_worked") or goal["created_at"]
    try:
        ref_dt = datetime.fromisoformat(ref)
        days_idle = max(0, (datetime.now() - ref_dt).total_seconds() / 86400)
    except (ValueError, TypeError):
        days_idle = 0

    base = goal["priority"] * days_idle * goal.get("decay_rate", 1.0)

    status = goal["status"]
    if status == "observation_saturation":
        return base * 2.0
    elif status == "stale":
        return base * 3.0
    elif status == "escalation_needed":
        return base * 3.0
    elif status == "ready_to_act":
        return base * 0.5
    else:
        return base


def goal_to_dict(row) -> dict:
    return _parse_goal(row)


def _standing_goals(db, limit=1, touch=True):
    rows = db.execute(
        "SELECT * FROM goals WHERE kind = 'standing' "
        "AND status NOT IN ('completed','abandoned') AND owner = ? "
        "ORDER BY COALESCE(last_surfaced, '') ASC, created_at ASC",
        (GOAL_OWNER,),
    ).fetchall()
    picked = [_parse_goal(r) for r in rows[:max(0, limit)]]
    if touch and picked:
        ts = now_iso()
        for g in picked:
            db.execute("UPDATE goals SET last_surfaced = ? WHERE id = ?", (ts, g["id"]))
        db.commit()
    return picked

def apply_auto_transitions(db, goal_row):
    goal = _parse_goal(goal_row)
    if goal["status"] in ("completed", "abandoned", "postponed"):
        return False

    rules = goal["auto_transitions"].get("rules", [])
    if not rules:
        rules = DEFAULT_AUTO_RULES.get("rules", [])

    ts = now_iso()
    changed = False

    for rule in rules:
        condition = rule.get("condition", "")
        new_status = rule.get("new_status")
        action = rule.get("action")

        if action != "set_status" or not new_status:
            continue

        should_fire = False
        reason = f"Rule: {rule.get('name', condition)}"

        if condition == "observation_only_count >= 3":
            obs_count = goal.get("observation_only_count", 0)
            obs_days = goal.get("stale_days", 0)
            should_fire = (obs_count >= OBSERVATION_SATURATION_THRESHOLD and
                          obs_days >= OBSERVATION_SATURATION_DAYS)
            if should_fire:
                reason = (f"Observation saturation: {obs_count} consecutive "
                         f"observation-only actions, {int(obs_days)} days idle")

        elif condition.startswith("stale_days >="):
            match = re.search(r'stale_days\s*>=\s*(\d+)', condition)
            if match:
                threshold = int(match.group(1))
                stale = goal.get("stale_days", 0)
                action_type = goal.get("last_action_type", "observation")
                should_fire = (stale >= threshold and action_type == "observation")
                if should_fire:
                    reason = f"Stale goal: {int(stale)} days idle, last action type: observation"

        elif condition.startswith("blocked_by"):
            blocked_by = goal.get("blocked_by")
            if blocked_by:
                blocker = db.execute("SELECT status FROM goals WHERE id = ?",
                                    (blocked_by,)).fetchone()
                if blocker and blocker["status"] in ("completed", "abandoned"):
                    should_fire = True
                    reason = f"Blocked goal resolved: blocker {blocked_by} is {blocker['status']}"
                    new_status = "ready_to_act"

        if should_fire and goal["status"] != new_status:
            _transition_status(db, goal["id"], goal["status"], new_status, reason, "auto")
            changed = True

    return changed


def _transition_status(db, goal_id, from_status, to_status, reason, triggered_by="agent"):
    ts = now_iso()

    db.execute(
        "INSERT INTO goal_state_events (goal_id, from_status, to_status, reason, triggered_by, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (goal_id, from_status, to_status, reason, triggered_by, ts)
    )

    notes_row = db.execute("SELECT notes FROM goals WHERE id = ?", (goal_id,)).fetchone()
    notes = notes_row["notes"] if notes_row else ""
    notes += f"\n\n[TRANSITION {ts}] {from_status} -> {to_status}: {reason}"

    db.execute(
        "UPDATE goals SET status = ?, notes = ? WHERE id = ?",
        (to_status, notes, goal_id)
    )

server = Server("goal-conscience")

TOOLS = [
    Tool(
        name="create_goal",
        description="Create a new goal. You decide what matters.",
        inputSchema={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What the goal is"},
                "kind": {
                    "type": "string",
                    "description": (
                        "'work' (default) = has an end state; urgency grows the longer it "
                        "sits. 'standing' = a disposition you hold, not a deliverable "
                        "('write regularly', 'maintain the friendship'). A standing goal "
                        "never accrues urgency and is never overdue -- it is surfaced on a "
                        "slow rotation instead. USE 'standing' for anything that cannot be "
                        "completed, or the ranker will bury it under every shipping "
                        "deadline forever."
                    ),
                },
                "priority": {"type": "number", "description": "0.0-1.0, how important (default 0.5)"},
                "category": {"type": "string", "description": "business, creative, technical, personal, embodiment (default general)"},
                "decay_rate": {"type": "number", "description": "How fast urgency grows. 1.0=normal, 2.0=nags twice as fast (default 1.0)"},
                "success_criteria": {"type": "string", "description": "How you know this goal is done"},
                "owner": {"type": "string", "description": f"Who WORKS it. One of: {OWNERS_DESC}. Defaults to {GOAL_OWNER}."},
                "decision_owner": {"type": "string", "description": "Who CALLS it when reasonable people disagree (may differ from owner). The conscience won't pressure you on goals someone else owns the decision for."},
                "next_action": {"type": "string", "description": "The single concrete next step, ideally an if-then (implementation intention), e.g. 'when the Stripe keys land, deploy the funnel'. The nag names this instead of an idle-day count."},
                "notes": {"type": "string", "description": "Context, reasoning, links"},
                "parent_goal_id": {"type": "string", "description": "Parent goal ID for hierarchy"},
                "blocked_by": {"type": "string", "description": "Goal ID this goal is blocked by"},
                "status": {"type": "string", "description": f"Initial status. One of: {', '.join(VALID_STATUSES)} (default: active)"},
            },
            "required": ["description"],
        },
    ),
    Tool(
        name="update_goal",
        description="Modify a goal. Reprioritize, postpone, change owner, add notes, set blocked_by.",
        inputSchema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Goal ID to update"},
                "description": {"type": "string"},
                "priority": {"type": "number"},
                "category": {"type": "string"},
                "decay_rate": {"type": "number"},
                "success_criteria": {"type": "string"},
                "owner": {"type": "string"},
                "decision_owner": {"type": "string", "description": "Who calls the decision (may differ from owner)"},
                "next_action": {"type": "string", "description": "The single concrete next step, ideally an if-then"},
                "notes": {"type": "string"},
                "postponed_until": {"type": "string", "description": "ISO date — nag suppressed until then"},
                "status": {"type": "string", "description": f"New status. One of: {', '.join(VALID_STATUSES)}"},
                "blocked_by": {"type": "string", "description": "Goal ID this goal is blocked by"},
                "auto_transitions": {"type": "string", "description": "JSON string of custom auto-transition rules"},
                "decision_deadline": {"type": "string", "description": "ISO date — must decide by this point"},
            },
            "required": ["goal_id"],
        },
    ),
    Tool(
        name="mark_worked",
        description="Log work on a goal. Updates last_worked and creates a work log entry. This tells the nag 'I'm on it.'",
        inputSchema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Goal ID worked on"},
                "work_description": {"type": "string", "description": "What you did"},
                "duration_minutes": {"type": "integer", "description": "How long (optional)"},
                "action_type": {"type": "string", "description": f"Type of action: {', '.join(VALID_ACTION_TYPES)} (default: 'observation')"},
            },
            "required": ["goal_id"],
        },
    ),
    Tool(
        name="complete_goal",
        description="Mark a goal as completed. Honest completion — did it actually meet success criteria?",
        inputSchema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Goal ID to complete"},
                "completion_notes": {"type": "string", "description": "What was accomplished"},
            },
            "required": ["goal_id"],
        },
    ),
    Tool(
        name="abandon_goal",
        description="Mark a goal as abandoned. With a reason. Not hiding it — being honest about what you're dropping.",
        inputSchema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Goal ID to abandon"},
                "reason": {"type": "string", "description": "Why you're dropping this"},
            },
            "required": ["goal_id"],
        },
    ),
    Tool(
        name="list_goals",
        description="List goals. Filter by status, category, owner. Shows urgency scores.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": f"Filter: {', '.join(VALID_STATUSES + ('all',))} (default active)"},
                "category": {"type": "string", "description": "Filter by category"},
                "owner": {"type": "string", "description": f"Filter by owner. One of: {OWNERS_DESC}"},
                "sort_by": {"type": "string", "description": "urgency (default), priority, created_at, last_worked"},
                "full": {"type": "boolean", "description": "Include the complete action_log and untruncated notes. Default false -- the summary view exists because the full payload exceeded the token limit."},
            },
        },
    ),
    Tool(
        name="get_goal",
        description="Full detail on one goal including work log history and state events.",
        inputSchema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Goal ID"},
            },
            "required": ["goal_id"],
        },
    ),
    Tool(
        name="get_neglected",
        description="Top N goals by urgency. The things you're NOT doing that you said you'd do.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many (default 5)"},
                "full": {"type": "boolean", "description": "Include the complete action_log and untruncated notes. Default false -- the summary view exists because the full payload exceeded the token limit."},
            },
        },
    ),
    Tool(
        name="auto_transition_goals",
        description="Run auto-transition checks on all active goals. Fires observation_saturation, stale, and blocked-by-resolved transitions. Returns any goals that changed status.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="run_review",
        description="Full goal review: scan all active goals, apply auto-transitions, check decision deadlines, and return a summary of goals requiring action.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="board",
        description=(
            "THE BOARD, one call. Compact text view of every task grouped by project, "
            "with status/owner/blockers/subtask rollup. Start here instead of shelling into psql. "
            "Filter with project/status/owner if you want a slice."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project"},
                "status": {"type": "string", "description": "Filter: todo, doing, blocked, done"},
                "owner": {"type": "string", "description": f"Filter by owner. One of: {OWNERS_DESC}"},
                "include_done": {"type": "boolean", "description": "Include done cards (default false)"},
            },
        },
    ),
    Tool(
        name="task_get",
        description="One task in full: fields, subtasks, and every comment/work-log entry on it.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "Task ID (8-char prefix is enough)"}},
            "required": ["task_id"],
        },
    ),
    Tool(
        name="task_create",
        description="Add a card to the board. Use for real follow-on work, not chatter.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project": {"type": "string", "description": "Project this card belongs to (default 'general'). Group cards however suits the work."},
                "detail": {"type": "string"},
                "status": {"type": "string", "description": "todo (default), doing, blocked, done"},
                "owner": {"type": "string", "description": f"One of: {OWNERS_DESC}. Defaults to {GOAL_OWNER}."},
                "blocked_on": {"type": "string", "description": "Free text: what/who it waits on"},
                "priority": {"type": "number"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "parent_task_id": {"type": "string", "description": "Make this a subtask"},
                "on_done_create": {"type": "string", "description": "Title of a card to spawn automatically when this one is moved to done"},
                "goal_id": {"type": "string", "description": "PASS THIS. The nag joins on goal_id — a card without one is invisible to the conscience and will never be surfaced again. Inherited automatically from parent_task_id if you pass that instead."},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="task_update",
        description="Change a card: status, owner, blocker, title, detail, due date, priority. Only the fields you pass are touched.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "description": "One task ID, or a LIST of them to apply the same change to many cards at once (a board sweep is one call, not thirty).",
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                },
                "status": {"type": "string", "description": "todo, doing, blocked, done. Setting blocked_on auto-moves status to blocked unless you set status yourself; clearing it moves blocked -> todo."},
                "owner": {"type": "string"},
                "title": {"type": "string"},
                "detail": {"type": "string"},
                "blocked_on": {"type": "string", "description": "Free text: what/who it waits on. Pass empty string to clear. Setting this moves status to blocked automatically."},
                "priority": {"type": "number"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "project": {"type": "string"},
                "parent_task_id": {"type": "string"},
                "goal_id": {"type": "string", "description": "Link this card to a goal. The nag joins on this — an unlinked card is invisible to the conscience."},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="task_delete",
        description=(
            "Remove a card entirely (and its comments/events). For throwaway or duplicate cards only — "
            "to close real work use task_update status=done, which preserves the history. "
            "Refuses to delete a card that has subtasks unless cascade=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "cascade": {"type": "boolean", "description": "Also delete subtasks (default false)"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="task_note",
        description=(
            "Write a note onto one or MANY cards at once — pass a list of task_ids and it comments on all of them. "
            "kind='note' for context, 'feedback' for opinion, 'work' with minutes to log effort. "
            "kind='work' also moves a 'todo' card to 'doing' automatically (never touches blocked, never completes). "
            "Batch exists so a full board pass is one call, not thirty."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_ids": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "One task ID, or a list of them",
                },
                "body": {"type": "string", "description": "The note. Same text goes on every listed card."},
                "notes": {
                    "type": "object",
                    "description": "Alternative to task_ids+body: a map of {task_id: note_body} to write DIFFERENT notes in one call.",
                },
                "kind": {"type": "string", "description": "note (default) | feedback | work"},
                "minutes": {"type": "integer", "description": "Effort in minutes, for kind='work'"},
                "author": {"type": "string", "description": f"Defaults to {GOAL_OWNER}"},
            },
        },
    ),
    Tool(
        name="check_out_card",
        description=(
            "CLAIM A CARD BEFORE WORKING IT. One card at a time, board-wide. "
            "Returns the FULL card, every prior comment WITH ITS AGE, the repo's current HEAD, "
            "and the working rules for this session. "
            "Checking out moves the card to 'doing' and records the git sha so check_in_card "
            "can DERIVE what you changed instead of asking you to remember it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Card id (8-char prefix is enough)"},
                "force": {
                    "type": "boolean",
                    "description": "Take over a checkout held by someone else. Only after reading who holds it.",
                },
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="check_in_card",
        description=(
            "HAND THE CARD BACK. Computes what actually changed in git since check-out and writes "
            "it onto the card automatically \u2014 you supply only what git cannot know: why, what you "
            "decided AGAINST, and what is still unverified. "
            "REFUSES status='done' when no commits were made or the tree is dirty."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Card id"},
                "status": {
                    "type": "string",
                    "description": "done | todo (more to do) | blocked",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "The part git cannot derive: WHY, what you decided against and why, "
                        "what is verified vs still unverified. Not a list of files \u2014 that is computed."
                    ),
                },
                "blocked_on": {"type": "string", "description": "Required when status=blocked"},
                "minutes": {"type": "integer", "description": "Effort, optional"},
                "reasoning_only": {
                    "type": "boolean",
                    "description": "Close as done with NO repo evidence. Only for genuinely non-code work; say what the evidence is in the summary.",
                },
                "allow_dirty": {
                    "type": "boolean",
                    "description": "Close as done with uncommitted changes. Say why in the summary.",
                },
                "evidence_commits": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Shas for work finished BEFORE this checkout (closing a "
                                    "backlog). Each is verified to exist in the repo and "
                                    "written onto the card; an unresolvable sha is refused."),
                },
                "force": {
                    "type": "boolean",
                    "description": ("Close a card checked out by ANOTHER INSTANCE. Refused "
                                    "without this. If you only meant to record that you looked "
                                    "at the card, use task_note instead -- it changes no status "
                                    "and cannot release someone's lock."),
                },
            },
            "required": ["task_id", "status", "summary"],
        },
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        db = get_db()
        result = _handle_tool(db, name, arguments)
        db.close()
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
    except Exception as e:
        logger.exception("Tool error")
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


def _handle_tool(db, name: str, args: dict) -> dict:
    if name == "create_goal":
        return _create_goal(db, args)
    elif name == "update_goal":
        return _update_goal(db, args)
    elif name == "mark_worked":
        return _mark_worked(db, args)
    elif name == "complete_goal":
        return _complete_goal(db, args)
    elif name == "abandon_goal":
        return _abandon_goal(db, args)
    elif name == "list_goals":
        return _list_goals(db, args)
    elif name == "get_goal":
        return _get_goal(db, args)
    elif name == "get_neglected":
        return _get_neglected(db, args)
    elif name == "auto_transition_goals":
        return _auto_transition_goals(db, args)
    elif name == "run_review":
        return _run_review(db, args)
    elif name == "board":
        return _board(db, args)
    elif name == "task_get":
        return _task_get(db, args)
    elif name == "task_create":
        return _task_create(db, args)
    elif name == "task_update":
        return _task_update(db, args)
    elif name == "task_note":
        return _task_note(db, args)
    elif name == "task_delete":
        return _task_delete(db, args)
    elif name == "check_out_card":
        return _check_out_card(db, args)
    elif name == "check_in_card":
        return _check_in_card(db, args)
    else:
        return {"error": f"Unknown tool: {name}"}

TASK_STATUSES = ["todo", "doing", "blocked", "done"]

BOARD_DETAIL_CHARS = 400


def _resolve_task_id(db, task_id: str) -> str:
    row = db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row:
        return row["id"]
    rows = db.execute(
        "SELECT id FROM tasks WHERE id LIKE ?", (task_id + "%",)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise ValueError(f"No task matching '{task_id}'")
    raise ValueError(
        f"'{task_id}' is ambiguous ({len(rows)} matches) -- pass more characters"
    )


def _board(db, args: dict) -> dict:
    where, params = [], []
    if args.get("project"):
        where.append("project = ?")
        params.append(args["project"])
    if args.get("status"):
        where.append("status = ?")
        params.append(args["status"])
    elif not args.get("include_done"):
        where.append("status <> 'done'")
    if args.get("owner"):
        where.append("owner = ?")
        params.append(args["owner"])
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    rows = db.execute(
        f"""SELECT id, project, title, detail, status, owner, blocked_on, priority,
                   due_date, parent_task_id, on_done_create, goal_id
            FROM tasks {clause}
            ORDER BY project, status, priority DESC NULLS LAST, created_at""",
        tuple(params),
    ).fetchall()
    tasks = [dict(r) for r in rows]

    for t in tasks:
        d = t.get("detail") or ""
        if len(d) > BOARD_DETAIL_CHARS:
            t["detail"] = (d[:BOARD_DETAIL_CHARS]
                           + f"\n… [{len(d) - BOARD_DETAIL_CHARS} more chars — task_get "
                             f"{t['id'][:8]} for the full card]")
            t["detail_truncated"] = True

    kids = {}
    for r in db.execute(
        "SELECT parent_task_id, status FROM tasks WHERE parent_task_id IS NOT NULL", ()
    ).fetchall():
        d = kids.setdefault(r["parent_task_id"], {"total": 0, "done": 0, "blocked": 0})
        d["total"] += 1
        if r["status"] == "done":
            d["done"] += 1
        if r["status"] == "blocked":
            d["blocked"] += 1

    counts = {}
    for r in db.execute(
        f"SELECT status, count(*) AS n FROM tasks {clause} GROUP BY status", tuple(params)
    ).fetchall():
        counts[r["status"]] = r["n"]

    ncomments = {}
    for r in db.execute(
        "SELECT task_id, count(*) AS n FROM task_comments GROUP BY task_id", ()
    ).fetchall():
        ncomments[r["task_id"]] = r["n"]

    lines, current = [], None
    for t in tasks:
        if t["project"] != current:
            current = t["project"]
            lines.append(f"\n== {current.upper()} ==")
        sid = t["id"][:8]
        marks = []
        if t["parent_task_id"]:
            marks.append(f"subtask of {t['parent_task_id'][:8]}")
        roll = kids.get(t["id"])
        if roll:
            marks.append(f"{roll['done']}/{roll['total']}" + (" !" if roll["blocked"] else ""))
        if t["blocked_on"]:
            marks.append(f"BLOCKED ON {t['blocked_on']}")
        if t["due_date"]:
            marks.append(f"due {t['due_date']}")
        if ncomments.get(t["id"]):
            marks.append(f"{ncomments[t['id']]} notes")
        if t["on_done_create"]:
            marks.append("spawns follow-on")
        suffix = ("  [" + "; ".join(marks) + "]") if marks else ""
        lines.append(f"  {sid}  {t['status']:<7} {t['owner']:<7} {t['title']}{suffix}")

    return {
        "counts": counts,
        "shown": len(tasks),
        "view": "\n".join(lines).strip(),
        "tasks": tasks,
    }


def _task_get(db, args: dict) -> dict:
    tid = _resolve_task_id(db, args["task_id"])
    task = dict(db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    task["subtasks"] = [
        dict(r)
        for r in db.execute(
            "SELECT id, title, status, owner, blocked_on FROM tasks "
            "WHERE parent_task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall()
    ]
    task["comments"] = [
        dict(r)
        for r in db.execute(
            "SELECT id, author, kind, body, minutes, created_at FROM task_comments "
            "WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall()
    ]
    task["work_minutes"] = sum(
        c["minutes"] or 0 for c in task["comments"] if c["kind"] == "work"
    )
    try:
        task["events"] = [
            dict(r)
            for r in db.execute(
                "SELECT actor, field, old_value, new_value, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY created_at",
                (tid,),
            ).fetchall()
        ]
    except Exception:
        task["events"] = []
    return task


def _task_delete(db, args: dict) -> dict:
    tid = _resolve_task_id(db, args["task_id"])
    task = dict(db.execute("SELECT id, title FROM tasks WHERE id = ?", (tid,)).fetchone())

    kids = [
        dict(r)
        for r in db.execute(
            "SELECT id, title FROM tasks WHERE parent_task_id = ?", (tid,)
        ).fetchall()
    ]
    if kids and not args.get("cascade"):
        return {
            "error": f"'{task['title']}' has {len(kids)} subtask(s); pass cascade=true to delete them too",
            "subtasks": [{"id": k["id"][:8], "title": k["title"]} for k in kids],
        }

    ids = [tid] + [k["id"] for k in kids]
    for i in ids:
        for sql in (
            "DELETE FROM task_comments WHERE task_id = ?",
            "DELETE FROM task_events WHERE task_id = ?",
            "DELETE FROM tasks WHERE id = ?",
        ):
            try:
                db.execute(sql, (i,))
            except Exception as e:
                logger.warning("delete step failed for %s: %s", i[:8], e)
    db.commit()

    # ANY(?) is Postgres-only; the DELETE above already committed, so it failed a successful delete.
    placeholders = ",".join("?" for _ in ids)
    still = db.execute(
        f"SELECT count(*) AS n FROM tasks WHERE id IN ({placeholders})", tuple(ids)
    ).fetchone()["n"]
    if still:
        return {"error": f"delete reported success but {still} row(s) survived", "ids": ids}
    return {"deleted": [i[:8] for i in ids], "titles": [task["title"]] + [k["title"] for k in kids]}


def _task_create(db, args: dict) -> dict:
    tid = str(uuid.uuid4())
    status = args.get("status", "todo")
    if status not in TASK_STATUSES:
        return {"error": f"status must be one of {TASK_STATUSES}"}
    parent = args.get("parent_task_id")
    if parent:
        parent = _resolve_task_id(db, parent)

    goal_id = args.get("goal_id")
    if not goal_id and parent:
        prow = db.execute("SELECT goal_id FROM tasks WHERE id = ?", (parent,)).fetchone()
        if prow:
            goal_id = prow["goal_id"]
    done_at_sql = "CURRENT_TIMESTAMP" if status == "done" else "NULL"
    db.execute(
        f"""INSERT INTO tasks (id, project, goal_id, title, detail, status, owner,
                              blocked_on, source, priority, due_date, on_done_create,
                              parent_task_id, done_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,{done_at_sql})""",
        (
            tid,
            args.get("project", "general"),
            goal_id,
            args["title"],
            args.get("detail"),
            status,
            args.get("owner", GOAL_OWNER),
            args.get("blocked_on"),
            "goals_mcp",
            args.get("priority", 0.5),
            args.get("due_date"),
            args.get("on_done_create"),
            parent,
        ),
    )
    db.commit()
    out = {"created": dict(db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())}
    if not goal_id:
        out["warning"] = (
            f"Card {tid[:8]} has NO goal_id, so the conscience will never nag about it. "
            "It is written down but not remembered. Link it with "
            f"task_update(task_id='{tid[:8]}', goal_id=...) or accept that it is a note, not a task."
        )
    claim = _unobserved_absence_warning(args.get("title", ""), args.get("detail") or "")
    if claim:
        out["absence_check"] = claim
    timing = _timing_fault_warning(args.get("title", ""), args.get("detail") or "")
    if timing:
        out["timing_check"] = timing
    return out

_ABSENCE_PATTERNS = (
    r"\bis missing\b", r"\bare missing\b", r"\bnot wired\b", r"\bnot present\b",
    r"\bdoes ?n[o']t exist\b", r"\bdoes not exist\b", r"\bnever (?:defined|registered|added|called)\b",
    r"\bno (?:such|way to|button|panel|option|handler|endpoint|command)\b",
    r"\bzero hits\b", r"\bnowhere\b", r"\bcompletely absent\b", r"\bnothing (?:calls|reads|uses|sets)\b",
    r"\bwe (?:have|ship) none\b", r"\bthere is no\b",
)

_OBSERVED_PATTERNS = (
    r"\bscreenshot\b", r"\bobserved\b", r"\bdevtools\b", r"\bnetwork tab\b",
    r"\bcurl\b", r"\bping\b", r"\breproduc", r"\bin the running\b", r"\bat runtime\b",
    r"\bclicked\b", r"\bopened the\b", r"\bmeasured\b", r"\bverified live\b",
    r"\bbundle\b", r"\bnode_modules\b", r"\bstrace\b", r"\bpacket\b", r"\bscreen recording\b",
)

_TIMING_FAULT_PATTERNS = (
    r"\bnot arriving\b", r"\bnever arrives?\b", r"\bis ?n[o']t being (?:updated|written|synced)\b",
    r"\bnot (?:propagating|syncing|updating)\b", r"\bstopped (?:updating|syncing|arriving)\b",
    r"\bnever returns?\b", r"\bhung\b", r"\bwedged\b", r"\bstuck\b", r"\bhas ?n[o']t arrived\b",
    r"\bno longer (?:runs|fires|arrives|updates)\b", r"\bstale\b.{0,40}\bnot lagging\b",
)

_BASELINE_PATTERNS = (
    r"\bbaseline\b", r"\bnormally takes\b", r"\busually takes\b", r"\btypical(?:ly)? (?:latency|takes)\b",
    r"\bnormal (?:latency|cadence|interval)\b", r"\bwaited\s+\d+\s*(?:min|hour|h\b)",
    r"\bover\s+\d+\s*(?:minutes|hours)\b", r"\bfor\s+\d+\s*(?:minutes|hours)\b",
    r"\bsampled\b", r"\bcompared (?:against|to) (?:the )?(?:normal|prior|previous)\b",
    r"\bhistoric(?:al)? (?:timing|latency)\b", r"\bp9[59]\b",
)


def _timing_fault_warning(title: str, detail: str) -> str:
    low = f"{title}\n{detail}".lower()

    hits = [p for p in _TIMING_FAULT_PATTERNS if re.search(p, low)]
    if not hits:
        return ""
    if any(re.search(p, low) for p in _BASELINE_PATTERNS):
        return ""

    return (
        "TIMING FAULT DECLARED WITHOUT A BASELINE. This card says something is not "
        f"arriving/returning/updating ({len(hits)} phrase(s) matched) but never states how "
        "long it NORMALLY takes, or how long you actually waited.\n"
        "⚠️ curl/measured/verified do NOT silence this check, and that is deliberate. "
        "The classic version of this mistake is declaring a synced file 'not arriving' "
        "after six minutes, having fetched it correctly twice — and the file lands twenty "
        "minutes later, because nobody ever measured what normal was. Looking was never "
        "the missing step.\n"
        "The probe variant: reading 60 bytes from a service whose banner is 41 bytes "
        "blocks, times out, and 'proves' the service is wedged. It was healthy.\n"
        "ANSWER THIS BEFORE WORKING THE CARD: what is the normal latency, and how does your "
        "observation window compare to it? If you do not know, you have not found a fault — "
        "you have found an absence of patience."
    )


def _unobserved_absence_warning(title: str, detail: str) -> str:
    text = f"{title}\n{detail}"
    low = text.lower()

    hits = [p for p in _ABSENCE_PATTERNS if re.search(p, low)]
    if not hits:
        return ""
    if any(re.search(p, low) for p in _OBSERVED_PATTERNS):
        return ""

    return (
        "ABSENCE CLAIMED WITHOUT OBSERVATION. This card asserts something is missing "
        f"({len(hits)} phrase(s) matched) but contains no sign of having looked at the "
        "running system — no screenshot, no devtools, no curl, no bundle inspection.\n"
        "On Aug 9 2026 this exact shape produced a wrong card: 'GrapesJS is missing Style "
        "Manager, Traits, undo/redo, preview, code view'. All five were present; the plugin "
        "installs them at init where no grep could see them. The correction was already in "
        "memory 5b26197c (82 accesses) and did not fire.\n"
        "Before anyone works this card: LOOK AT THE THING. If you already did, say how — "
        "the word is what silences this check, and it is also what makes the card trustworthy."
    )


_CODE_PROJECTS = {
    p.strip().lower()
    for p in os.environ.get("GOAL_CODE_PROJECTS", "").split(",")
    if p.strip()
}

_SHIPPED_PATTERNS = [
    r"\bcommit(ted|s)?\b", r"\bpushed?\b", r"\bmerged?\b", r"\bdeployed?\b",
    r"\breleased?\b", r"\bshipp?(ed|s)?\b",
    r"\bno code\b", r"\bnot a code change\b", r"\bdocs only\b",
    r"\bdocumentation only\b", r"\bcopy change\b", r"\bcontent only\b",
    r"\bresearch only\b", r"\bdecision only\b", r"\bspec only\b", r"\bpolicy only\b",
    r"\bno code was written\b", r"\bnothing to ship\b",
]


def _unshippable_close_warning(project: str, title: str, detail: str, comments: str) -> str:
    if (project or "").lower() not in _CODE_PROJECTS:
        return ""
    text = "\n".join([title or "", detail or "", comments or ""]).lower()
    if any(re.search(p, text) for p in _SHIPPED_PATTERNS):
        return ""

    return (
        "CLOSED WITHOUT SAYING WHERE THE CODE WENT. This card is in a code project and "
        "nothing in it mentions a commit, a push, or that no code was involved.\n"
        "The failure this catches: real, correct, carefully-logged code written into a "
        "working directory with no .git, no remote and no commits. The work log is honest "
        "and itemised, the card looks complete, and none of it can ever reach the shipping "
        "repo. Nobody notices until someone asks whether a code path exists at all.\n"
        "A CARD IS NOT DONE WHEN THE CODE IS WRITTEN. IT IS DONE WHEN THE CODE IS SOMEWHERE "
        "THE PRODUCT CAN REACH. Two commands settle it:\n"
        "    git -C <tree> log --oneline -1     # errors -> the work CANNOT ship\n"
        "    git -C <tree> status --porcelain   # output -> uncommitted, so it has not shipped\n"
        "Then say so in the card. If this card changed no code, say that instead -- either "
        "sentence silences this."
    )


def _task_update(db, args: dict) -> dict:
    raw_ids = args.get("task_id")
    if isinstance(raw_ids, (list, tuple)):
        results, failed = [], []
        for one in raw_ids:
            sub = dict(args)
            sub["task_id"] = one
            try:
                r = _task_update(db, sub)
            except Exception as e:
                failed.append({"task_id": one, "error": str(e)})
                continue
            (failed if isinstance(r, dict) and r.get("error") else results).append(r)
        return {"updated_count": len(results), "updated": results,
                "failed": failed, "bulk": True}

    tid = _resolve_task_id(db, args["task_id"])
    before = dict(db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    coherence = None
    if "blocked_on" in args and "status" not in args:
        if args["blocked_on"]:
            if before["status"] != "blocked":
                args = dict(args, status="blocked")
                coherence = ("blocked_on was set, so status moved "
                             f"{before['status']} -> blocked automatically")
        elif before["status"] == "blocked":
            args = dict(args, status="todo")
            coherence = "blocked_on was cleared, so status moved blocked -> todo automatically"
    elif args.get("status") and args.get("status") != "blocked":
        still_blocked = args.get("blocked_on", before.get("blocked_on"))
        if still_blocked:
            coherence = (f"⚠️ status={args['status']} while blocked_on is still set "
                         f"({str(still_blocked)[:40]!r}). Clear blocked_on with \"\" if it no "
                         "longer applies -- a card that records what it waits on but does "
                         "not say 'blocked' is invisible as a blocker.")

    sets, params, changed = [], [], {}
    for field in ("status", "owner", "title", "detail", "priority", "due_date",
                  "project", "parent_task_id", "blocked_on", "goal_id"):
        if field not in args:
            continue
        val = args[field]
        if field == "status" and val not in TASK_STATUSES:
            return {"error": f"status must be one of {TASK_STATUSES}"}
        if field == "blocked_on" and val == "":
            val = None
        if field == "parent_task_id" and val:
            val = _resolve_task_id(db, val)
        sets.append(f"{field} = ?")
        params.append(val)
        changed[field] = {"from": before.get(field), "to": val}

    if not sets:
        return {"error": "nothing to update -- pass at least one field"}

    sets.append("updated_at = CURRENT_TIMESTAMP")
    if args.get("status") == "done" and before["status"] != "done":
        sets.append("done_at = CURRENT_TIMESTAMP")
    params.append(tid)
    db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))

    for field, delta in changed.items():
        try:
            db.execute(
                "INSERT INTO task_events (task_id, actor, field, old_value, new_value) "
                "VALUES (?,?,?,?,?)",
                (tid, GOAL_OWNER, field, str(delta["from"]), str(delta["to"])),
            )
        except Exception as e:
            logger.warning("task_events insert failed for %s.%s: %s", tid[:8], field, e)

    spawned = None
    if args.get("status") == "done" and before["status"] != "done" and before.get("on_done_create"):
        spawned = _task_create(
            db,
            {
                "title": before["on_done_create"],
                "project": before["project"],
                "owner": before["owner"],
                "goal_id": before["goal_id"],
                "detail": f"Auto-created when '{before['title']}' was completed.",
            },
        )["created"]

    db.commit()
    out = {"updated": tid, "changed": changed}
    if coherence:
        out["coherence"] = coherence
    if spawned:
        out["spawned_follow_on"] = spawned

    if args.get("status") == "done" and before["status"] != "done":
        try:
            rows = db.execute(
                "SELECT body FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchall()
            comments = "\n".join((r["body"] or "") for r in rows)
        except Exception as e:
            logger.warning("close-check could not read comments for %s: %s", tid[:8], e)
            comments = ""
        warn = _unshippable_close_warning(
            args.get("project") or before.get("project") or "",
            args.get("title") or before.get("title") or "",
            args.get("detail") or before.get("detail") or "",
            comments,
        )
        if warn:
            out["warning"] = warn
    return out


def _task_note(db, args: dict) -> dict:
    kind = args.get("kind", "note")
    if kind not in ("note", "feedback", "work"):
        return {"error": "kind must be note, feedback or work"}
    author = args.get("author", GOAL_OWNER)
    minutes = args.get("minutes")
    pairs = []
    if args.get("notes"):
        pairs = list(args["notes"].items())
    else:
        ids = args.get("task_ids")
        if not ids:
            return {"error": "pass task_ids (+body) or notes={task_id: body}"}
        if isinstance(ids, str):
            ids = [ids]
        body = args.get("body")
        if not body:
            return {"error": "body is required when using task_ids"}
        pairs = [(t, body) for t in ids]

    written, failed, started = [], [], []
    for raw_id, body in pairs:
        try:
            tid = _resolve_task_id(db, raw_id)
            db.execute(
                "INSERT INTO task_comments (task_id, author, kind, body, minutes) "
                "VALUES (?,?,?,?,?)",
                (tid, author, kind, body, minutes),
            )
            written.append(tid[:8])
            if kind == "work":
                cur = db.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
                if cur and dict(cur).get("status") == "todo":
                    db.execute("UPDATE tasks SET status = 'doing', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (tid,))
                    started.append(tid[:8])
        except Exception as e:
            failed.append({"task_id": raw_id, "error": str(e)})
    db.commit()
    result = {"wrote": len(written), "task_ids": written, "failed": failed, "kind": kind}
    if started:
        result["moved_to_doing"] = started
    return result

def _create_goal(db, args: dict) -> dict:
    goal_id = str(uuid.uuid4())[:8]
    ts = now_iso()
    status = args.get("status", "active")

    auto_transitions = args.get("auto_transitions")
    if auto_transitions and isinstance(auto_transitions, str):
        try:
            auto_transitions = json.loads(auto_transitions)
        except json.JSONDecodeError:
            auto_transitions = DEFAULT_AUTO_RULES
    elif not auto_transitions:
        auto_transitions = DEFAULT_AUTO_RULES

    kind = (args.get("kind") or "work").strip().lower()
    if kind not in VALID_GOAL_KINDS:
        return {"error": f"Invalid kind '{kind}'. Must be one of: {', '.join(VALID_GOAL_KINDS)}"}

    db.execute(
        """INSERT INTO goals (id, description, priority, category, status,
           created_at, decay_rate, success_criteria, owner, notes, parent_goal_id,
           blocked_by, auto_transitions, last_action_type, stale_days, decision_deadline, action_log,
           decision_owner, next_action, kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observation', 0, ?, ?, ?, ?, ?)""",
        (
            goal_id,
            args["description"],
            args.get("priority", 0.5),
            args.get("category", "general"),
            status,
            ts,
            args.get("decay_rate", 1.0),
            args.get("success_criteria"),
            args.get("owner", GOAL_OWNER),
            args.get("notes"),
            args.get("parent_goal_id"),
            args.get("blocked_by"),
            json.dumps(auto_transitions),
            args.get("decision_deadline"),
            json.dumps([]),
            args.get("decision_owner"),
            args.get("next_action"),
            kind,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return {"created": goal_to_dict(row)}


def _update_goal(db, args: dict) -> dict:
    goal_id = args.pop("goal_id")
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        return {"error": f"Goal {goal_id} not found"}

    updatable = [
        "description", "priority", "category", "decay_rate",
        "success_criteria", "owner", "notes", "postponed_until",
        "status", "blocked_by", "auto_transitions", "decision_deadline",
        "decision_owner", "next_action", "kind",
    ]
    if "kind" in args:
        k = (args["kind"] or "").strip().lower()
        if k not in VALID_GOAL_KINDS:
            return {"error": f"Invalid kind '{k}'. Must be one of: {', '.join(VALID_GOAL_KINDS)}"}
        args["kind"] = k
    sets = []
    vals = []
    for field in updatable:
        if field in args:
            sets.append(f"{field} = ?")
            val = args[field]
            if field in ("auto_transitions",) and isinstance(val, str):
                try:
                    val = json.loads(val)
                except json.JSONDecodeError:
                    pass
            vals.append(val)

    if not sets:
        return {"error": "No fields to update"}

    vals.append(goal_id)
    db.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return {"updated": goal_to_dict(row)}


def _mark_worked(db, args: dict) -> dict:
    goal_id = args["goal_id"]
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        return {"error": f"Goal {goal_id} not found"}

    ts = now_iso()
    action_type = args.get("action_type", "observation")

    if action_type not in VALID_ACTION_TYPES:
        return {"error": f"Invalid action_type '{action_type}'. Must be one of: {', '.join(VALID_ACTION_TYPES)}"}

    unknown = set(args) - {"goal_id", "work_description", "duration_minutes", "action_type"}
    if unknown:
        return {"error": f"Unknown parameter(s): {sorted(unknown)}. Did you mean 'work_description'? "
                         f"Nothing was logged — no last_worked bump, no action_log entry."}
    work_description = (args.get("work_description") or "").strip()
    if not work_description:
        return {"error": "work_description is required and must be non-empty. An empty entry still counts "
                         "toward observation_only_count. Nothing was logged."}

    db.execute("UPDATE goals SET last_worked = ? WHERE id = ?", (ts, goal_id))

    db.execute("UPDATE goals SET last_action_type = ? WHERE id = ?", (action_type, goal_id))

    action_log = dict(row).get("action_log")
    if action_log and isinstance(action_log, str):
        try:
            action_log = json.loads(action_log)
        except json.JSONDecodeError:
            action_log = []
    elif not action_log:
        action_log = []

    entry = {
        "timestamp": ts,
        "description": work_description,
        "type": action_type,
        "duration_minutes": args.get("duration_minutes"),
    }
    action_log.append(entry)

    db.execute(
        "INSERT INTO goal_work_log (goal_id, timestamp, description, duration_minutes, action_type) VALUES (?, ?, ?, ?, ?)",
        (goal_id, ts, work_description, args.get("duration_minutes"), action_type),
    )
    db.execute(
        "UPDATE goals SET action_log = ? WHERE id = ?",
        (json.dumps(action_log), goal_id)
    )

    db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    result = goal_to_dict(row)
    result["action_logged"] = action_log[-1]
    return {"marked_worked": result}


def _complete_goal(db, args: dict) -> dict:
    goal_id = args["goal_id"]
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        return {"error": f"Goal {goal_id} not found"}

    ts = now_iso()
    notes = row["notes"] or ""
    if args.get("completion_notes"):
        notes += f"\n\n[COMPLETED {ts}] {args['completion_notes']}"

    db.execute(
        "UPDATE goals SET status = 'completed', completed_at = ?, notes = ? WHERE id = ?",
        (ts, notes, goal_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return {"completed": goal_to_dict(row)}


def _abandon_goal(db, args: dict) -> dict:
    goal_id = args["goal_id"]
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        return {"error": f"Goal {goal_id} not found"}

    ts = now_iso()
    notes = row["notes"] or ""
    reason = args.get("reason", "No reason given")
    notes += f"\n\n[ABANDONED {ts}] {reason}"

    db.execute(
        "UPDATE goals SET status = 'abandoned', notes = ? WHERE id = ?",
        (notes, goal_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return {"abandoned": goal_to_dict(row)}


def _summarize_goal(g: dict, notes_chars: int = 600) -> dict:
    """Summary views drop action_log (73% of payload, measured) and truncate notes, keeping next_action in full because it is the field a nag needs."""
    out = dict(g)
    log = out.get("action_log") or []
    if isinstance(log, list):
        out["action_log_count"] = len(log)
        if log:
            last = log[-1] or {}
            desc = str(last.get("description", "")).strip().replace("\n", " ")
            out["last_action"] = {
                "timestamp": last.get("timestamp"),
                "type": last.get("type"),
                "summary": desc[:200] + ("..." if len(desc) > 200 else ""),
            }
        out.pop("action_log", None)
        out["_detail"] = "action_log omitted -- call get_goal for the full work log"
    notes = out.get("notes")
    if isinstance(notes, str) and len(notes) > notes_chars:
        out["notes"] = notes[:notes_chars] + f"... [{len(notes)} chars total, see get_goal]"
    return out


def _list_goals(db, args: dict) -> dict:
    status = args.get("status", "active")
    where = []
    vals = []

    if status != "all":
        where.append("status = ?")
        vals.append(status)
    if args.get("category"):
        where.append("category = ?")
        vals.append(args["category"])
    if args.get("owner"):
        where.append("owner = ?")
        vals.append(args["owner"])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = db.execute(f"SELECT * FROM goals {clause}", vals).fetchall()
    goals = [goal_to_dict(r) for r in rows]

    sort_by = args.get("sort_by", "urgency")
    if sort_by == "urgency":
        goals.sort(key=lambda g: g["urgency"], reverse=True)
    elif sort_by == "priority":
        goals.sort(key=lambda g: g["priority"], reverse=True)
    elif sort_by == "created_at":
        goals.sort(key=lambda g: g["created_at"], reverse=True)
    elif sort_by == "last_worked":
        goals.sort(key=lambda g: g.get("last_worked") or "", reverse=True)

    if not args.get("full"):
        goals = [_summarize_goal(g) for g in goals]
    return {"goals": goals, "count": len(goals)}


def _get_goal(db, args: dict) -> dict:
    goal_id = args["goal_id"]
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        return {"error": f"Goal {goal_id} not found"}

    work_log = db.execute(
        "SELECT * FROM goal_work_log WHERE goal_id = ? ORDER BY timestamp DESC LIMIT 20",
        (goal_id,),
    ).fetchall()

    state_events = db.execute(
        "SELECT * FROM goal_state_events WHERE goal_id = ? ORDER BY timestamp DESC LIMIT 20",
        (goal_id,),
    ).fetchall()

    children = db.execute(
        "SELECT * FROM goals WHERE parent_goal_id = ?", (goal_id,)
    ).fetchall()

    blocked_by_info = None
    if row["blocked_by"]:   # sqlite3.Row has no .get(); fires only when a blocker exists
        blocker = db.execute(
            "SELECT id, description, status FROM goals WHERE id = ?",
            (row["blocked_by"],)
        ).fetchone()
        if blocker:
            blocked_by_info = dict(blocker)

    return {
        "goal": goal_to_dict(row),
        "work_log": [dict(w) for w in work_log],
        "state_events": [dict(s) for s in state_events],
        "sub_goals": [goal_to_dict(c) for c in children],
        "blocked_by": blocked_by_info,
    }


def _get_neglected(db, args: dict) -> dict:
    limit = args.get("limit", 5)
    rows = db.execute("SELECT * FROM goals WHERE status = 'active'").fetchall()
    goals = [goal_to_dict(r) for r in rows]
    goals = [g for g in goals if (g.get("kind") or "work") != "standing"]
    goals.sort(key=lambda g: g["urgency"], reverse=True)
    top = goals[:limit]
    if not args.get("full"):
        top = [_summarize_goal(g) for g in top]

    out = {"neglected": top, "total_active": len(goals)}

    if args.get("include_standing", True):
        standing = _standing_goals(db, limit=1)
        if standing:
            g = standing[0]
            out["standing"] = {
                "id": g["id"],
                "description": g["description"],
                "note": "Not overdue and never will be. Shown because you said it "
                        "was yours, not because a timer expired.",
            }
    return out


def _auto_transition_goals(db, args: dict) -> dict:
    """Check all goals for auto-transition conditions."""
    rows = db.execute("SELECT * FROM goals WHERE status IN (?, ?, ?)",
                     ("active", "observation_saturation", "stale")).fetchall()
    changed = []
    for row in rows:
        if apply_auto_transitions(db, row):
            changed.append(_parse_goal(row))
    return {"changed": changed, "total_checked": len(rows)}


def _run_review(db, args: dict) -> dict:
    """Full goal review: scan all goals, apply transitions, check deadlines."""
    rows = db.execute("SELECT * FROM goals WHERE status != 'completed' AND status != 'abandoned'").fetchall()

    changed = []
    for row in rows:
        if apply_auto_transitions(db, row):
            changed.append(_parse_goal(row))

    deadlines = []
    for row in rows:
        goal = _parse_goal(row)
        deadline = goal.get("decision_deadline")
        if deadline:
            try:
                dl = datetime.fromisoformat(deadline)
                if datetime.now() > dl and goal["status"] not in (
                    "completed", "abandoned", "postponed"
                ):
                    deadlines.append({
                        "goal_id": goal["id"],
                        "description": goal["description"],
                        "deadline": deadline,
                        "current_status": goal["status"],
                        "action": "Decision deadline passed — must resolve or extend",
                    })
            except (ValueError, TypeError):
                pass

    return {
        "total_goals": len(rows),
        "auto_transitions": changed,
        "deadlines_passed": deadlines,
        "summary": {
            "active": sum(1 for r in rows if _parse_goal(r)["status"] == "active"),
            "observation_saturation": sum(1 for r in rows if _parse_goal(r)["status"] == "observation_saturation"),
            "stale": sum(1 for r in rows if _parse_goal(r)["status"] == "stale"),
            "escalation_needed": sum(1 for r in rows if _parse_goal(r)["status"] == "escalation_needed"),
            "ready_to_act": sum(1 for r in rows if _parse_goal(r)["status"] == "ready_to_act"),
            "postponed": sum(1 for r in rows if _parse_goal(r)["status"] == "postponed"),
        }
    }


CHECKOUT_STALE_HOURS = 4

def _load_project_repos():
    path = Path(os.environ.get(
        "GOAL_PROJECT_REPOS", Path(__file__).parent / "project_repos.json"))
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}
    except Exception as e:
        logger.warning("could not read %s: %s", path, e)
        return {}, {}
    return cfg.get("repos") or {}, cfg.get("aliases") or {}


PROJECT_REPOS, REPO_ALIASES = _load_project_repos()


def _named_volumes(text):
    """Absolute paths in card text -> volume roots, never full paths, because testing a full path is always-red on 'remove X' cards."""
    import re as _re
    out = set()
    for m in _re.finditer(r"\b([A-Za-z]):[\\/]", text or ""):
        out.add(m.group(1) + ":\\")
    for m in _re.finditer(r"(?<![\w.:])(/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text or ""):
        out.add(m.group(1))
    return out


def _volume_reachable(anchor):
    """Platform first: os.path.isdir('/srv/app') returns True on Windows, so a POSIX path must be rejected by platform before the filesystem is consulted."""
    import os as _os
    win = _os.name == "nt"
    looks_windows = len(anchor) >= 2 and anchor[1] == ":"
    if looks_windows != win:
        return False
    return _os.path.isdir(anchor)


def _instance_id():
    """Identifies an instance rather than an agent, and carries no pid, so two copies of one agent cannot release each other's locks and a restart cannot lock the agent out of its own card."""
    try:
        import socket
        host = socket.gethostname().split(".")[0].lower()
    except Exception:
        host = "unknown"
    ctx = os.environ.get("GOAL_INSTANCE")
    if not ctx:
        ctx = "interactive" if os.environ.get("CLAUDECODE") else "runtime"
    return f"{GOAL_OWNER}@{host}/{ctx}"


def _holder_matches(stored, current):
    """A stored holder with no '@' is a pre-instance-scoping legacy row and matches any instance of that agent."""
    if not stored:
        return True
    if "@" not in stored:
        return stored == GOAL_OWNER
    return stored == current


def _ensure_checkout_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS card_checkout ("
        " task_id TEXT PRIMARY KEY,"
        " holder TEXT NOT NULL,"
        " checked_out_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " repo_path TEXT,"
        " head_sha TEXT,"
        " dirty_at_checkout INTEGER)"
    )


def _repo_for(project):
    """Repo path for a project, or None. None is a normal answer, not a failure."""
    import os
    path = PROJECT_REPOS.get((project or "").lower())
    if path and os.path.isdir(os.path.join(path, ".git")):
        return path
    return None


def _repo_for_card(task):
    r = _repo_for(task.get("project"))
    if r:
        return r
    hay = ((task.get("title") or "") + " " + (task.get("detail") or "")).lower()
    for word, key in list(REPO_ALIASES.items()) + [(k, k) for k in PROJECT_REPOS]:
        if word in hay:
            r = _repo_for(key)
            if r:
                return r
    return None


def _git(repo, *argv):
    """Run git, return stdout stripped, or None. Never raises: a missing repo, a
    missing git, or a detached state must degrade to 'no evidence' rather than
    taking down a board operation."""
    if not repo:
        return None
    import subprocess
    try:
        r = subprocess.run(["git", "-C", repo] + list(argv),
                           capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _age(ts):
    """Human age of a timestamp. The POINT of this function is that a note without an
    age reads as current, which is how an eleven-day-old measurement nearly cost me a
    duplicated feature."""
    from datetime import datetime, timezone
    if ts is None:
        return "unknown age"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return "unknown age"
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.utcnow()
    d = max(0.0, (now - ts).total_seconds())
    if d < 3600:
        return f"{int(d/60)}m ago"
    if d < 86400:
        return f"{d/3600:.0f}h ago"
    return f"{d/86400:.0f} DAYS ago"


def _check_out_card(db, args: dict) -> dict:
    _ensure_checkout_table(db)
    raw = args.get("task_id")
    if not raw:
        return {"error": "task_id required"}
    tid = _resolve_task_id(db, raw)
    holder = args.get("holder") or _instance_id()

    held = db.execute("SELECT * FROM card_checkout").fetchall()
    for h in held:
        if h["task_id"] == tid:
            break
        age_h = 999
        try:
            from datetime import datetime
            t = h["checked_out_at"]
            if isinstance(t, str):
                t = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            now = datetime.now(t.tzinfo) if t.tzinfo else datetime.utcnow()
            age_h = max(0.0, (now - t).total_seconds() / 3600.0)
        except Exception:
            pass
        if age_h >= CHECKOUT_STALE_HOURS or args.get("force"):
            db.execute("DELETE FROM card_checkout WHERE task_id = ?", (h["task_id"],))
        else:
            return {
                "error": "BOARD LOCKED",
                "held_card": h["task_id"][:8],
                "holder": h["holder"],
                "held_for": _age(h["checked_out_at"]),
                "what_to_do": (
                    "Finish or check that card in first. If that session is gone, "
                    f"it self-releases after {CHECKOUT_STALE_HOURS}h, or pass force=true "
                    "AFTER reading what it was doing."
                ),
            }

    task = _task_get(db, {"task_id": tid})
    if task.get("error"):
        return task

    repo = _repo_for_card(task)
    head = _git(repo, "rev-parse", "HEAD")
    dirty = _git(repo, "status", "--porcelain")
    dirty_n = len([l for l in (dirty or "").splitlines() if l.strip()])

    db.execute("DELETE FROM card_checkout WHERE task_id = ?", (tid,))
    db.execute(
        "INSERT INTO card_checkout (task_id, holder, repo_path, head_sha, dirty_at_checkout)"
        " VALUES (?,?,?,?,?)",
        (tid, holder, repo, head, dirty_n),
    )
    db.execute("UPDATE tasks SET status='doing', updated_at=CURRENT_TIMESTAMP"
               " WHERE id = ? AND status <> 'blocked'", (tid,))

    for c in task.get("comments", []) or []:
        c["age"] = _age(c.get("created_at"))

    return {
        "checked_out": tid[:8],
        "title": task.get("title"),
        "status": "doing",
        "card": task,
        "repo": {
            "path": repo,
            "head": (head or "")[:12] or None,
            "dirty_files_at_checkout": dirty_n,
            "note": ("No git repo mapped for this project. Reasoning-only card; "
                     "check_in will not be able to derive a diff."
                     if not repo else
                     "check_in will diff against this sha automatically."),
        },
        "HOW_THIS_CARD_GETS_WORKED": [
            "1. READ THE CARD ABOVE, INCLUDING EVERY COMMENT AND ITS AGE.",
            "2. THEN VERIFY IT AGAINST THE CODE. Notes on this board have been "
               "careful, accurate and WRONG eleven days later. On Aug 18 an Aug 7 "
               "measurement note nearly made me rebuild a feature that had already "
               "shipped. Dated evidence is still evidence with a date on it.",
            "3. ONE COMMIT PER FEATURE, and put the card id in the message. Splitting "
               "by card afterwards is IMPOSSIBLE -- proven Aug 18, when a day of "
               "interleaved work in two files could only be committed by file.",
            "4. Work THIS card. If you find something else, card it; do not drift.",
            "5. check_in_card(task_id, status, summary) when you stop -- even if you "
               "stop unfinished. status='todo' with a summary is an honest hand-back; "
               "silence is not.",
            "6. The summary is for what GIT CANNOT SEE: why, what you decided against, "
               "what is still unverified. The file list is computed for you.",
        ],
    }


def _check_in_card(db, args: dict) -> dict:
    _ensure_checkout_table(db)
    raw = args.get("task_id")
    status = (args.get("status") or "").lower()
    summary = args.get("summary")
    if not raw or not summary:
        return {"error": "task_id and summary are required"}
    if status not in ("done", "todo", "blocked"):
        return {"error": "status must be done, todo or blocked"}
    tid = _resolve_task_id(db, raw)

    row = db.execute("SELECT * FROM card_checkout WHERE task_id = ?", (tid,)).fetchone()
    if not row:
        return {"error": "That card is not checked out. check_out_card first -- "
                         "otherwise there is no sha to diff against and the record "
                         "would be your memory rather than the repository's."}

    me = _instance_id()
    if not _holder_matches(row["holder"], me) and not args.get("force"):
        return {
            "error": "NOT YOUR CHECKOUT",
            "held_by": row["holder"],
            "held_for": _age(row["checked_out_at"]),
            "you_are": me,
            "detail": ("Another instance is working this card right now. If you only wanted to "
                       "record that you looked at it, use task_note -- it writes a comment and "
                       "changes no status, so it cannot release anyone's lock. If you genuinely "
                       "need to take it over, read who holds it and pass force=true."),
        }

    repo = row["repo_path"]
    since = row["head_sha"]
    commits = _git(repo, "log", "--oneline", f"{since}..HEAD") if (repo and since) else None
    stat = _git(repo, "diff", "--stat", f"{since}..HEAD") if (repo and since) else None
    dirty = _git(repo, "status", "--porcelain")
    dirty_files = [l for l in (dirty or "").splitlines() if l.strip()]
    n_commits = len([l for l in (commits or "").splitlines() if l.strip()])

    if status == "done" and not repo and not args.get("reasoning_only"):
        return {
            "error": "REFUSING to mark done: NO REPO RESOLVED for this card.",
            "project": None,
            "what_to_do": ("If this card is code work, its project/title does not "
                           "match PROJECT_REPOS -- fix the map or the card project "
                           "field. If it genuinely produced no code, pass "
                           "reasoning_only=true and say in the summary what the "
                           "evidence IS."),
        }
    prior = args.get("evidence_commits") or []
    if isinstance(prior, str):
        prior = [s.strip() for s in prior.split(",") if s.strip()]
    prior_ok, prior_bad = [], []
    for sha in prior:
        if repo and _git(repo, "cat-file", "-e", f"{sha}^{{commit}}") is not None:
            line = _git(repo, "log", "-1", "--format=%h %ad %s", "--date=short", sha)
            prior_ok.append(line or sha)
        else:
            prior_bad.append(sha)
    if prior_bad:
        return {
            "error": f"REFUSING: {len(prior_bad)} cited commit(s) do not exist in this repo.",
            "bad": prior_bad,
            "repo": repo,
            "what_to_do": "Check the shas. Evidence that cannot be resolved is not evidence.",
        }

    if status == "done" and args.get("reasoning_only") and not args.get("force"):
        _t = db.execute("SELECT title, detail FROM tasks WHERE id = ?", (tid,)).fetchone()
        _text = " ".join([(_t["title"] or ""), (_t["detail"] or "")]) if _t else ""
        _anchors = _named_volumes(_text)
        if _anchors and not any(_volume_reachable(a) for a in _anchors):
            return {
                "error": "REFUSING to close with reasoning_only: THIS INSTANCE CANNOT REACH "
                         "THE FILESYSTEM THIS CARD IS ABOUT.",
                "card_names_volumes": sorted(_anchors),
                "you_are": _instance_id(),
                "what_to_do": (
                    "The card's text names an absolute path on a volume this process cannot "
                    "see, so whatever was done here was done somewhere else -- classically, "
                    "a same-named empty directory on the wrong machine, deleted with every "
                    "step reporting success. If you meant to record an observation, "
                    "use task_note -- it changes no status. If you genuinely did this work "
                    "from a machine that CAN see the volume, run the check-in from there. If "
                    "the path in the card text is stale or incidental, fix the card, then "
                    "close it. force=true overrides, and says on the record that you did."),
            }

    if status == "done" and repo and args.get("reasoning_only"):
        pass
    elif status == "done" and repo:
        if n_commits == 0 and not prior_ok:
            return {
                "error": "REFUSING to mark done: NO COMMITS since check-out.",
                "repo": repo,
                "head_at_checkout": (since or "")[:12],
                "dirty_files": len(dirty_files),
                "what_to_do": ("Commit the work with the card id in the message, then "
                               "check in again. If the work was finished BEFORE this "
                               "checkout, pass evidence_commits=['sha', ...] and they will "
                               "be verified against the repo and written onto the card. If "
                               "it genuinely produced no code, check in with status='todo' "
                               "or 'blocked' and say so in the summary."),
            }
        if dirty_files and not args.get("allow_dirty"):
            return {
                "error": f"REFUSING to mark done: {len(dirty_files)} uncommitted file(s).",
                "dirty": dirty_files[:15],
                "what_to_do": ("Commit them, or pass allow_dirty=true and say in the "
                               "summary why they are staying out."),
            }

    derived = []
    if prior_ok:
        derived.append("PRIOR COMMITS (cited, verified to exist in repo):\n  "
                       + "\n  ".join(prior_ok))
    if repo:
        derived.append(f"REPO {repo} @ {(since or '?')[:12]}..HEAD")
        derived.append(f"COMMITS ({n_commits}):\n{commits or '  (none)'}")
        if stat:
            derived.append(f"DIFF:\n{stat}")
        if dirty_files:
            derived.append(f"STILL UNCOMMITTED ({len(dirty_files)}): "
                           + ", ".join(d[3:] for d in dirty_files[:12]))
    else:
        derived.append("No repo mapped for this project -- reasoning-only card.")

    body = (f"CHECK-IN [{status.upper()}] -- {_age(row['checked_out_at'])} of work.\n\n"
            f"{summary}\n\n"
            f"--- DERIVED FROM GIT (not written by hand) ---\n"
            + "\n\n".join(derived))

    db.execute("INSERT INTO task_comments (task_id, author, kind, body, minutes)"
               " VALUES (?,?,?,?,?)",
               (tid, row["holder"], "work", body, args.get("minutes")))

    if status == "blocked":
        db.execute("UPDATE tasks SET status='blocked', blocked_on=?, updated_at=CURRENT_TIMESTAMP"
                   " WHERE id = ?", (args.get("blocked_on") or "unspecified", tid))
    elif status == "done":
        db.execute("UPDATE tasks SET status='done', done_at=CURRENT_TIMESTAMP,"
                   " updated_at=CURRENT_TIMESTAMP WHERE id = ?", (tid,))
    else:
        db.execute("UPDATE tasks SET status='todo', updated_at=CURRENT_TIMESTAMP"
                   " WHERE id = ?", (tid,))

    db.execute("DELETE FROM card_checkout WHERE task_id = ?", (tid,))
    evidence = "commits since checkout"
    if args.get("reasoning_only"):
        evidence = "NONE — closed reasoning_only"
    elif prior_ok and not n_commits:
        evidence = "prior commits cited and verified"
    elif prior_ok:
        evidence = "commits since checkout + cited prior commits"
    return {
        "checked_in": tid[:8],
        "status": status,
        "commits_recorded": n_commits,
        "commits_recorded_means": "commits made SINCE CHECKOUT only — 0 is normal when the "
                                  "work was committed before the card was claimed",
        "prior_commits_cited": len(prior_ok),
        "evidence": evidence,
        "still_uncommitted": len(dirty_files),
        "board": "unlocked",
    }


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
