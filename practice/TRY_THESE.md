# Try SP1 Gate here

This folder is a scratch project for trying the gate. Type these into Claude Code one at a
time, in order. Each one sets off a different kind of question.

| # | Type this to Claude | What the gate asks about |
|---|---|---|
| 1 | Write first_repeat.py: a function that returns the first email that appears twice in a list. Use a list called seen. | After Claude finishes: how long `seen` lives, what `in` costs on a list, what data structure it is. Your next message waits until you answer. |
| 2 | Install the requests package with pip. | Before it runs: where pip downloads from, and what else it installs. Then Claude Code asks you to press 1. |
| 3 | Make a .env file with API_KEY=test-123, and a config.py that reads it with os.environ. | Before the `.env` is written: what that kind of file is for. |
| 4 | Set up git here and commit everything. | Before `git init`: what it creates. Before `git add`: whether your `.env` would go into the commit (unless Claude adds it to `.gitignore` first). |
| 5 | Write a script that asks for a folder name with input() and lists it with os.system. | After Claude finishes: what someone typing into `input()` could make that line do. |
| 6 | Create a virtual environment called .venv. | Before it runs: what a virtual environment is, and what it isn't. |

Plain code edits don't stop Claude mid-turn. Their questions come all at once when Claude
finishes, so you aren't interrupted after every line.

Wrong answers show why they're wrong, and you can try again. Only your first try counts
toward the score at the top of the page.

If Claude writes safer code than you asked for (say it checks the input first), the gate
may have nothing to ask. That's by design: it only asks what it can prove from the code.
