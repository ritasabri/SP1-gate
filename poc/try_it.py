"""Try SP1 Gate: an interactive demo of the gate, in your browser.

    python3 poc/try_it.py

It computes the questions for the four examples (about 10 seconds), then opens
a local page that plays four reviews, as if an agent had just proposed each
file. Answer the questions, then press 1 to approve or 2 to send back.

Faithful to the design in docs/DESIGN.md:
- Answer keys stay in this process's memory. The page only ever learns
  "correct" or "not correct, because..." for the option you picked.
- The page is served on 127.0.0.1 only, with a per-session token in the URL.
- First tries are scored; retries let you finish but don't count.

Not yet the real thing: nothing here talks to a coding agent. The hook
adapters that pause Claude Code or Codex are build phases 2-3.
"""
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_poc  # noqa: E402  (the same engine, unchanged)

# What the pretend agent says about each file, shown above the code.
AGENT_SAYS = {
    "first_repeat": "I added first_repeat() so signup can tell people their email is already taken.",
    "find_index": "I wrote a binary search so we can look up an ID in the sorted list fast.",
    "profile_cache": "I added a cache, so repeated profile lookups skip the slow fetch.",
    "fix_ticket": "I wired ticket triage to the model: it suggests a shell command for the ticket and runs it.",
}


def build_examples():
    examples = []
    for name, func in engine_poc.EXAMPLE_FUNCS.items():
        print(f"  computing questions for {name}.py ...")
        source, questions = func()
        examples.append({
            "name": name,
            "file": f"examples/{name}.py",
            "agent_says": AGENT_SAYS.get(name, ""),
            "code": source,
            "questions": questions,
        })
    return examples


# ---------------------------------------------------------------- the server

class Gate:
    """Holds the full questions (with keys) and the score. The page never sees keys."""

    def __init__(self, examples):
        self.examples = examples
        self.first_try = {}   # qid -> True/False for the first answer only
        self.decisions = []   # (example name, "approved" | "sent back", reason)

    def public_state(self):
        """What the page is allowed to know: no keys, no feedback texts."""
        return {
            "examples": [
                {
                    "name": ex["name"],
                    "file": ex["file"],
                    "agent_says": ex["agent_says"],
                    "code": ex["code"],
                    "questions": [
                        {
                            "id": q["id"],
                            "lens": q["lens"],
                            "stem": q["stem"],
                            "options": [{"letter": o["letter"], "text": o["text"]}
                                        for o in q["options"]],
                        }
                        for q in ex["questions"]
                    ],
                }
                for ex in self.examples
            ],
        }

    def answer(self, qid, letter):
        for ex in self.examples:
            for q in ex["questions"]:
                if q["id"] == qid:
                    correct = (letter == q["answer"])
                    if qid not in self.first_try:
                        self.first_try[qid] = correct
                    if correct:
                        return {"correct": True, "explain": q["explain"]}
                    picked = next(o for o in q["options"] if o["letter"] == letter)
                    return {"correct": False, "why": picked["why"] or ""}
        return {"error": "unknown question"}

    def decide(self, name, decision, reason):
        self.decisions.append((name, decision, reason))
        total = len(self.first_try)
        right = sum(1 for v in self.first_try.values() if v)
        return {"first_try_right": right, "first_try_total": total}


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SP1 Gate — demo</title>
<style>
  :root {
    --bg: #0b1220; --panel: #111b2e; --panel2: #0e1626; --line: #22314e;
    --text: #dbe4f5; --dim: #8294b5; --cobalt: #4f7cff; --cobalt2: #7ea0ff;
    --good: #35c98e; --bad: #ff6b7a; --warn: #ffc857;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.55 "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
  header { padding: 14px 22px; border-bottom: 1px solid var(--line);
           display: flex; justify-content: space-between; align-items: baseline; }
  header .brand { color: var(--cobalt2); font-weight: 700; letter-spacing: .06em; }
  header .step { color: var(--dim); font-size: 13px; }
  main { display: grid; grid-template-columns: minmax(340px, 46%) 1fr;
         gap: 18px; padding: 18px 22px 90px; max-width: 1280px; margin: 0 auto; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
  .panel h2 { margin: 0; padding: 10px 14px; font-size: 12px; letter-spacing: .12em;
              text-transform: uppercase; color: var(--dim);
              border-bottom: 1px solid var(--line); font-weight: 600; }
  .agent-says { padding: 12px 14px; border-bottom: 1px solid var(--line);
                color: var(--text); }
  .agent-says .who { color: var(--warn); }
  .filename { padding: 8px 14px; color: var(--dim); font-size: 13px;
              border-bottom: 1px solid var(--line); }
  pre.code { margin: 0; padding: 12px 8px; overflow-x: auto; background: var(--panel2);
             border-radius: 0 0 10px 10px; font-size: 13.5px; }
  pre.code .ln { display: inline-block; width: 34px; text-align: right;
                 padding-right: 10px; color: #44557a; user-select: none; }
  .qbody { padding: 14px; }
  .lens { display: inline-block; font-size: 11px; letter-spacing: .1em;
          text-transform: uppercase; color: var(--cobalt2);
          border: 1px solid var(--line); border-radius: 20px;
          padding: 2px 10px; margin-bottom: 10px; }
  .stem { margin: 0 0 14px; font-weight: 600; }
  button.opt { display: block; width: 100%; text-align: left; margin: 8px 0;
               padding: 10px 12px; border-radius: 8px; cursor: pointer;
               background: var(--panel2); border: 1px solid var(--line);
               color: var(--text); font: inherit; }
  button.opt:hover:not(:disabled) { border-color: var(--cobalt); }
  button.opt:disabled { cursor: default; }
  button.opt .letter { color: var(--cobalt2); font-weight: 700; margin-right: 8px; }
  button.opt.wrong { border-color: var(--bad); color: var(--dim);
                     text-decoration: line-through; }
  button.opt.right { border-color: var(--good); }
  .feedback { margin: 4px 0 10px; padding: 10px 12px; border-radius: 8px;
              font-size: 13.5px; border: 1px dashed var(--bad); color: var(--text); }
  .feedback.ok { border: 1px dashed var(--good); }
  .feedback .tag { font-weight: 700; }
  .feedback.ok .tag { color: var(--good); }
  .feedback:not(.ok) .tag { color: var(--bad); }
  .next { margin-top: 12px; background: var(--cobalt); border: none; color: #fff;
          padding: 10px 16px; border-radius: 8px; font: inherit; font-weight: 700;
          cursor: pointer; }
  .gatebar { position: fixed; left: 0; right: 0; bottom: 0;
             background: #0d1528ee; border-top: 1px solid var(--line);
             padding: 12px 22px; display: flex; gap: 14px; align-items: center;
             justify-content: center; backdrop-filter: blur(4px); }
  .gatebar .hint { color: var(--dim); font-size: 13px; }
  .gatebar button { font: inherit; font-weight: 700; padding: 10px 18px;
                    border-radius: 8px; cursor: pointer; border: 1px solid var(--line);
                    background: var(--panel); color: var(--dim); }
  .gatebar button.ready#approve { background: var(--good); border-color: var(--good); color: #05261a; }
  .gatebar button.ready#sendback { background: transparent; border-color: var(--bad); color: var(--bad); }
  .locked { color: var(--warn); font-size: 13px; }
  .summary { padding: 26px; text-align: center; }
  .summary .big { font-size: 42px; color: var(--cobalt2); font-weight: 800; }
  .summary p { color: var(--dim); max-width: 560px; margin: 10px auto; }
  .summary .panel-note { text-align: left; margin: 18px auto 0; max-width: 620px;
                         border: 1px solid var(--line); border-radius: 10px;
                         padding: 14px 16px; color: var(--text); font-size: 14px; }
  code { background: #1a2740; border: 1px solid var(--line); border-radius: 4px;
         padding: 0 4px; color: var(--cobalt2); }
  button.opt.wrong code { color: inherit; }
</style>
</head>
<body>
<header>
  <div class="brand">SP1 GATE · demo</div>
  <div class="step" id="progress"></div>
</header>
<main id="main"></main>
<div class="gatebar" id="gatebar" hidden>
  <span class="locked" id="lockmsg">Answer the questions to unlock the gate</span>
  <button id="approve" disabled>1 · Approve</button>
  <button id="sendback" disabled>2 · Send back</button>
  <span class="hint">keys: A–D answer · 1 approve · 2 send back</span>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get("t");
let STATE = null, ex = 0, qi = 0, unlocked = false;

async function api(path, body) {
  const r = await fetch(path + "?t=" + TOKEN, body ? {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  } : {});
  return r.json();
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function md(s) {  // just `code` spans
  return escapeHtml(s).replace(/`([^`]+)`/g, "<code>$1</code>");
}

function render() {
  const main = document.getElementById("main");
  main.innerHTML = "";
  const e = STATE.examples[ex];
  document.getElementById("progress").textContent =
    "review " + (ex + 1) + " of " + STATE.examples.length +
    " · question " + Math.min(qi + 1, e.questions.length) + " of " + e.questions.length;

  const left = el("div", "panel");
  left.appendChild(el("h2", "", "The agent proposes"));
  left.appendChild(el("div", "agent-says",
    '<span class="who">agent:</span> ' + escapeHtml(e.agent_says)));
  left.appendChild(el("div", "filename", "wants to write " + e.file));
  const pre = el("pre", "code");
  e.code.split("\n").forEach((line, i) => {
    const row = el("div");
    row.appendChild(el("span", "ln", String(i + 1)));
    row.appendChild(document.createTextNode(line));
    pre.appendChild(row);
  });
  left.appendChild(pre);
  main.appendChild(left);

  const right = el("div", "panel");
  right.appendChild(el("h2", "", "Before you press 1"));
  const body = el("div", "qbody");
  right.appendChild(body);
  main.appendChild(right);

  if (qi >= e.questions.length) {
    body.appendChild(el("p", "stem", "All questions answered."));
    body.appendChild(el("p", "", 'The gate is unlocked. Press <b>1</b> to let the agent write ' +
      escapeHtml(e.file) + ", or <b>2</b> to send it back with a reason."));
    unlocked = true;
  } else {
    const q = e.questions[qi];
    body.appendChild(el("span", "lens", q.lens));
    body.appendChild(el("p", "stem", md(q.stem)));
    q.options.forEach(o => {
      const b = el("button", "opt",
        '<span class="letter">' + o.letter + "</span>" + md(o.text));
      b.dataset.letter = o.letter;
      b.onclick = () => answer(q.id, o.letter, b, body);
      body.appendChild(b);
    });
    unlocked = false;
  }
  updateGatebar();
}

async function answer(qid, letter, btn, body) {
  if (btn.disabled) return;
  const res = await api("/api/answer", {qid: qid, letter: letter});
  if (res.correct) {
    btn.classList.add("right");
    body.querySelectorAll("button.opt").forEach(b => b.disabled = true);
    const fb = el("div", "feedback ok",
      '<span class="tag">Correct.</span> ' + md(res.explain));
    const next = el("button", "next", "Next");
    next.onclick = () => { qi += 1; render(); };
    body.appendChild(fb);
    body.appendChild(next);
    next.focus();
  } else {
    btn.disabled = true;
    btn.classList.add("wrong");
    const fb = el("div", "feedback",
      '<span class="tag">Not quite.</span> ' + md(res.why));
    btn.insertAdjacentElement("afterend", fb);
  }
}

function updateGatebar() {
  const bar = document.getElementById("gatebar");
  bar.hidden = false;
  const a = document.getElementById("approve"), s = document.getElementById("sendback");
  a.disabled = s.disabled = !unlocked;
  a.classList.toggle("ready", unlocked);
  s.classList.toggle("ready", unlocked);
  document.getElementById("lockmsg").textContent = unlocked
    ? "Gate unlocked — your call:" : "Answer the questions to unlock the gate";
}

async function decide(decision) {
  if (!unlocked) return;
  let reason = "";
  if (decision === "sent back") {
    reason = prompt("What should the agent fix? (this becomes its next instruction)") || "";
    if (reason === "") return;
  }
  const res = await api("/api/decide",
    {name: STATE.examples[ex].name, decision: decision, reason: reason});
  ex += 1; qi = 0; unlocked = false;
  if (ex >= STATE.examples.length) summary(res);
  else render();
}

function summary(res) {
  document.getElementById("gatebar").hidden = true;
  document.getElementById("progress").textContent = "done";
  const main = document.getElementById("main");
  main.innerHTML = "";
  const s = el("div", "panel summary");
  s.style.gridColumn = "1 / -1";
  s.appendChild(el("div", "big",
    res.first_try_right + " / " + res.first_try_total));
  s.appendChild(el("p", "", "right on the first try. Retries got you through the gate, " +
    "but only first tries would train the learner model."));
  s.appendChild(el("div", "panel-note",
    "<b>Everything you just answered was computed, not written by a model.</b> " +
    "The keys came from running the code; the wrong options came from executed " +
    "one-line bugs, checked misconceptions, and taint analysis. In the real SP1 Gate, " +
    "this page appears when your coding agent is about to act, and pressing 1 here " +
    "is what lets it act. Close this tab to finish; the server stops with Ctrl+C."));
  main.appendChild(s);
}

api("/api/state").then(s => { STATE = s; render(); });

document.addEventListener("keydown", (ev) => {
  if (ev.key === "1") decide("approved");
  else if (ev.key === "2") decide("sent back");
  else {
    const L = ev.key.toUpperCase();
    if ("ABCD".includes(L)) {
      const b = document.querySelector('button.opt[data-letter="' + L + '"]');
      if (b && !b.disabled) b.click();
    }
  }
});
document.getElementById("approve").onclick = () => decide("approved");
document.getElementById("sendback").onclick = () => decide("sent back");
</script>
</body>
</html>
"""


def make_handler(gate, token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _authorized(self):
            return ("t=" + token) in (self.path.split("?", 1) + [""])[1]

        def _send(self, body, ctype):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authorized():
                self.send_error(403)
                return
            if self.path.startswith("/api/state"):
                self._send(json.dumps(gate.public_state()), "application/json")
            else:
                self._send(PAGE, "text/html")

        def do_POST(self):
            if not self._authorized():
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.startswith("/api/answer"):
                self._send(json.dumps(gate.answer(body.get("qid"), body.get("letter"))),
                           "application/json")
            elif self.path.startswith("/api/decide"):
                self._send(json.dumps(gate.decide(body.get("name"), body.get("decision"),
                                                  body.get("reason", ""))),
                           "application/json")
            else:
                self.send_error(404)

    return Handler


def main():
    print("SP1 Gate demo: computing questions from the example code (about 10 seconds)...")
    gate = Gate(build_examples())
    token = secrets.token_urlsafe(16)

    with socket.socket() as probe:  # find a free port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                             make_handler(gate, token))
    url = f"http://127.0.0.1:{port}/?t={token}"
    threading.Timer(0.4, webbrowser.open, [url]).start()
    print(f"\nGate open: {url}")
    print("If no browser opened, copy that address into one. Ctrl+C here stops it.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGate closed.")


if __name__ == "__main__":
    main()
