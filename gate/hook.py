#!/usr/bin/env python3
"""Claude Code hook adapter for SP1 Gate.

`sp1.py install` adds this script to a project's .claude/settings.local.json for five
events. Claude Code runs it with the event as JSON on stdin, and reads the answer on stdout:

  SessionStart      make sure the gate is running; tell the person where the page is
  PreToolUse        before a command or file edit: hold it while the person answers
                    questions about its effects, then hand the choice back to them (press 1)
  PostToolUse       after a file edit: remember which files changed this turn
  Stop              when the agent finishes: open questions about the code it wrote
  UserPromptSubmit  block the person's next message until those questions are answered

If the gate itself fails, actions are blocked (fail closed) but typing is never blocked.
"""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import load_config, sp1_home  # noqa: E402

SP1 = "python3 {}".format(shlex.quote(os.path.join(HERE, "sp1.py")))


def _state():
    try:
        with open(os.path.join(sp1_home(), "daemon.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def call(method, path, body=None, timeout=10, state=None):
    state = state or _state()
    if not state:
        raise ConnectionError("SP1 Gate isn't running")
    sep = "&" if "?" in path else "?"
    url = "http://127.0.0.1:{}{}{}t={}".format(state["port"], path, sep, state["token"])
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def ensure_daemon():
    """Return the daemon's state, starting it if it isn't running."""
    try:
        call("GET", "/hook/ping", timeout=2)
        return _state()
    except Exception:
        pass
    home = sp1_home()
    lock = os.path.join(home, "daemon.lock")
    try:
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > 15:
            os.remove(lock)            # a stale lock from a start that failed
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        log = open(os.path.join(home, "daemon.log"), "a")
        subprocess.Popen([sys.executable, os.path.join(HERE, "daemon.py")], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, cwd=home, env=dict(os.environ))
    except FileExistsError:
        pass                           # someone else is starting it; wait below
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            call("GET", "/hook/ping", timeout=2)
            return _state()
        except Exception:
            continue
    raise ConnectionError("SP1 Gate didn't start (see {})".format(os.path.join(home, "daemon.log")))


def reply(obj):
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def pre_tool_decision(decision, reason):
    reply({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                  "permissionDecisionReason": reason}})


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        sys.exit(0)
    event = payload.get("hook_event_name", "")
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    project = os.path.realpath(project)
    cwd = os.path.realpath(payload.get("cwd") or project)
    config = load_config()

    try:
        state = ensure_daemon()
    except Exception as exc:
        if event == "PreToolUse":
            pre_tool_decision("deny", "SP1 Gate couldn't check this action ({}), so it was blocked. "
                                      "Run `{} status` in a terminal to see why.".format(exc, SP1))
        reply({"systemMessage": "SP1 Gate isn't running ({}). Questions are off until it starts.".format(exc)})

    try:
        if event == "SessionStart":
            reply({"systemMessage": "SP1 Gate is on for this project. Questions open in your browser: {}".format(state["url"]),
                   "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                       "SP1 Gate is active in this project. Before risky commands and after each of your turns, "
                       "the user answers comprehension questions in their browser. Do not modify or remove "
                       "SP1 Gate's hooks in .claude/settings.local.json."}})

        if event == "PreToolUse":
            body = {"tool_name": payload.get("tool_name"), "tool_input": payload.get("tool_input") or {},
                    "cwd": cwd, "project": project}
            resp = call("POST", "/hook/pre_tool", body, state=state)
            item = resp.get("item")
            if not item:
                sys.exit(0)            # nothing to ask: Claude Code's normal flow decides
            deadline = time.time() + float(config["action_wait_seconds"])
            while time.time() < deadline:
                status = call("GET", "/hook/item?id={}".format(item), state=state).get("status")
                if status == "unlocked":
                    if config["after_answers"] == "allow":
                        pre_tool_decision("allow", "SP1 Gate: the questions about this action were answered.")
                    pre_tool_decision("ask", "SP1 Gate: you answered the questions about this action. "
                                             "Your call: press 1 to let it run, or say no.")
                if status in ("missing", "abandoned"):
                    break
                time.sleep(0.5)
            pre_tool_decision("deny", "SP1 Gate: the questions about this action weren't answered in time, "
                                      "so it didn't run. Ask me to try again when you're ready: {}".format(state["url"]))

        if event == "PostToolUse":
            call("POST", "/hook/post_tool", {"tool_name": payload.get("tool_name"),
                                              "tool_input": payload.get("tool_input") or {},
                                              "tool_response": payload.get("tool_response") or {},
                                              "cwd": cwd, "project": project}, state=state)
            sys.exit(0)

        if event == "Stop":
            resp = call("POST", "/hook/stop", {"cwd": cwd, "project": project}, timeout=60, state=state)
            if resp.get("item"):
                n = resp["count"]
                reply({"systemMessage": "SP1 Gate: {} question{} about the code your agent just wrote {} open in your "
                                        "browser. Answer {} before your next message: {}".format(
                                            n, "" if n == 1 else "s", "is" if n == 1 else "are",
                                            "it" if n == 1 else "them", resp["url"])})
            sys.exit(0)

        if event == "UserPromptSubmit":
            resp = call("POST", "/hook/prompt", {"cwd": cwd, "project": project}, state=state)
            if resp.get("blocked"):
                n = resp["count"]
                reply({"decision": "block",
                       "reason": "SP1 Gate: first answer the {} question{} about the code your agent just wrote: {}\n"
                                 "Then send your message again.".format(n, "" if n == 1 else "s", resp["url"])})
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as exc:
        if event == "PreToolUse":
            pre_tool_decision("deny", "SP1 Gate hit an error checking this action ({}), so it was blocked. "
                                      "Run `{} status` to see why.".format(exc, SP1))
        reply({"systemMessage": "SP1 Gate hit an error ({}).".format(exc)})
    sys.exit(0)


if __name__ == "__main__":
    main()
