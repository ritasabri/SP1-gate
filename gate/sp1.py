#!/usr/bin/env python3
"""SP1 Gate command line.

    python3 gate/sp1.py install [FOLDER]     turn the gate on for a project (default: this folder)
    python3 gate/sp1.py uninstall [FOLDER]   turn it off again
    python3 gate/sp1.py status               is it running, and where is the page?
    python3 gate/sp1.py open                 open the gate page in your browser
    python3 gate/sp1.py stop                 stop the background gate server
    python3 gate/sp1.py set after_answers allow|ask
                                             ask (default): after the questions, Claude Code still
                                             asks you to press 1. allow: it runs right away.
"""
import json
import os
import shlex
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import DEFAULT_CONFIG, sp1_home  # noqa: E402

HOOK = os.path.join(HERE, "hook.py")
MARK = "--sp1"
EVENTS = {
    "SessionStart": {"timeout": 30},
    "PreToolUse": {"matcher": "Bash|Edit|Write", "timeout": 600},
    "PostToolUse": {"matcher": "Edit|Write", "timeout": 30},
    "Stop": {"timeout": 120},
    "UserPromptSubmit": {"timeout": 30},
}


def _settings_path(project):
    return os.path.join(project, ".claude", "settings.local.json")


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        text = f.read().strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        sys.exit("Can't read {} (it isn't valid JSON). Fix or remove it, then run install again.".format(path))


def _strip_sp1(settings):
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if MARK not in h.get("command", "")]
            if kept:
                group = dict(group, hooks=kept)
                groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def _project(args):
    project = os.path.realpath(os.path.expanduser(args[0] if args else os.getcwd()))
    if not os.path.isdir(project):
        sys.exit("No such folder: {}".format(project))
    if project in (os.path.realpath(os.path.expanduser("~")), "/"):
        sys.exit("Pick a project folder, not your whole home folder.")
    return project


def install(args):
    project = _project(args)
    path = _settings_path(project)
    settings = _strip_sp1(_load(path))
    command = "{} {} {}".format(shlex.quote(sys.executable), shlex.quote(HOOK), MARK)
    hooks = settings.setdefault("hooks", {})
    for event, opts in EVENTS.items():
        group = {"hooks": [{"type": "command", "command": command, "timeout": opts["timeout"]}]}
        if "matcher" in opts:
            group = {"matcher": opts["matcher"], **group}
        hooks.setdefault(event, []).append(group)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    if os.path.isdir(os.path.join(project, ".git")):
        gi = os.path.join(project, ".gitignore")
        existing = open(gi).read() if os.path.exists(gi) else ""
        if ".claude/settings.local.json" not in existing:
            with open(gi, "a") as f:
                f.write(("\n" if existing and not existing.endswith("\n") else "") + ".claude/settings.local.json\n")
    sys.path.insert(0, HERE)
    from hook import ensure_daemon
    state = ensure_daemon()
    print("SP1 Gate is on for {}".format(project))
    print("  hooks written to  {}".format(path))
    print("  gate page         {}".format(state["url"]))
    print()
    print("Next: start Claude Code in that folder (`cd {} && claude`),".format(shlex.quote(project)))
    print("or open the folder in VS Code and use the Claude Code extension.")
    print("If Claude Code was already open there, restart it so it loads the hooks.")


def uninstall(args):
    project = _project(args)
    path = _settings_path(project)
    if not os.path.exists(path):
        print("SP1 Gate wasn't installed in {}".format(project))
        return
    settings = _strip_sp1(_load(path))
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("SP1 Gate is off for {} (restart Claude Code there to apply).".format(project))


def status(_args):
    from hook import call, _state
    state = _state()
    try:
        call("GET", "/hook/ping", timeout=2)
        print("SP1 Gate is running: {}".format(state["url"]))
    except Exception:
        print("SP1 Gate isn't running. It starts by itself when Claude Code starts in a project")
        print("where it's installed. Log: {}".format(os.path.join(sp1_home(), "daemon.log")))


def open_page(_args):
    from hook import ensure_daemon
    url = ensure_daemon()["url"]
    webbrowser.open(url)
    print(url)


def stop(_args):
    from hook import call
    try:
        call("POST", "/hook/shutdown", {}, timeout=3)
        print("SP1 Gate stopped.")
    except Exception:
        print("SP1 Gate wasn't running.")


def set_option(args):
    if len(args) != 2 or args[0] not in DEFAULT_CONFIG:
        sys.exit("Usage: sp1.py set <option> <value>. Options: {}".format(", ".join(DEFAULT_CONFIG)))
    key, raw = args
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        value = raw.lower() in ("1", "true", "yes", "on")
    elif isinstance(default, int):
        value = int(raw)
    else:
        value = raw
    if key == "after_answers" and value not in ("ask", "allow"):
        sys.exit("after_answers must be ask or allow")
    path = os.path.join(sp1_home(), "config.json")
    config = {}
    if os.path.exists(path):
        with open(path) as f:
            config = json.load(f)
    config[key] = value
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print("{} = {} (the gate server picks it up the next time it starts: `sp1.py stop`)".format(key, value))


COMMANDS = {"install": install, "uninstall": uninstall, "status": status, "open": open_page,
            "stop": stop, "set": set_option}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
