"""SP1 Gate daemon: a small web server on your own computer that holds pending reviews.

- It serves the gate page on 127.0.0.1 only, and every request needs this run's secret token.
- Answer keys stay in this process's memory. The page only learns "correct" or
  "not quite, because..." for the option that was picked.
- The hook scripts (hook.py) talk to it over the same local port and token.

Started automatically by hook.py; `python3 sp1.py stop` stops it.
"""
import http.server
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from actions import questions_for_action  # noqa: E402
from code_questions import questions_for_turn  # noqa: E402
from common import load_config, make_rng, public_question, sp1_home  # noqa: E402

MAX_FILE_BYTES = 200_000
ABANDON_AFTER = 8.0        # seconds without a poll from the waiting hook
IDLE_EXIT_AFTER = 12 * 3600


def _read(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


class Gate:
    def __init__(self, port, token):
        self.port, self.token = port, token
        self.secret = secrets.token_hex(16)     # seeds option order: the agent can't predict it
        self.lock = threading.RLock()
        self.items = {}
        self.order = []
        self.turns = {}           # project -> {path: {"before": text or None}}
        self.history = []
        self.counter = 0
        self.last_page_seen = 0.0
        self.last_activity = time.time()
        self.config = load_config()

    @property
    def url(self):
        return "http://127.0.0.1:{}/?t={}".format(self.port, self.token)

    def _new_id(self, kind):
        self.counter += 1
        return "{}{}".format(kind[0], self.counter)

    def _open_page(self):
        if not self.config.get("open_browser", True):
            return
        if time.time() - self.last_page_seen < 4:
            return                 # the page is already open and polling
        self.last_page_seen = time.time()      # don't open twice in a row
        threading.Thread(target=webbrowser.open, args=(self.url,), daemon=True).start()

    def _log(self, item, qid, correct):
        q = next(q for q in item["questions"] if q["id"] == qid)
        record = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": item["kind"], "project": item["project"],
                  "question": qid.split("-")[1] if "-" in qid else qid, "lens": q["lens"],
                  "first_try_correct": correct}
        try:
            with open(os.path.join(sp1_home(), "history.jsonl"), "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ hook side

    def pre_tool(self, body):
        tool, tool_input = body.get("tool_name"), body.get("tool_input") or {}
        cwd = body.get("cwd") or body.get("project") or os.getcwd()
        with self.lock:
            self.last_activity = time.time()
            rng = make_rng(self.secret, self.counter)
            try:
                qs = questions_for_action(tool, tool_input, cwd, rng, self.config["max_action_questions"])
            except Exception as exc:          # a bug in a detector must not block the agent's work
                sys.stderr.write("pre_tool error: {!r}\n".format(exc))
                qs = []
            if os.environ.get("SP1_DEBUG"):
                sys.stderr.write("pre_tool {} {!r} -> {} question(s)\n".format(
                    tool, tool_input.get("command") or tool_input.get("file_path"), len(qs)))
            if not qs:
                return {"item": None}
            item_id = self._new_id("action")
            view = {"tool": tool, "cwd": cwd}
            if tool == "Bash":
                view["command"] = tool_input.get("command", "")
                view["description"] = tool_input.get("description", "")
            else:
                path = tool_input.get("file_path", "")
                view["file"] = os.path.relpath(path, cwd) if path.startswith(cwd + os.sep) else path
                if tool == "Edit":
                    view["old"] = tool_input.get("old_string", "")
                    view["new"] = tool_input.get("new_string", "")
                else:
                    view["new"] = (tool_input.get("content") or "")[:20000]
            self.items[item_id] = {"id": item_id, "kind": "action", "project": body.get("project", cwd),
                                   "created": time.time(), "last_poll": time.time(), "view": view,
                                   "questions": qs, "first_try": {}, "solved": [], "status": "pending"}
            self.order.append(item_id)
            self._open_page()
            return {"item": item_id, "count": len(qs), "url": self.url}

    def item_status(self, item_id):
        with self.lock:
            item = self.items.get(item_id)
            if not item:
                return {"status": "missing"}
            item["last_poll"] = time.time()
            return {"status": item["status"]}

    def post_tool(self, body):
        tool, tool_input = body.get("tool_name"), body.get("tool_input") or {}
        response = body.get("tool_response") or {}
        path = tool_input.get("file_path") or (response.get("filePath") if isinstance(response, dict) else None)
        if tool not in ("Edit", "Write") or not path:
            return {}
        path = os.path.realpath(path)
        project = body.get("project") or body.get("cwd") or ""
        with self.lock:
            self.last_activity = time.time()
            files = self.turns.setdefault(project, {})
            if path not in files:
                before = None
                if isinstance(response, dict):
                    if response.get("type") == "create":
                        before = None
                    elif "originalFile" in response:
                        before = response.get("originalFile")
                files[path] = {"before": before}
        return {}

    def stop(self, body):
        project = body.get("project") or body.get("cwd") or ""
        with self.lock:
            self.last_activity = time.time()
            files = self.turns.pop(project, {})
            changes = []
            for path, rec in files.items():
                after = _read(path)
                if after is None:
                    continue
                if rec["before"] is not None and rec["before"] == after:
                    continue
                changes.append({"path": path, "before": rec["before"], "after": after})
            if not changes:
                return {"item": None}
            rng = make_rng(self.secret, "turn{}".format(self.counter))
            try:
                qs = questions_for_turn(changes, project, rng, self.config["max_turn_questions"])
            except Exception as exc:
                sys.stderr.write("stop error: {!r}\n".format(exc))
                qs = []
            if not qs:
                return {"item": None}
            item_id = self._new_id("turn")
            view_files = []
            from code_questions import changed_lines
            for ch in changes:
                rel = os.path.relpath(ch["path"], project) if ch["path"].startswith(project + os.sep) else ch["path"]
                view_files.append({"rel": rel, "text": ch["after"],
                                   "changed": sorted(changed_lines(ch["before"], ch["after"])),
                                   "created": ch["before"] is None})
            self.items[item_id] = {"id": item_id, "kind": "turn", "project": project, "created": time.time(),
                                   "view": {"files": view_files}, "questions": qs, "first_try": {},
                                   "solved": [], "status": "pending"}
            self.order.append(item_id)
            self._open_page()
            return {"item": item_id, "count": len(qs), "url": self.url}

    def prompt(self, body):
        project = body.get("project") or body.get("cwd") or ""
        with self.lock:
            self.last_activity = time.time()
            pending = [i for i in self.order if self.items[i]["kind"] == "turn"
                       and self.items[i]["status"] == "pending" and self.items[i]["project"] == project]
            if not pending:
                return {"blocked": False}
            left = sum(len(self.items[i]["questions"]) - len(self.items[i]["solved"]) for i in pending)
            self._open_page()
            return {"blocked": True, "count": left, "url": self.url}

    # ------------------------------------------------------------ page side

    def _abandon_stale(self):
        now = time.time()
        for i in self.order:
            item = self.items[i]
            if item["kind"] == "action" and item["status"] == "pending" and now - item["last_poll"] > ABANDON_AFTER:
                item["status"] = "abandoned"
                self._archive(item)

    def _archive(self, item):
        self.history.insert(0, {
            "kind": item["kind"], "status": item["status"],
            "title": item["view"].get("command") or item["view"].get("file")
            or ", ".join(f["rel"] for f in item["view"].get("files", [])),
            "right": sum(1 for v in item["first_try"].values() if v), "total": len(item["questions"]),
            "when": time.strftime("%H:%M"),
        })
        del self.history[12:]

    def public_state(self):
        with self.lock:
            self.last_page_seen = time.time()
            self._abandon_stale()
            pending = [self.items[i] for i in self.order if self.items[i]["status"] == "pending"]
            pending.sort(key=lambda it: (it["kind"] != "action", it["created"]))
            current = pending[0] if pending else None
            right = sum(h["right"] for h in self.history)
            total = sum(h["total"] for h in self.history if h["status"] != "abandoned")
            state = {"after_answers": self.config["after_answers"], "waiting": max(0, len(pending) - 1),
                     "history": self.history, "score": {"right": right, "total": total}, "current": None}
            if current:
                state["current"] = {"id": current["id"], "kind": current["kind"], "view": current["view"],
                                    "project": os.path.basename(current["project"].rstrip(os.sep)),
                                    "questions": [dict(public_question(q), evidence=q.get("evidence", {}))
                                                  for q in current["questions"]],
                                    "solved": current["solved"]}
            return state

    def answer(self, item_id, qid, letter):
        with self.lock:
            item = self.items.get(item_id)
            if not item or item["status"] != "pending":
                return {"error": "This review is closed."}
            q = next((q for q in item["questions"] if q["id"] == qid), None)
            if not q:
                return {"error": "Unknown question."}
            correct = letter == q["answer"]
            if qid not in item["first_try"]:
                item["first_try"][qid] = correct
                self._log(item, qid, correct)
            if not correct:
                picked = next((o for o in q["options"] if o["letter"] == letter), None)
                return {"correct": False, "why": picked["why"] if picked else ""}
            if qid not in item["solved"]:
                item["solved"].append(qid)
            done = len(item["solved"]) == len(item["questions"])
            if done:
                item["status"] = "unlocked" if item["kind"] == "action" else "done"
                self._archive(item)
            return {"correct": True, "explain": q["explain"], "done": done}


def make_handler(gate):
    page_path = os.path.join(HERE, "page.html")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _query(self):
            return parse_qs(urlparse(self.path).query)

        def _authorized(self):
            return secrets.compare_digest(self._query().get("t", [""])[0], gate.token)

        def _send(self, obj, ctype="application/json", code=200):
            data = obj.encode("utf-8") if isinstance(obj, str) else json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            if not self._authorized():
                return self._send({"error": "forbidden"}, code=403)
            path = urlparse(self.path).path
            if path == "/":
                with open(page_path, encoding="utf-8") as f:
                    return self._send(f.read(), "text/html")
            if path == "/api/state":
                return self._send(gate.public_state())
            if path == "/hook/ping":
                return self._send({"ok": True, "url": gate.url, "pid": os.getpid()})
            if path == "/hook/item":
                return self._send(gate.item_status(self._query().get("id", [""])[0]))
            return self._send({"error": "not found"}, code=404)

        def do_POST(self):
            if not self._authorized():
                return self._send({"error": "forbidden"}, code=403)
            path, body = urlparse(self.path).path, self._body()
            routes = {
                "/api/answer": lambda: gate.answer(body.get("item"), body.get("qid"), body.get("letter")),
                "/hook/pre_tool": lambda: gate.pre_tool(body),
                "/hook/post_tool": lambda: gate.post_tool(body),
                "/hook/stop": lambda: gate.stop(body),
                "/hook/prompt": lambda: gate.prompt(body),
            }
            if path == "/hook/shutdown":
                self._send({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path in routes:
                return self._send(routes[path]())
            return self._send({"error": "not found"}, code=404)

    return Handler


def main():
    home = sp1_home()
    state_file = os.path.join(home, "daemon.json")
    token = secrets.token_urlsafe(18)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), None)
    gate = Gate(server.server_address[1], token)
    server.RequestHandlerClass = make_handler(gate)
    tmp = state_file + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": gate.port, "token": token, "pid": os.getpid(), "url": gate.url,
                   "started": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    os.replace(tmp, state_file)
    try:
        os.remove(os.path.join(home, "daemon.lock"))
    except OSError:
        pass

    def idle_watch():
        while True:
            time.sleep(60)
            if time.time() - gate.last_activity > IDLE_EXIT_AFTER and time.time() - gate.last_page_seen > 600:
                server.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        try:
            with open(state_file) as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(state_file)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
