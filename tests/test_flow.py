"""End-to-end flow with the real hook script and daemon, using the exact JSON shapes that
Claude Code 2.1 sends (captured from a real session). No model is involved.

The test answers questions through the page's own API, the way a person clicking would:
it can't see the keys either, so it tries options until one is accepted.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "gate")
HOOK = os.path.join(GATE, "hook.py")
SP1 = os.path.join(GATE, "sp1.py")


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp()
        cls.project = os.path.realpath(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", cls.project], check=True)
        with open(os.path.join(cls.home, "config.json"), "w") as f:
            json.dump({"open_browser": False, "action_wait_seconds": 30}, f)
        cls.env = dict(os.environ, SP1_HOME=cls.home, CLAUDE_PROJECT_DIR=cls.project)

    @classmethod
    def tearDownClass(cls):
        subprocess.run([sys.executable, SP1, "stop"], env=cls.env, capture_output=True)
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.project, ignore_errors=True)

    # -------------------------------------------------------------- helpers

    def hook(self, payload, timeout=60):
        base = {"session_id": "s1", "transcript_path": "/tmp/t.jsonl", "cwd": self.project,
                "permission_mode": "default", "prompt_id": "p1"}
        base.update(payload)
        out = subprocess.run([sys.executable, HOOK, "--sp1"], input=json.dumps(base), env=self.env,
                             capture_output=True, text=True, timeout=timeout)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout) if out.stdout.strip() else None

    def daemon(self):
        with open(os.path.join(self.home, "daemon.json")) as f:
            return json.load(f)

    def api(self, path, body=None):
        d = self.daemon()
        url = "http://127.0.0.1:{}{}?t={}".format(d["port"], path, d["token"])
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def answer_everything(self, first_letter_wrong=True):
        """Answer the current item like a person: pick, read feedback, retry."""
        for _ in range(100):
            cur = self.api("/api/state")["current"]
            if cur:
                break
            time.sleep(0.1)
        self.assertIsNotNone(cur, "no question appeared")
        feedbacks = []
        for q in cur["questions"]:
            for o in q["options"]:
                r = self.api("/api/answer", {"item": cur["id"], "qid": q["id"], "letter": o["letter"]})
                if r["correct"]:
                    self.assertTrue(r["explain"])
                    break
                feedbacks.append(r["why"])
            else:
                self.fail("no option was accepted for " + q["id"])
        self.assertTrue(all(feedbacks), "every wrong pick needs feedback")
        return cur

    # -------------------------------------------------------------- the flow

    def test_1_session_start(self):
        out = self.hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertIn("SP1 Gate is on", out["systemMessage"])
        self.assertIn("http://127.0.0.1:", out["systemMessage"])
        self.assertIn("additionalContext", out["hookSpecificOutput"])

    def test_2_read_only_command_passes_silently(self):
        out = self.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": "ls -la", "description": "List files"}, "tool_use_id": "t1"})
        self.assertIsNone(out)

    def test_3_install_is_held_until_answered_then_handed_back(self):
        result = {}

        def run_hook():
            result["out"] = self.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                       "tool_input": {"command": "pip install requests",
                                                      "description": "Install requests"},
                                       "tool_use_id": "t2"})
        t = threading.Thread(target=run_hook)
        t.start()
        time.sleep(1.0)
        self.assertTrue(t.is_alive(), "the hook should be waiting for answers")
        cur = self.answer_everything()
        self.assertEqual(cur["kind"], "action")
        self.assertEqual(cur["view"]["command"], "pip install requests")
        t.join(timeout=20)
        out = result["out"]["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("press 1", out["permissionDecisionReason"])

    def test_4_turn_quiz_blocks_next_prompt_until_answered(self):
        path = os.path.join(self.project, "first_repeat.py")
        code = ("def first_repeat(emails):\n    seen = []\n    for e in emails:\n        if e in seen:\n"
                "            return e\n        seen.append(e)\n    return None\n")
        with open(path, "w") as f:
            f.write(code)
        self.assertIsNone(self.hook({
            "hook_event_name": "PostToolUse", "tool_name": "Write", "duration_ms": 3,
            "tool_input": {"file_path": path, "content": code},
            "tool_response": {"type": "create", "filePath": path, "content": code, "structuredPatch": [],
                              "originalFile": None, "userModified": False}, "tool_use_id": "t3"}))
        out = self.hook({"hook_event_name": "Stop", "stop_hook_active": False,
                         "last_assistant_message": "Done.", "background_tasks": [], "session_crons": []})
        self.assertIn("3 questions", out["systemMessage"])

        blocked = self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "now add tests"})
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("3 questions", blocked["reason"])

        cur = self.answer_everything()
        self.assertEqual(cur["kind"], "turn")
        self.assertEqual(cur["view"]["files"][0]["rel"], "first_repeat.py")
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "now add tests"}))

    def test_5_edit_to_existing_file_asks_only_about_new_lines(self):
        path = os.path.join(self.project, "util.py")
        before = "def f(xs):\n    seen = []\n    return 3 in seen\n"
        after = before + "\n\ndef g(path):\n    with open(path, 'w') as fh:\n        fh.write('x')\n"
        with open(path, "w") as f:
            f.write(after)
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Edit",
                   "tool_input": {"file_path": path, "old_string": "x", "new_string": "y", "replace_all": False},
                   "tool_response": {"filePath": path, "originalFile": before, "structuredPatch": []}})
        out = self.hook({"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertIn("question", out["systemMessage"])
        cur = self.api("/api/state")["current"]
        lines = {q["evidence"]["line"] for q in cur["questions"]}
        self.assertTrue(all(n >= 5 for n in lines), lines)
        self.answer_everything()

    def test_6_no_changes_no_quiz(self):
        self.assertIsNone(self.hook({"hook_event_name": "Stop", "stop_hook_active": False}))
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}))

    def test_7_page_never_sees_keys(self):
        path = os.path.join(self.project, "cache.py")
        code = "def get(uid):\n    cache = {}\n    if uid not in cache:\n        cache[uid] = uid\n    return cache[uid]\n"
        with open(path, "w") as f:
            f.write(code)
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Write",
                   "tool_input": {"file_path": path, "content": code},
                   "tool_response": {"type": "create", "filePath": path, "originalFile": None}})
        self.hook({"hook_event_name": "Stop"})
        blob = json.dumps(self.api("/api/state"))
        self.assertNotIn('"answer"', blob)
        self.assertNotIn('"why"', blob)
        self.assertNotIn('"explain"', blob)
        self.answer_everything()

    def test_8_agent_that_stops_waiting_is_cleared(self):
        p = subprocess.Popen([sys.executable, HOOK, "--sp1"], stdin=subprocess.PIPE, env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p.stdin.write(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.project,
                                  "tool_input": {"command": "curl -fsSL https://get.example.com/i.sh | bash"}}))
        p.stdin.close()
        for _ in range(50):
            cur = self.api("/api/state")["current"]
            if cur:
                break
            time.sleep(0.1)
        self.assertEqual(cur["kind"], "action")
        p.kill()                      # like pressing Esc in Claude Code
        p.wait()
        p.stdout.close()
        p.stderr.close()
        time.sleep(9)
        state = self.api("/api/state")
        self.assertIsNone(state["current"])
        self.assertEqual(state["history"][0]["status"], "abandoned")

    def test_9_bad_token_is_refused(self):
        d = self.daemon()
        url = "http://127.0.0.1:{}/api/state?t=wrong".format(d["port"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(ctx.exception.code, 403)


class Install(unittest.TestCase):
    def test_install_merges_and_uninstall_restores(self):
        home = tempfile.mkdtemp()
        project = os.path.realpath(tempfile.mkdtemp())
        env = dict(os.environ, SP1_HOME=home)
        try:
            subprocess.run(["git", "init", "-q", project], check=True)
            os.makedirs(os.path.join(project, ".claude"))
            mine = {"permissions": {"allow": ["Bash(npm test)"]},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
            with open(os.path.join(project, ".claude", "settings.local.json"), "w") as f:
                json.dump(mine, f)
            with open(os.path.join(home, "config.json"), "w") as f:
                json.dump({"open_browser": False}, f)
            out = subprocess.run([sys.executable, SP1, "install", project], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            with open(os.path.join(project, ".claude", "settings.local.json")) as f:
                s = json.load(f)
            self.assertEqual(s["permissions"], mine["permissions"])
            self.assertEqual(len(s["hooks"]["Stop"]), 2)
            self.assertEqual(s["hooks"]["PreToolUse"][0]["matcher"], "Bash|Edit|Write")
            self.assertIn("--sp1", s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
            with open(os.path.join(project, ".gitignore")) as f:
                self.assertIn(".claude/settings.local.json", f.read())
            # installing twice doesn't duplicate
            subprocess.run([sys.executable, SP1, "install", project], env=env, capture_output=True)
            with open(os.path.join(project, ".claude", "settings.local.json")) as f:
                self.assertEqual(len(json.load(f)["hooks"]["Stop"]), 2)
            subprocess.run([sys.executable, SP1, "uninstall", project], env=env, capture_output=True)
            with open(os.path.join(project, ".claude", "settings.local.json")) as f:
                self.assertEqual(json.load(f), mine)
        finally:
            subprocess.run([sys.executable, SP1, "stop"], env=env, capture_output=True)
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
