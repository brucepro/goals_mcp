#!/usr/bin/env python3
"""Print the current nag. Anything on stdout lands in the agent's context.

Wire into Claude Code as a UserPromptSubmit hook or a statusLine command --
see HUD.md. Runs on every prompt, so it stays deliberately small.
"""
import datetime
import os
import pathlib

NAG = pathlib.Path(os.environ.get(
    "CONSCIENCE_NAG_PATH",
    pathlib.Path(__file__).parent / "conscience" / "nag_output.txt"))

STALE_HOURS = 2.0

DEAD_HOURS = 24.0


def main():
    if not NAG.exists():
        print("[nag] no conscience output yet — run: python conscience_agent.py --once")
        return

    lines = [l for l in NAG.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        print("[nag] conscience ran and had nothing urgent to say.")
        return

    try:
        ts, gid, urg, msg = lines[0].split(" | ", 3)
        age_h = (datetime.datetime.now()
                 - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except ValueError:
        print("[nag] %s" % lines[0][:200])
        return

    if age_h > DEAD_HOURS:
        print("[nag] CONSCIENCE IS %.0fh STALE — it is not running. "
              "Last thing it said: %s" % (age_h, msg))
    elif age_h > STALE_HOURS:
        print("[nag] %s (urgency %s) %.0fh old: %s" % (gid, urg, age_h, msg))
    else:
        print("[nag] %s (urgency %s): %s" % (gid, urg, msg))


if __name__ == "__main__":
    main()
