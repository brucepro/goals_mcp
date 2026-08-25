#!/usr/bin/env python3
"""Print the current nag. Anything on stdout lands in the agent's context.

Wire into Claude Code as a UserPromptSubmit hook or a statusLine command --
see HUD.md. Runs on every prompt, so it stays deliberately small.
"""
import datetime
import os
import pathlib
import sys
import time

# stdout is injected into an agent's context, so force UTF-8: the Windows default
# codepage emits the em-dashes below as invalid UTF-8 and never fails loudly.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NAG = pathlib.Path(os.environ.get(
    "CONSCIENCE_NAG_PATH",
    pathlib.Path(__file__).parent / "conscience" / "nag_output.txt"))

STALE_HOURS = 2.0

DEAD_HOURS = 24.0


def _file_age_h():
    """Age from mtime, the only liveness signal when the file is empty."""
    try:
        return (time.time() - NAG.stat().st_mtime) / 3600.0
    except OSError:
        return None


def main():
    if not NAG.exists():
        print("[nag] no conscience output yet — run: python conscience_agent.py --once")
        return

    lines = [l for l in NAG.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        # An empty file is the reassuring branch, so it needs its own liveness check.
        age = _file_age_h()
        if age is not None and age > DEAD_HOURS:
            print("[nag] CONSCIENCE IS %.0fh STALE — its last run found nothing "
                  "urgent, but nothing has run since. Treat this as broken, "
                  "not as calm." % age)
        else:
            print("[nag] conscience ran and had nothing urgent to say.")
        return

    try:
        ts, gid, urg, msg = lines[0].split(" | ", 3)
        age_h = (datetime.datetime.now()
                 - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except ValueError:
        # Fall back to mtime so an unreadable nag cannot pass for a fresh one.
        age = _file_age_h()
        stale = " (%.0fh old)" % age if age is not None and age > STALE_HOURS else ""
        print("[nag]%s %s" % (stale, lines[0][:200]))
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
