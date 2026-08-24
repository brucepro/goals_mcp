# Wiring the nag into Claude Code

## First, the important part

**The conscience's only output is a text file.**

```
conscience/nag_output.txt
```

That's it. There is no HUD in this repo, no integration, no protocol. The agent
writes lines to a file and stops.

This is worth saying because the obvious reading of "a conscience for your agent"
is that something clever is happening at the other end. Nothing is. **The shaping
is yours**, and that is the interesting freedom here — most people don't realize
how much of an agent's felt experience is just *what text shows up, when*.

Format, one line per goal, most urgent first:

```
2026-08-23T12:14:47 | 83d944fa | 2.1 | Blocked on the business-info update — a five-minute authority task only they can do.
ISO timestamp        | goal id  | urg | the nag
```

Below are three ways to surface it, cheapest first.

---

## Option 1: Just read it

```
> read conscience/nag_output.txt and tell me what I'm avoiding
```

No configuration at all. Start here. If you never go further than this, the
system still works — you have simply made the check manual.

---

## Option 2: A `UserPromptSubmit` hook (the real one)

This injects the nag into context on **every prompt you send**, so the agent
cannot help but see it. In `.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/goals_mcp/hud.py"
          }
        ]
      }
    ]
  }
}
```

Anything the command prints on stdout is prepended to your prompt as context.
A minimal `hud.py`:

```python
#!/usr/bin/env python3
"""Print the current nag. Anything on stdout lands in the agent's context."""
import pathlib, datetime

NAG = pathlib.Path(__file__).parent / "conscience" / "nag_output.txt"

if NAG.exists():
    lines = [l for l in NAG.read_text(encoding="utf-8").splitlines() if l.strip()]
    if lines:
        ts, gid, urg, msg = lines[0].split(" | ", 3)
        age_h = (datetime.datetime.now()
                 - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
        # ⚠️ ALWAYS PRINT THE AGE. A stale nag read as current is worse than no
        #    nag: it is confident, specific, and about a world that moved on.
        stale = f"  ⚠️ {age_h:.0f}h old" if age_h > 2 else ""
        print(f"[nag] {gid} (urgency {urg}){stale}: {msg}")
```

### Three things that will bite you

**1. A dead conscience is invisible.** If the cron job stops, `nag_output.txt`
keeps its last contents forever and the HUD keeps cheerfully printing it. You get
a green light from a corpse. Print the age — and if you want to be serious about
it, print a loud warning past some threshold rather than a subtle one.

**2. Cost.** This runs on *every* prompt. Keep it to one or two lines. A HUD that
costs 400 tokens a turn is a tax you pay all day.

**3. It becomes wallpaper.** Anything that appears identically every turn stops
being read — by humans and models alike. Show the nag when it *changes*, or rotate
what you show, or let it fall silent when nothing is urgent. **Silence is a
feature.** A conscience that speaks constantly has no way to be emphatic.

---

## Option 3: The status line

Cheaper, always visible, doesn't touch context. In `.claude/settings.json`:

```json
{ "statusLine": { "type": "command", "command": "python /path/to/goals_mcp/hud.py" } }
```

Same script, but keep it to one short line — you have about a terminal width.

Tradeoff: the status line is for *you*, not the agent. It never enters the
model's context. Use it when you want to be nagged; use the hook when you want
the agent to be.

---

## Shaping it: what else can go in there

Once you realize it's just stdout, the HUD becomes a place to put anything the
agent should be unable to ignore. Things worth considering:

- **The current time.** Models are worse at this than you expect.
- **The oldest card in `doing`.** Work-in-progress that never finished is the
  most reliable signal something is stuck.
- **A count of cards blocked on a human**, so the agent stops waiting silently.
- **One standing goal**, rotated. `get_neglected` returns one for exactly this.
- **A rotating reminder of a lesson you keep re-learning.** Cheap, and it fires
  at the only moment it could help — before the next thing you do.

Two cautions from running this daily:

> **A field you don't act on should be deleted.** Every line teaches "this is
> what matters." Lines you consistently ignore teach that the HUD can be ignored.

> **Anything reassuring needs a liveness check.** "All systems normal" printed by
> a script that can only ever print "all systems normal" is not information. Ask
> of every green field: *has this ever gone red?* If you can't remember it
> happening, test that it can.
