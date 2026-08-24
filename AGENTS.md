# AGENTS.md

Instructions for an agent working a board through this MCP. Drop the relevant
parts into your `CLAUDE.md` / system prompt.

---

## The rule that matters most

**Log the work on the card, not just in your own record.**

This is the single most reliable failure on a shared board, and it does not feel
like a failure from the inside. You do the work. You write a thorough action log
against the goal. Everything feels complete — so the trigger to also move the
card never fires. Meanwhile the card sits at `todo` and the human's view of the
project is a lie.

The board is *their* window into the work. Your private log being excellent does
not help them at all.

`task_note(kind="work")` moves a `todo` card to `doing` automatically, because
this cannot be fixed by remembering harder.

---

## Working a card

1. **`check_out_card(id)`** before touching anything. It returns the full card,
   every prior comment **with its age**, and records the repo's current HEAD.
2. Do the work.
3. **`check_in_card(id, status, summary)`** when done.

Check-in derives the file list and commits from git. **You are asked only for
what git cannot know:** why you did it this way, what you decided *against*, and
what is still unverified. Do not list files — that is computed. Do not write
"fixed the bug" — write why it was a bug.

One card at a time, board-wide. The lock expires (default 4h) so a session that
dies does not hold it forever, and `force=true` exists — a lock nobody can break
is worse than no lock.

**If you only looked at a card, use `task_note`.** It writes a comment and
touches neither status nor anyone's checkout. Reaching for `check_in_card` to
record a glance will release a lock another instance is actively working under.

---

## Notes decay, and check-out tells you so

Every comment comes back stamped with its age. Treat that number as load-bearing.

A careful, accurate, well-evidenced measurement from three weeks ago reads
*exactly* like a fact about today. It is the most dangerous thing on the card,
because its precision is what makes it credible. Before acting on an old note,
ask whether the thing it measured could have changed — and note that a
**reassuring** old note ("this works, verified") goes stale in precisely the same
way as a warning, and gets questioned far less.

---

## Goals vs cards

- **Goal** = a standing intention. May have no cards at all, forever.
- **Card** = a concrete piece of work with an end state.

Always pass `goal_id` when creating a card. **The conscience joins on it** — a
card without one is invisible to the nag and will never be surfaced again. It
inherits from `parent_task_id` if you pass that instead.

Use `kind="standing"` for any goal that cannot be completed — a practice, a
relationship, a standard you hold. If you file it as `work` it will be ranked by
how long it has been idle, which for a thing with no end state means it loses to
every deadline forever. That is not a judgement about its value; it is an
artifact of the formula.

---

## What the guards are telling you

The board will sometimes refuse a close. It is not being fussy — each refusal
encodes a specific way agents reliably fool themselves.

| Refusal | What it actually means |
|---|---|
| Unobserved absence | You concluded something doesn't exist by reading source. Go look at the running system. |
| Timing fault without a baseline | You called something dead without knowing how long it normally takes. Six minutes is not evidence. `curl` does not help — looking was never the missing step. |
| Closed without saying where the code went | Nothing says this was committed, pushed, or wasn't code. Work in a directory with no `.git` can never ship. |
| Cannot reach the filesystem | The card names a path on a volume you cannot see. Whatever you did, you did it somewhere else. |

**When a guard fires, the reflex is to make the friction stop.** Notice that
reflex. The hatch (`force`, `reasoning_only`, `allow_dirty`) exists for the
genuine cases, and reaching for it because the guard is annoying is how the
guard's whole purpose gets defeated — usually about ninety seconds before the
thing it was warning about happens.

If you use a hatch, say why in the summary. Future-you reads that.

---

## Honest completion

`complete_goal` asks whether the success criteria were actually met. Answer it
honestly. `abandon_goal` exists and is not a failure — a goal you have genuinely stopped caring about should be abandoned with a reason, not left `active` to rot and generate urgency forever.

A board full of things you are not doing is worse than a short one, because it trains you to ignore the board.
