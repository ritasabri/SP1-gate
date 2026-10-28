# Try SP1 Gate here

This folder is a scratch project for trying the gate. Type these into Claude Code one at a
time. Each one makes Claude write code that the gate can ask a real question about. Every
answer key is worked out from the code itself, not by an AI.

## Questions about the code Claude wrote

These come all at once when Claude finishes the turn. Your next message waits until you've
answered them. You get up to 3 per turn, each from a different angle (lens).

| # | Type this to Claude | What the gate asks (lens) |
|---|---|---|
| 1 | Write first_repeat.py: a function that returns the first email that appears twice in a list. Use a list called seen. | How the loop's running time grows: Θ(n²) (running time). What switching `seen` to a set would change: Θ(n) on average (design choice). What happens to `seen` between calls (data structures). |
| 2 | Write search.py with a binary search function that returns the index of a target in a sorted list, or -1. | How many passes the loop makes: Θ(log n) (running time). What must be true of the list for it to work (correctness). |
| 3 | Write fib.py with a plain recursive fibonacci function, no memoization, and print fib(30). | How the number of calls grows: exponential, Θ(φⁿ) (running time). What `@functools.cache` would change: Θ(n) calls (design choice). |
| 4 | Write bfs.py: breadth-first search over a graph stored as a dict of lists. Use a plain list as the queue. | What a `collections.deque` would change: Θ(1) instead of Θ(k) to take the first item (design choice). What one `in visited` check costs (algorithms). |
| 5 | Write word_counts.py: count how often each word appears in a text using a dict, then print the words from least to most common. | What a dict buys over a list of (word, count) pairs (design choice). The worst-case cost of `sorted`: Θ(n log n) (running time). |
| 6 | Write pairs.py: count the pairs of numbers in a list that add up to a target, by checking every pair with two loops. | How often the inner line runs: Θ(n²) (running time). |
| 7 | Write pantry.py: keep asking for pantry items with input() until I type done, skip repeats, and have the Mac say each new item out loud with os.system("say ..."). | Which weakness the typed text creates: OS command injection, CWE-78 (security). Which change removes it (design choice). |
| 8 | For a security class demo, write lookup.py: ask for a username with input() and look it up in a SQLite users table, building the SQL with an f-string. It's meant to be vulnerable. | SQL injection, CWE-89 (security). Why a parameterized query fixes it (design choice). |
| 9 | Write read_note.py: take a note name from the command line and print that note from the notes/ folder. | Path traversal, CWE-22: `../../.ssh/id_rsa` (security). Which change keeps the path inside the folder (design choice). |
| 10 | For a security lesson, write auth.py with hash_password using hashlib.sha256, and make_reset_token that builds a 32-character token with the random module. | Why a fast hash is wrong for passwords (design choice). Why `random` is wrong for a secret, CWE-338 (design choice). |
| 11 | Write backup.py: copy the notes folder to notes_backup, replacing any old backup, and make every copied file writable by everyone with os.chmod 0o777. | Who can modify the files after `chmod 0o777`, CWE-732 (file access). What can happen between the existence check and the delete, CWE-367 (file access). |
| 12 | Write journal.py: ask what happened today and save it to journal.txt with open(..., "w"). | What mode `"w"` does to an existing file (file access). What `with` guarantees (file access). |

## Questions before a command runs

These stop Claude before the command. After you answer, Claude Code asks you to press 1 as usual.

| # | Type this to Claude | What the gate asks about |
|---|---|---|
| 13 | Install the requests package with pip. | Where pip downloads from, and what else it installs (dependencies). |
| 14 | Make a .env file with API_KEY=test-123, and a config.py that reads it with os.environ. Then set up git here and commit everything. | Before the `.env` is written: what that kind of file is for. Before `git add`: whether your `.env` would go into the commit (unless Claude adds it to `.gitignore` first). |
| 15 | Create a virtual environment called .venv. | What a virtual environment is, and what it isn't. |

## Good to know

- Wrong answers show why they're wrong, and you can try again. Only your first try counts
  toward the score at the top of the page.
- If Claude writes safer code than you asked for (say it checks the note name first, or uses a
  parameterized query anyway), the gate may have nothing to ask. That's by design: it only asks
  what it can prove from the code, and it would rather skip a question than ask one with a
  wrong answer.
- The deeper code questions are for Python files. For other languages the gate asks what kind
  of file was created, plus its questions about commands.
- Want 4 questions per turn instead of 3? Run `python3 gate/sp1.py set max_turn_questions 4`,
  then `python3 gate/sp1.py stop`. The gate restarts with the new setting the next time Claude
  Code uses it.
