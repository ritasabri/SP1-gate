# Proof of concept: questions whose answers are computed

A throwaway script that checks the central claim of `docs/QUESTION_ENGINE.md` on real code. It isn't the architecture: there are no hook adapters, no gate page and no learner model, and it only handles Python.

```bash
python3 poc/try_it.py                  # the interactive gate, in your browser
python3 poc/engine_poc.py              # all four examples, printed; about 10 seconds
python3 poc/engine_poc.py find_index   # one example
```

`try_it.py` plays four reviews as if an agent had just proposed each file: you answer the questions, then press **1** to approve or **2** to send the code back with a reason. Answer keys stay in the server process, the page is served on 127.0.0.1 with a per-session token, and only first tries are scored, matching `docs/DESIGN.md`. No agent is connected: that's build phases 2–3.

It prints each example's code and questions, and writes `poc/out/<example>.json`. It needs Python 3.9 or later and no packages. `fix_ticket.py` imports `anthropic`, but that example is only analyzed, never run.

Timings differ a little from run to run; `out.txt` is the run that `docs/QUESTION_ENGINE.md` quotes. If a busy computer keeps the timing from confirming a running-time rule after three tries, those questions are skipped with a note rather than asked on shaky evidence.

> **Not a sandbox.** The script runs code with your permissions, in a separate process with a timeout and a temporary working folder. That limits accidents, not attacks. Run it on the included examples only. The real engine must run every probe inside an OS sandbox (`docs/DESIGN.md`, principle 4).

| Example | What the agent wrote | Questions | How the key is computed |
|---|---|---|---|
| `first_repeat` | First repeated email, using a list for `seen` | running time; list vs set | Loop and membership rules, with the degree checked by timing (measured exponent ≈ 2) |
| `find_index` | Binary search | trace a call; running time | Running the real code; the halving-loop rule |
| `profile_cache` | A "cache" created inside the function | what happens to `cache`; is the agent's claim true | Scope rule; running the code with a call counter |
| `fix_ticket` | A model's reply run with `shell=True` | what the ticket text can do; which fix works | Taint analysis on the code and on each proposed fix |

## Where wrong options come from

- **Executed mutants.** The same code with one realistic bug, run on the same input: a flipped comparison (`<=` → `<`), a dropped `+ 1`, a missing `if` check, a set swapped for a list, or a variable moved out of the function.
  - A mutant whose output matches the real code is thrown away.
  - Outputs that change across eight hash seeds are thrown away.
  - Results containing a set are thrown away.
  - A mutant that merely times out is dropped, because a timeout proves nothing.
  - "Nothing: the loop never ends" is used only when a `while` loop provably cycles: the loop makes no calls, and the same line runs with the same variable values twice. In deterministic code, a repeated state proves it.
- **A misconception catalog.** Documented wrong beliefs, such as "one loop means linear time" or "a dict inside a function lasts between calls". Each comes with its feedback text. Where it can, the engine checks them against the code too. For example, "sets would be slower" is measured and shown wrong for lists of 1,000 to 8,000 emails.
