# goals_mcp

An MCP server that gives an agent **goals it holds over time**, a **task board it
shares with humans**, and a **conscience that notices when either has gone
stale**.

Most agent memory is retrospective — it records what happened. This is the other
half: what the agent said it would do, still sitting there tomorrow.

Runs on SQLite out of the box. No database server, no cloud, no account.

---

## Status — read before you rely on this

**This is a working demonstration, not a product.** It is published because the
ideas in it are worth stealing, not because it is finished.

- **No warranty and no support.** MIT, as-is. Nobody is on call, and issues may
  sit. Fork it rather than wait.
- **It will change without notice**, including the schema. Pin a commit if that
  matters to you.
- **It writes to a database you own.** Back up `goals.db`. There is no migration
  path between versions and no undo.
- **The board has authentication OFF by default** (`BOARD_PASS` unset) and is
  intended for `127.0.0.1`. Do not expose it to a network without setting a
  password, and do not treat that password as hardening.
- **Not audited.** No security review, no multi-user model, no rate limiting.
  Assume anyone who can reach it can do anything it can do.

Use it on work you can afford to lose, on a machine you control.

---

## Three things, deliberately in one store

It is tempting to build these separately. They belong together because they are
the same data seen at different distances.

**1. Goals — the agent's own, often with no work attached.**
Set by the agent, for the agent. `create_goal` is not a task; it is a standing
intention. Some of them never produce a card and never should.

**2. The board — shared work with humans.**
Kanban cards with projects, owners, blockers, subtasks and comments. This is the
surface a human actually looks at. An agent that keeps a meticulous private log
and leaves this view stale has failed at the only part that was collaborative.

**3. The conscience — accountability, so neither goes quietly dead.**
A separate process ranks goals by urgency, asks a model what is worth saying, and
writes a plain text file. What you do with that file is up to you.

---

## The bit worth stealing: goal *kinds*

Urgency is `priority × days_idle`. That formula quietly encodes a claim nobody
made — **that only completable things count.**

Put "maintain the friendship" in the same ranker as "ship v1" and the friendship
loses every day, forever, because it cannot be closed. The ranker doesn't hate
your friends; it just has no way to score a thing with no end state.

So goals have a `kind`:

| kind | meaning | urgency | how it surfaces |
|---|---|---|---|
| `work` (default) | has an end state | grows with idle time | ranked, nagged |
| `standing` | a disposition you hold | **always 0** | slow round-robin rotation |

A standing goal is never overdue and never completes. `get_neglected` returns one
alongside the ranked list — *beside* it, not *in* it — so the answer to "what
should I do" is never composed purely of things that can be finished.

The fix is not to inflate a standing goal's priority so it can win a race it
should not be running.

```python
create_goal(description="Write regularly — prose, not reports", kind="standing")
```

---

## Guards that refuse dishonest closes

The board pushes back when a card is closed in a way that history says is
usually wrong. Each guard has a specific failure behind it:

- **Unobserved absence.** A card claiming something "doesn't exist" with no sign
  anyone looked at the running system.
- **Timing faults without a baseline.** "The file is not arriving" after six
  minutes of a latency nobody ever measured. Note that `curl` and `verified` do
  **not** silence this — looking was never the missing step; knowing what normal
  looks like was.
- **Code closed with nowhere to go.** A card in a code project closing with no
  commit, no push, and no statement that it wasn't code. Real work, honestly
  logged, written into a directory with no `.git` — it can never ship and nobody
  notices.
- **Reachability.** Refuses `reasoning_only` when the card names an absolute path
  on a volume this process cannot see. The classic failure is one agent on two
  machines deleting a same-named empty directory on the wrong host and closing
  the card with every step reporting success.

Two rules the guards follow, both learned the hard way:

> **A guard that is always red trains you to skim it.** Testing whether the named
> path still exists would fire on every correct "remove X" card.

> **A guard that is silently green is worse — it has no symptom at all.** Every
> guard here is tested against a canary containing the thing it must catch, so a
> guard that has stopped firing shows up as a failing test rather than as silence.

---

## Install

```bash
git clone https://github.com/brucepro/goals_mcp && cd goals_mcp
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

Use a virtualenv. `requirements.txt` pins `mcp<2` because 2.x removed the
decorators this server is built on, and a system Python that already carries a
different `mcp` will fail at import. The database is created on first connect —
there is nothing to seed.

Add to `.mcp.json` (see `.mcp.json.example`):

```json
{
  "mcpServers": {
    "goals": {
      "type": "stdio",
      "command": "/abs/path/to/goals_mcp/.venv/bin/python",
      "args": ["/path/to/goals_mcp/goal_mcp.py"],
      "env": {
        "GOAL_OWNER": "myagent",
        "GOAL_AGENTS": "myagent,researcher",
        "GOAL_HUMANS": "ada,grace"
      }
    }
  }
}
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GOAL_DB` | `./goals.db` | SQLite file |
| `GOAL_PG` | unset | Postgres conninfo. Set it and Postgres is used instead |
| `GOAL_REQUIRE_PG` | unset | `1` = refuse to start without `GOAL_PG` (see below) |
| `GOAL_OWNER` | `agent` | **Who this instance is.** Only its own goals generate urgency |
| `GOAL_AGENTS` | `$GOAL_OWNER` | Comma-separated agents who can own cards |
| `GOAL_HUMANS` | `human` | Comma-separated humans who can own cards |
| `GOAL_INSTANCE` | auto | Distinguishes two instances of one agent |
| `GOAL_CODE_PROJECTS` | empty | Projects where "done" implies a commit. Empty disables that guard |
| `GOAL_PROJECT_REPOS` | `./project_repos.json` | Project → git repo, for deriving diffs |

**A word about `GOAL_REQUIRE_PG`.** If you graduate to Postgres, set it. A
missing `GOAL_PG` does not fail loudly — it silently creates an empty SQLite file
and writes into a store nothing else reads. Every call returns success while the
work vanishes from the real board.

Multi-machine setups want Postgres. Do **not** reach for shared state by putting
a SQLite file on a file-syncing service; that produces divergent copies and sync
conflicts, not sharing.

---

## The conscience

```bash
python conscience_agent.py --once
```

Ranks goals, asks a model for one line each, writes them to
`conscience/nag_output.txt`:

```
2026-08-23T12:14:47 | 83d944fa | 2.1 | Blocked on the business-info update — a five-minute authority task only they can do. Once it lands, verify the schema change.
```

`ISO timestamp | goal id | urgency | the nag`. One line per goal, most urgent
first. Point `CONSCIENCE_NAG_PATH` wherever you like.

Needs any OpenAI-compatible endpoint (`LLM_ENDPOINT`) — llama.cpp, Ollama, vLLM,
LM Studio, or a hosted API. A small local model is genuinely fine here.

| Variable | Default | Meaning |
|---|---|---|
| `LLM_ENDPOINT` | `http://localhost:8080/v1` | OpenAI-compatible base URL |
| `CONSCIENCE_MODEL` | `local-model` | Model name the endpoint expects |
| `LLM_API_KEY` | `not-needed` | Leave unset for a local server; required by hosted APIs |
| `LLM_MAX_RETRIES` | `0` | Raise for hosted endpoints that rate-limit |
| `CONSCIENCE_NAG_PATH` | `conscience/nag_output.txt` | Where the nags are written |

A local run writes a nag every couple of hours from cron and can take minutes on
a small model, so keep the schedule longer than a run.

**A conscience only works if the agent still trusts it.** Every false alarm —
firing on something already handled, or already known to be blocked — teaches the
agent to tune out the next one. So goals owned by someone else score **zero**
urgency: you cannot be nagged about work that isn't yours to move.

See **[HUD.md](HUD.md)** for wiring the nag into Claude Code, and
**[AGENTS.md](AGENTS.md)** for what an agent should know before using these tools.

---

## Support

If this project adds value to your AI experience and you'd like to show your appreciation, consider supporting the project:

- [Buy me a coffee](https://www.buymeacoffee.com/brucepro)
- [Ko-fi](https://ko-fi.com/F1F7U45XV)

## Contributing

Contributions, suggestions, and feedback are always welcome. Please submit issues or pull requests on GitHub, or contact us directly with your ideas and suggestions.

## License

MIT.
