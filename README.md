# SP1 Gate (live, version 1)

SP1 Gate plugs into Claude Code. While you work with the agent, it asks multiple-choice
questions at two moments:

1. **Before a risky action.** Before Claude installs, downloads, deletes, changes
   permissions, pushes, or edits a secrets or settings file, you answer 1–2 questions
   about what that action will do. Then Claude Code asks you to press 1, as usual.
2. **After Claude writes code.** When Claude finishes its turn, 1–3 questions about the code
   it just wrote appear, covering data structures, running time, logic, files and
   security. Your next message to Claude waits until you've answered them.

The questions open in a page in your browser. Answers are computed from the code, not
written by an AI, and nothing leaves your computer. No API key is needed: SP1 Gate uses
the Claude Code you already have.

## Try it

You need Python 3.9 or later (macOS already has `python3`) and Claude Code. Claude Code needs a
Pro, Max, Team, Enterprise or Console account; setup is at https://code.claude.com/docs/en/setup.

```bash
cd ~/Downloads/sp1-gate-live
python3 gate/sp1.py install practice        # turn the gate on for the practice folder
cd practice
claude                                      # start Claude Code there
```

Then type the prompts in `practice/TRY_THESE.md`. When questions come up, your browser
opens the gate page. Keep that tab open.

To use it on a real project, run `python3 gate/sp1.py install /path/to/project`. Don't move
the `sp1-gate-live` folder afterwards: the hooks point to it. To move it, uninstall, move
the folder, then install again.

## Commands

| Command | What it does |
|---|---|
| `python3 gate/sp1.py install FOLDER` | Turns the gate on for FOLDER (writes `FOLDER/.claude/settings.local.json`) |
| `python3 gate/sp1.py uninstall FOLDER` | Turns it off again; your other settings stay |
| `python3 gate/sp1.py open` | Opens the gate page |
| `python3 gate/sp1.py status` | Shows whether the gate is running |
| `python3 gate/sp1.py stop` | Stops the gate's background server |
| `python3 gate/sp1.py set after_answers allow` | Skips the "press 1" step after correct answers (`ask` restores it) |

## What's in here

| Path | What it is |
|---|---|
| `gate/` | The live gate: `hook.py` (Claude Code hooks), `daemon.py` (local server), `page.html` (the page), `actions.py` (questions before actions), `code_questions.py` and `taint.py` (questions about code) |
| `practice/` | A scratch project to try it in |
| `tests/` | 93 tests (`python3 -m unittest discover -s tests`) |
| `docs/` | The design (`DESIGN.md`) and how questions are made (`QUESTION_ENGINE.md`) |
| `poc/` | The earlier proof of concept, and `try_it.py`, a demo that needs no agent |

## Tested, and not yet tested

Tested:
- The whole flow with real Claude Code 2.1.286 sessions (run without a person at the keyboard). A `git init`, a new `.env` file, and a `git add` that included `.env` were each held until their questions were answered. After Claude wrote code, the questions opened, the next message was blocked, and it went through once the questions were answered.
- The page in Chromium.
- All tests pass on Python 3.9 and 3.11.

Not yet seen by a person: the "press 1" prompt that Claude Code shows after you answer. That's the documented behavior of the hook's `ask` answer, but no one has watched it happen yet. If it misbehaves, run `python3 gate/sp1.py set after_answers allow`.

## Limits of version 1

- **Claude Code only.** Codex, Cursor, VS Code Copilot and Gemini CLI come next; each needs its own small adapter.
- **Code questions are about Python.** For other languages, a new file gets one question about what kind of file it is.
- **It never runs the agent's code.** It asks only what reading the code can prove. Questions that need running the code wait for an OS sandbox (`docs/DESIGN.md`, principle 4).
- **It's a learning tool, not a security boundary.** Anyone at the keyboard can uninstall it, and the agent runs as you.
