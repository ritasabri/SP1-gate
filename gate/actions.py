"""Phase 1: questions about an action, before it runs.

Only what can be read from the action itself: which file it touches, what it downloads
or installs, and what it changes about the setup. Nothing the agent wrote is run to find
the answers. The only commands SP1 Gate runs itself are read-only git queries
(`git remote get-url`, `git status`, `git check-ignore`, `git rev-parse`).

Every question has one key that follows from the parsed action, and wrong options that
the same facts rule out. If a command is too unusual to be sure, it gets no question.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

from common import load_stdlib_names, question

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = SHELLS | {"python", "python3", "node", "perl", "ruby"}
OPS = {"|", "||", "&&", ";", "&", "|&", ";;"}
REDIRS = {">", ">>", ">|", "&>", "&>>", "<", "<<", "<<<", ">&", "<&", "<>"}
AGENT = "the AI agent"
AGENT_CAP = "The AI agent"


# ----------------------------------------------------------------- parsing commands

def _incomplete(text):
    """True if text can't stand alone: an open quote or an open parenthesis."""
    try:
        shlex.split(text)
    except ValueError:
        return True
    return text.count("(") > text.count(")")


def _logical_lines(command):
    """Split a command into lines, joining backslash continuations. Heredoc bodies are
    data, not commands, so they're dropped. If the line that started a heredoc is left
    open (as in `git commit -m "$(cat <<'EOF'` ... `EOF` `)"`), it continues after the body."""
    raw = command.replace("\\\n", " ").split("\n")
    lines, skip_until, pending = [], None, None
    for line in raw:
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        if pending is not None:
            if _incomplete(pending):
                line = pending + " " + line
            elif pending.strip():
                lines.append(pending)
            pending = None
        m = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if m:
            skip_until = m.group(1)
            pending = line[:m.start()] + line[m.end():]
            continue
        if line.strip():
            lines.append(line)
    if pending is not None and pending.strip():
        lines.append(pending)
    return lines


def _tokens(line):
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""   # shlex would treat '#' inside a URL as a comment; bash doesn't
    out = []
    for tok in lex:
        if tok.startswith("#") and not out:
            break
        if tok.startswith("#") and out and out[-1] in OPS:
            break
        out.append(tok)
    return out


def _new_segment():
    return {"argv": [], "redirects": [], "procsubs": [], "op_after": None,
            "pipe_to": None, "piped": False}


def parse_command(command):
    """Parse a shell command into simple commands ("segments"). Returns None if the
    command can't be parsed with confidence (unbalanced quotes, odd syntax)."""
    segments = []
    for line in _logical_lines(command):
        try:
            toks = _tokens(line)
        except ValueError:
            return None
        cur = _new_segment()
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in OPS:
                cur["op_after"] = t
                segments.append(cur)
                cur = _new_segment()
                i += 1
                continue
            if t in ("<(", ">("):            # process substitution: bash <(curl ...)
                j = i + 1
                inner = []
                while j < len(toks) and toks[j] != ")":
                    inner.append(toks[j])
                    j += 1
                cur["procsubs"].append(inner)
                if t == "<(" and not cur["argv"]:
                    return None
                cur["argv"].append("<(...)")
                i = j + 1
                continue
            if t in ("(", ")", "{", "}", "$", "$(", "`"):
                i += 1
                continue
            fd = None
            if t.isdigit() and i + 1 < len(toks) and toks[i + 1] in REDIRS:
                fd = int(t)
                i += 1
                t = toks[i]
            if t in REDIRS:
                target = toks[i + 1] if i + 1 < len(toks) else None
                cur["redirects"].append((t, target, fd))
                i += 2
                continue
            cur["argv"].append(t)
            i += 1
        segments.append(cur)
        segments[-1]["op_after"] = segments[-1]["op_after"] or "\n"
    segments = [s for s in segments if s["argv"] or s["redirects"]]
    for a, b in zip(segments, segments[1:]):
        if a["op_after"] in ("|", "|&"):
            a["pipe_to"] = b
            b["piped"] = True
    for s in segments:
        _normalize(s)
    return segments


def _normalize(seg):
    """Strip what doesn't change which program runs: VAR=value prefixes, sudo, env,
    nohup, time, command, exec. Remember whether sudo was used."""
    argv = list(seg["argv"])
    seg["sudo"] = False
    changed = True
    while argv and changed:
        changed = False
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv.pop(0)
            changed = True
        elif argv[0] == "sudo":
            seg["sudo"] = True
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                flag = argv.pop(0)
                if flag in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U") and argv:
                    argv.pop(0)
            changed = True
        elif argv[0] in ("env", "nohup", "time", "command", "exec", "builtin"):
            argv.pop(0)
            changed = True
    seg["args"] = argv[1:] if argv else []
    seg["prog"] = os.path.basename(argv[0]) if argv else ""
    seg["words"] = argv


def _shown(seg, limit=60):
    text = " ".join(shlex.quote(w) if re.search(r"\s", w) else w for w in seg["words"])
    if seg.get("sudo"):
        text = "sudo " + text
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _run_git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return out.returncode, out.stdout


def _q(spec_list, qid, priority, lens, stem, key, why, distractors):
    spec_list.append({"qid": qid, "priority": priority, "lens": lens, "stem": stem,
                      "key": key, "why": why, "distractors": distractors})


# ----------------------------------------------------------------- downloads

CURL_VALUE_SHORT = set("odHXuAebcFTwxKmrECYyzUtQP")
CURL_SENDS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json",
              "-F", "--form", "-T", "--upload-file"}
CURL_LONG_VALUE = {"--output", "--header", "--request", "--user", "--user-agent", "--referer",
                   "--cookie", "--cookie-jar", "--write-out", "--proxy", "--max-time", "--range",
                   "--cert", "--continue-at", "--time-cond", "--url", "--config", "--retry",
                   "--connect-timeout", "--output-dir", "--create-dirs"} | CURL_SENDS


def _parse_curl(args):
    """Return (url, output_file, remote_name, sends_data) or None if unsure."""
    url, output, remote_name, sends = None, None, False, False
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name, _, inline = a.partition("=")
            if name in ("--config", "--output-dir", "--next"):
                return None
            if name in CURL_SENDS:
                sends = True
            if name == "--remote-name":
                remote_name = True
            elif name in CURL_LONG_VALUE:
                value = inline if inline else (args[i + 1] if i + 1 < len(args) else None)
                if not inline:
                    i += 1
                if name == "--output":
                    output = value
                if name == "--url":
                    url = value
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for k, ch in enumerate(cluster):
                if ch == "O":
                    remote_name = True
                elif ch in CURL_VALUE_SHORT:
                    value = cluster[k + 1:] or (args[i + 1] if i + 1 < len(args) else None)
                    if not cluster[k + 1:]:
                        i += 1
                    if ch == "o":
                        output = value
                    if ch == "K":
                        return None
                    if ch in "dFT":
                        sends = True
                    break
            i += 1
            continue
        if re.match(r"https?://", a):
            if url:
                return None
            url = a
        i += 1
    if not url:
        return None
    return url, output, remote_name, sends


def _parse_wget(args):
    url, output = None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-O", "--output-document"):
            output = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--output-document="):
            output = a.split("=", 1)[1]
        elif a in ("-P", "--directory-prefix", "-i", "--input-file", "-r", "--recursive",
                   "-m", "--mirror", "--post-data", "--post-file", "-e", "--execute") \
                or a.startswith(("--directory-prefix=", "--input-file=", "--post-")):
            return None
        elif a.startswith("-O") and len(a) > 2:
            output = a[2:]
        elif re.match(r"https?://", a):
            if url:
                return None
            url = a
        i += 1
    return (url, output) if url else None


def _url_name(url):
    name = os.path.basename(urlparse(url).path)
    return name or "index.html"


def download_questions(seg, cwd, specs):
    if seg["prog"] not in ("curl", "wget"):
        return
    if seg["prog"] == "curl":
        parsed = _parse_curl(seg["args"])
        if not parsed:
            return
        url, output, remote_name, sends = parsed
        if sends:
            return
    else:
        parsed = _parse_wget(seg["args"])
        if not parsed:
            return
        url, output = parsed
        remote_name = False
    host = urlparse(url).hostname or url
    runner = seg["pipe_to"]["prog"] if seg["pipe_to"] else None
    stdout_redirect = next((t for op, t, fd in seg["redirects"]
                            if op in (">", ">|", "&>") and fd in (None, 1) and t and t != "/dev/null"), None)

    if runner:
        if runner not in INTERPRETERS or seg["pipe_to"]["redirects"]:
            return
        outcome = "run"
        saved_as = _url_name(url)
    else:
        if seg["prog"] == "curl":
            if output and output != "-":
                outcome, saved_as = "save", output
            elif remote_name:
                outcome, saved_as = "save", _url_name(url)
            elif stdout_redirect:
                outcome, saved_as = "save", stdout_redirect
            else:
                outcome, saved_as = "print", _url_name(url)
        else:
            if output == "-":
                outcome, saved_as = ("save", stdout_redirect) if stdout_redirect else ("print", _url_name(url))
            else:
                saved_as = output or _url_name(url)
                if not output and os.path.exists(os.path.join(cwd, saved_as)):
                    return   # wget would pick another name (name.1): too fiddly to state
                outcome = "save"
    shown = _shown(seg, 50) + (" | " + _shown(seg["pipe_to"], 20) if runner else "")
    texts = {
        "run": "It runs the script from `{}` right away, without saving or showing it".format(host),
        "save": "It saves the file from `{}` as `{}`; nothing runs".format(host, saved_as),
        "print": "It shows the file from `{}` in the terminal; nothing runs".format(host),
    }
    feedback = {
        ("run", "save"): "The `|` sends the download straight into `{}` instead of into a file, so it runs without ever being saved.".format(runner),
        ("run", "print"): "Nothing is shown to you: the `|` hands the text to `{}`, which runs it as a program.".format(runner),
        ("save", "run"): "Nothing runs it: the download only goes into a file. It would take another command, such as `bash {}`, to run it.".format(saved_as),
        ("save", "print"): "The output goes into the file `{}`, not onto the screen.".format(saved_as),
        ("print", "save"): "With no `-o`, `-O` or `>`, curl writes what it downloads to the screen, not to a file.",
        ("print", "run"): "Nothing runs it. It would take a `|` into a program such as `bash` to run the text.",
    }
    distractors = [(texts[o], feedback[(outcome, o)]) for o in ("run", "save", "print") if o != outcome]
    distractors.append((
        "Your antivirus scans it first, then {} runs or saves it".format(seg["prog"]),
        "`{}` just downloads. Nothing checks the file on the way, which is why piping a download into a shell is risky.".format(seg["prog"])))
    why = {
        "run": "`{}` downloads, and the `|` pipes the script straight into `{}`, which runs it with your permissions. Nobody reads it first.".format(seg["prog"], runner),
        "save": "The download goes into the file `{}`. Nothing runs until someone runs that file.".format(saved_as),
        "print": "Without an output file or a pipe, `{}` shows what it downloaded in the terminal.".format(seg["prog"]),
    }[outcome]
    _q(specs, "download", {"run": 10, "save": 5, "print": 3}[outcome], "action",
       "What does `{}` do with what it downloads?".format(shown),
       texts[outcome], why, distractors)
    if outcome == "run":
        _q(specs, "download-trust", 9, "security",
           "This command runs a script straight from `{}`. Who decides what that script does?".format(host),
           "Whoever controls `{}` when it runs; the script can change at any time".format(host),
           "The script is fetched fresh each time, so it's whatever `{}` serves right then. If that site or its account is hacked, so are you. Safer: download it, read it, then run it.".format(host),
           [("{}, which checked the script before proposing it".format(AGENT_CAP),
             "The agent only wrote the command. It can't see what the server will send later."),
            ("Nobody: scripts on well-known sites can't change",
             "Anyone who controls the site can change the file at any time."),
            ("Your computer's security settings, which approve it first",
             "Nothing approves a script piped into a shell: it runs with your permissions right away.")])


# ----------------------------------------------------------------- deletes

def delete_questions(seg, cwd, specs):
    if seg["prog"] != "rm":
        return
    recursive = False
    targets = []
    only_targets = False
    for a in seg["args"]:
        if only_targets or not a.startswith("-") or a == "-":
            targets.append(a)
            continue
        if a == "--":
            only_targets = True
            continue
        if a.startswith("--"):
            if a == "--recursive":
                recursive = True
            elif a in ("--force", "--verbose"):
                pass
            else:
                return
            continue
        for ch in a[1:]:
            if ch in "rR":
                recursive = True
            elif ch in "fv":
                pass
            else:
                return   # -i, -I, -d, -P ...: prompts or special cases
    if len(targets) != 1:
        return
    t = targets[0]
    if re.search(r"[*?\[\]{}$~]", t) and t not in ("~", "~/"):
        return
    if t in ("~", "~/"):
        path, label = os.path.expanduser("~"), "your whole home folder"
    else:
        path = os.path.normpath(os.path.join(cwd, t))
        label = "`{}`".format(t)
    if path == "/":
        return
    is_dir, is_file = os.path.isdir(path), os.path.isfile(path)
    if not (is_dir or is_file):
        return
    trash = ("It moves to the Trash, so you can get it back",
             "`rm` doesn't use the Trash. Files it deletes are gone unless you have a backup.")
    if is_dir and recursive:
        key = "{} and everything inside it are deleted for good; there's no undo".format(label[0].upper() + label[1:])
        why = "`-r` deletes a folder and everything in it, and `rm` never uses the Trash."
        distractors = [trash,
                       ("Only the files directly inside it; its subfolders stay",
                        "`-r` means recursive: subfolders and everything in them go too."),
                       ("Nothing, unless the folder is already empty",
                        "That's `rmdir`. With `-r`, `rm` deletes folders that still have files in them.")]
        prio = 10 if t in ("~", "~/", ".", "..") else 9
    elif is_dir:
        key = "Nothing: `rm` refuses to delete a folder unless you add `-r`"
        why = "Without `-r`, `rm` stops with \"is a directory\" and deletes nothing."
        distractors = [(label[0].upper() + label[1:] + " and everything inside it are deleted for good",
                        "That needs `-r`. Without it, `rm` refuses to touch folders."),
                       trash,
                       ("Only the files inside it are deleted; the empty folder stays",
                        "`rm` doesn't empty folders. Without `-r` it refuses and deletes nothing.")]
        prio = 4
    else:
        key = "{} is deleted for good; there's no undo".format(label[0].upper() + label[1:])
        why = "`rm` deletes files directly. It never uses the Trash."
        distractors = [trash,
                       ("Its contents are erased, but the empty file stays",
                        "`rm` removes the file itself. Emptying a file is what `> {}` would do.".format(t)),
                       ("It's hidden, and it comes back when you restart the computer",
                        "Deleted means deleted: nothing brings it back.")]
        prio = 8
    _q(specs, "delete", prio, "action", "What happens to {} when `{}` runs?".format(label, _shown(seg)),
       key, why, distractors)


# ----------------------------------------------------------------- redirection

def redirect_questions(seg, cwd, specs):
    for op, target, fd in seg["redirects"]:
        if op not in (">", ">|", ">>", "&>", "&>>") or fd not in (None, 1) or not target:
            continue
        if target.startswith(("/dev/", "&")):
            continue
        path = os.path.join(cwd, target)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, errors="replace") as f:
                n = sum(1 for _ in f)
        except OSError:
            continue
        if n == 0:
            continue
        lines_text = "{} line{}".format(n, "" if n == 1 else "s")
        erase = "They're erased, and the command's output replaces them"
        append = "They stay, and the command's output is added after them"
        if op in (">>", "&>>"):
            key, why = append, "`>>` appends: it adds to the end of the file and keeps what's there."
            distractors = [(erase, "That's a single `>`. The double `>>` keeps the old lines."),
                           ("Only the last line is replaced by the output",
                            "`>>` never replaces anything; it only adds."),
                           ("Nothing is written: `>>` compares two values",
                            "In the shell, `>>` sends output into a file. Comparisons are written differently.")]
            prio = 3
        else:
            key, why = erase, "A single `>` empties the file first, then writes the output into it. The shell doesn't ask."
            distractors = [(append, "That's `>>`. A single `>` wipes the file before writing."),
                           ("The shell asks before replacing them",
                            "The shell never asks: `>` empties the file immediately."),
                           ("Nothing is written: `>` compares two values",
                            "In the shell, `>` sends output into a file. It isn't a comparison.")]
            prio = 7
        _q(specs, "redirect", prio, "action",
           "`{}` has {} now. What happens to them when `{}` runs?".format(target, lines_text, _shown(seg)),
           key, why, distractors)
        return


# ----------------------------------------------------------------- installs

PIP_NOVALUE = {"-U", "--upgrade", "--user", "-q", "--quiet", "-v", "--verbose", "--no-deps", "--pre",
               "--force-reinstall", "--no-cache-dir", "--break-system-packages", "--no-input",
               "--disable-pip-version-check", "--no-color", "--isolated", "--require-virtualenv",
               "-I", "--ignore-installed", "--no-warn-script-location", "--compile", "--no-compile",
               "-qq", "-qqq", "-vv", "--system"}
NPM_NOVALUE = {"-D", "--save-dev", "-S", "--save", "-E", "--save-exact", "-O", "--save-optional",
               "--no-save", "--ignore-scripts", "--legacy-peer-deps", "--no-fund", "--no-audit",
               "--silent", "--quiet", "-g", "--global", "--dev", "-P", "--save-prod", "--exact"}
PKG_RE = re.compile(r"^(@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_,.-]+\])?"
                    r"((==|>=|<=|~=|!=|@)[A-Za-z0-9.*+!_^~<>=-]+)?$")


def _pip_index_configured():
    if any(os.environ.get(v) for v in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL",
                                         "UV_DEFAULT_INDEX", "UV_INDEX", "UV_EXTRA_INDEX_URL")):
        return True
    home = os.path.expanduser("~")
    places = [os.path.join(home, ".pip", "pip.conf"), os.path.join(home, ".config", "pip", "pip.conf"),
              os.path.join(home, "Library", "Application Support", "pip", "pip.conf"),
              "/etc/pip.conf", "/Library/Application Support/pip/pip.conf",
              os.path.join(home, ".config", "uv", "uv.toml")]
    if os.environ.get("VIRTUAL_ENV"):
        places.append(os.path.join(os.environ["VIRTUAL_ENV"], "pip.conf"))
    for p in places:
        try:
            with open(p, errors="replace") as f:
                if "index" in f.read():
                    return True
        except OSError:
            pass
    return False


def _npm_registry_configured(cwd):
    if os.environ.get("npm_config_registry") or os.environ.get("NPM_CONFIG_REGISTRY"):
        return True
    for p in (os.path.join(cwd, ".npmrc"), os.path.join(os.path.expanduser("~"), ".npmrc")):
        try:
            with open(p, errors="replace") as f:
                if "registry" in f.read():
                    return True
        except OSError:
            pass
    return False


def _install_target(seg):
    """Return (manager, packages, requirements_file, no_deps) for a recognized install,
    or None. packages=[] with requirements_file=None means 'everything in the manifest'."""
    words = seg["words"]
    if not words:
        return None
    prog = seg["prog"]
    args = seg["args"]
    if re.fullmatch(r"python(3(\.\d+)?)?", prog) and args[:2] == ["-m", "pip"]:
        prog, args = "pip", args[2:]
    if prog == "uv" and args[:1] == ["pip"]:
        prog, args = "pip", args[1:]
    elif prog == "uv" and args[:1] == ["add"]:
        prog, args = "uv-add", args[1:]
    if re.fullmatch(r"pip(3(\.\d+)?)?", prog):
        if args[:1] != ["install"]:
            return None
        args = args[1:]
        manager = "pip"
        novalue = PIP_NOVALUE
    elif prog == "uv-add":
        manager = "pip"
        novalue = PIP_NOVALUE | {"--dev"}
    elif prog in ("npm", "pnpm", "yarn", "bun"):
        if not args:
            if prog == "yarn":
                return "npm", [], None, False
            return None
        verb, args = args[0], args[1:]
        if prog == "npm" and verb not in ("install", "i", "add"):
            return None
        if prog in ("pnpm", "bun") and verb not in ("add", "install", "i"):
            return None
        if prog == "yarn" and verb not in ("add", "install"):
            return None
        manager = "npm"
        novalue = NPM_NOVALUE
    else:
        return None
    packages, req, no_deps = [], None, False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-r", "--requirement") and manager == "pip":
            if i + 1 >= len(args) or req:
                return None
            req = args[i + 1]
            i += 2
            continue
        if a.startswith("--requirement="):
            req = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("-"):
            if a not in novalue:
                return None
            if a == "--no-deps":
                no_deps = True
            i += 1
            continue
        if not PKG_RE.match(a):
            return None
        packages.append(a)
        i += 1
    if manager == "pip" and not packages and not req:
        return None
    return manager, packages, req, no_deps


def _pkg_name(spec):
    m = re.match(r"^(@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9._-]*", spec)
    return m.group(0) if m else spec


def install_questions(seg, cwd, specs):
    parsed = _install_target(seg)
    if not parsed:
        return
    manager, packages, req, no_deps = parsed
    names = [_pkg_name(p) for p in packages]
    shown = _shown(seg)
    if manager == "pip":
        if _pip_index_configured():
            return
        stable, version_dependent = load_stdlib_names()
        if any(n.lower().replace("-", "_") in stable | version_dependent for n in names):
            return
        index = "PyPI, Python's public package index, where anyone can publish"
        stdlib_wrong = ("Python's standard library, already on your computer",
                        "The standard library comes with Python and never needs pip. `pip install` fetches packages that aren't part of Python.")
    else:
        if _npm_registry_configured(cwd):
            return
        index = "The npm registry (npmjs.com), where anyone can publish"
        stdlib_wrong = ("Node.js itself, which already includes every package",
                        "Node.js ships with a few built-in modules. Everything you `npm install` is downloaded from the registry.")
    vetted = ("{}'s company, which vets each package first".format(AGENT_CAP),
              "The agent only typed the command. The package comes straight from the public registry, and nobody reviews packages before they're published there.")
    local = ("A copy that pip keeps inside this project folder",
             "A bare package name is looked up online. Local copies are only used when you give a path, like `.`.")
    if req:
        stem = "Where does `{}` get the packages listed in `{}` from?".format(shown, req)
    elif packages:
        listed = ", ".join("`{}`".format(n) for n in names[:3]) + (" and more" if len(names) > 3 else "")
        stem = "If {} {} installed yet, where does `{}` get {} from?".format(
            listed, "isn't" if len(names) == 1 else "aren't", shown, "it" if len(names) == 1 else "them")
    else:
        if not os.path.isfile(os.path.join(cwd, "package.json")):
            return
        stem = "Where does `{}` get the packages listed in `package.json` from?".format(shown)
    why = ("Anyone can publish to a public registry, and malicious packages often copy popular names "
           "with a small typo. Check each name's exact spelling before pressing 1.")
    _q(specs, "install-source", 7, "dependencies", stem, index, why, [stdlib_wrong, vetted, local])

    if manager == "pip" and packages and not req and not no_deps and len(names) == 1:
        name = names[0]
        _q(specs, "install-deps", 5, "dependencies",
           "Besides `{}`, what else can `{}` install?".format(name, shown),
           "The packages `{}` depends on, which pip installs automatically".format(name),
           "pip installs everything a package needs to work, unless you add `--no-deps`. That's more code, from more authors, than the one name you typed.",
           [("Nothing else: pip installs exactly the names you type",
             "pip also installs the package's dependencies, and their dependencies."),
            ("Every package on PyPI whose name starts with `{}`".format(name),
             "pip installs one exact name, plus what that package depends on."),
            ("The packages already listed in `requirements.txt`",
             "pip only reads `requirements.txt` when you pass it with `-r`.")])


# ----------------------------------------------------------------- permissions, sudo

PERM_WORDS = {7: "read, change and run it", 6: "read and change it", 5: "read and run it",
              4: "read it", 3: "change and run it", 2: "change it", 1: "run it", 0: "do nothing with it"}


def _describe_mode(mode):
    o, g, e = (int(c) for c in mode[-3:])
    if o == g == e:
        return "Everyone on this computer can {}".format(PERM_WORDS[o])
    return "You can {}; your group can {}; everyone else can {}".format(PERM_WORDS[o], PERM_WORDS[g], PERM_WORDS[e])


def chmod_questions(seg, cwd, specs):
    if seg["prog"] != "chmod":
        return
    args = [a for a in seg["args"] if a not in ("-R", "-v")]
    if len(args) != 2 or any(a.startswith("-") for a in args):
        return
    mode, target = args
    if re.fullmatch(r"0?[0-7]{3}", mode):
        mode = mode[-3:]
        key = _describe_mode(mode)
        others = [m for m in ("777", "755", "700", "644", "600", "750") if m != mode]
        distractors = []
        seen = {key}
        for m in others:
            text = _describe_mode(m)
            if text in seen:
                continue
            seen.add(text)
            distractors.append((text, "That would be `chmod {}`. Each digit is one audience (you, your group, everyone else): 4 = read, 2 = change, 1 = run, added up.".format(m)))
            if len(distractors) == 3:
                break
        why = "The three digits are you, your group, and everyone else. Each is 4 (read) + 2 (change) + 1 (run), so {} means: {}.".format(mode, key[0].lower() + key[1:])
        prio = 8 if mode[-1] in "2367" else 5
        _q(specs, "chmod", prio, "security",
           "After `{}`, who can do what with `{}`?".format(_shown(seg), target), key, why, distractors)
    elif mode in ("+x", "a+x", "u+x", "ugo+x"):
        _q(specs, "chmod", 4, "action", "What does `{}` change about `{}`?".format(_shown(seg), target),
           "It can now be run as a program, like `./{}`".format(os.path.basename(target)),
           "`+x` adds the \"execute\" permission, which lets the file be run directly.",
           [("It's locked so nobody can change it",
             "Locking would mean removing write permission (`-w`). `+x` adds permission to run."),
            ("It's deleted when the program exits",
             "chmod only changes permissions. It never deletes."),
            ("It's hidden from other users",
             "Hiding isn't a permission. `x` means execute.")])


def sudo_questions(seg, cwd, specs):
    if not seg.get("sudo") or not seg["words"]:
        return
    _q(specs, "sudo", 8, "security", "What does `sudo` change about `{}`?".format(_shown(seg)),
       "It runs as the administrator (root), so it can change system files far outside this project",
       "`sudo` runs the command with the highest privileges on the computer. A mistake or a malicious package can then reach everything.",
       [("It runs faster by skipping safety checks",
         "Speed doesn't change. What changes is *who* the command runs as: the administrator."),
        ("It runs in a protected sandbox, away from your files",
         "It's the opposite: it removes limits instead of adding them."),
        ("It only asks for your password; the command still runs as you",
         "After the password, the command runs as root, not as you.")])


# ----------------------------------------------------------------- git and setup

SENSITIVE_NAMES = {"credentials.json", "secrets.json", "service-account.json", ".npmrc", ".pypirc",
                   ".netrc", "secrets.yml", "secrets.yaml"}


def is_sensitive(path):
    base = os.path.basename(path)
    if base == ".env" or (base.startswith(".env.") and not base.endswith((".example", ".sample", ".template", ".dist"))):
        return True
    if base.endswith((".pem", ".key", ".p12", ".pfx")) or base.startswith(("id_rsa", "id_ed25519", "id_ecdsa")):
        return True
    return base in SENSITIVE_NAMES


def _display_remote(url):
    url = url.strip()
    m = re.match(r"^[^@\s]+@([^:\s]+):(.+)$", url)       # git@github.com:owner/repo.git
    if m and "://" not in url:
        return "{}/{}".format(m.group(1), m.group(2))
    p = urlparse(url)
    if p.hostname:
        return p.hostname + p.path
    return url


def git_questions(seg, cwd, specs):
    if seg["prog"] != "git" or not seg["args"]:
        return
    args = seg["args"]
    # git -C dir ... : work in that dir
    while args and args[0] in ("-C",) and len(args) > 2:
        cwd = os.path.normpath(os.path.join(cwd, args[1]))
        args = args[2:]
    if not args:
        return
    verb, rest = args[0], args[1:]
    shown = _shown(seg)
    if verb == "init":
        code, _ = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
        if code == 0 or code is None:
            return
        _q(specs, "git-init", 3, "action", "What does `{}` create?".format(shown),
           "A hidden `.git` folder that starts tracking this folder's history, only on your computer",
           "`git init` works locally. Nothing goes online until you add a remote and push.",
           [("A new repository on GitHub",
             "Git and GitHub are different things. `git init` never talks to GitHub."),
            ("A backup copy of every file, stored in the cloud",
             "It stores history in `.git` on this computer only."),
            ("A connection to a remote called `origin`",
             "A new repository has no remotes. `git remote add origin …` would create one.")])
    elif verb == "push":
        flags = [a for a in rest if a.startswith("-")]
        positional = [a for a in rest if not a.startswith("-")]
        force = any(f in ("-f", "--force", "--force-with-lease") or f.startswith("--force-with-lease=") for f in flags)
        remote = positional[0] if positional else "origin"
        code, out = _run_git(cwd, "remote", "get-url", remote)
        if code != 0 or not out.strip():
            return
        where = _display_remote(out)
        if force:
            _q(specs, "git-force", 8, "action", "What can `--force` make `{}` do?".format(shown),
               "Replace the history on `{}` with yours, deleting commits that exist only there".format(where),
               "A normal push refuses when the remote has commits you don't have. `--force` overrides that and can erase other people's work.",
               [("Push even when the internet connection is weak",
                 "`--force` has nothing to do with the network. It overrides git's safety check on history."),
                ("Upload your uncommitted changes too",
                 "Only commits are ever pushed, forced or not."),
                ("Nothing extra: it only hides warnings",
                 "It changes what happens: the remote's history is replaced.")])
        _q(specs, "git-push", 4, "action", "What does `{}` send, and where?".format(shown),
           "Your commits, to `{}`; changes you haven't committed stay on your computer".format(where),
           "`git push` uploads commits. Edits you haven't committed are never pushed.",
           [("Every file in this folder, including changes you haven't committed",
             "Only committed changes are pushed. Uncommitted edits stay local."),
            ("Nothing yet: pushing only asks `{}` for permission".format(where),
             "The upload happens right away, if the remote accepts it."),
            ("Only the names of the changed files, not their contents",
             "The full contents of your commits are uploaded.")])
    elif verb == "add":
        force = any(a in ("-f", "--force") for a in rest)
        targets = [a for a in rest if not a.startswith("-")]
        all_flag = any(a in ("-A", "--all") for a in rest)
        whole = all_flag or targets in (["."], ["*"], [":/"])
        if not whole and not targets:
            return
        rels = _secrets_to_stage(cwd, targets, whole, all_flag, force, seg.get("ctx", {}))
        if not rels:
            return
        rel = rels[0]
        if whole:
            key = "Yes: `.gitignore` doesn't exclude it, so it will be committed"
            why = "`git add .` stages everything under the folder that `.gitignore` doesn't exclude. Add `{}` to `.gitignore` first.".format(os.path.basename(rel))
            last = ("No: `git add .` adds code files only, not settings files",
                    "`.` means everything under this folder, whatever the file type.")
        else:
            key = "Yes: the command names it, so it goes into the next commit"
            why = "Naming a file in `git add` stages it. Once it's committed, it stays in the history even if you delete it later."
            last = ("No: git only adds code files, like `.py` or `.js`",
                    "Git adds any file you name, whatever its type.")
        _q(specs, "git-add-secret", 10, "security",
           "`{}` can hold passwords and API keys. Will `{}` include it?".format(rel, shown), key, why,
           [("No: git never adds hidden files, like ones starting with a dot",
             "Git treats dot-files like any other file. Only `.gitignore` keeps a file out."),
            ("No: git automatically leaves out files that contain secrets",
             "Git doesn't look inside files. Some hosting sites scan for leaked keys after a push, when it's already too late."),
            last])
    if verb == "init":
        code, _ = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
        if code != 0:
            seg.get("ctx", {})["git_init"] = True


def _ignored_fresh(cwd, rel):
    """Would a brand-new repository in cwd ignore rel? Uses cwd's .gitignore plus the
    user's global git excludes, through a throwaway repository."""
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", tmp], capture_output=True, timeout=5)
        gi = os.path.join(cwd, ".gitignore")
        if os.path.isfile(gi):
            shutil.copy(gi, os.path.join(tmp, ".gitignore"))
        r = subprocess.run(["git", "-C", tmp, "check-ignore", "-q", "--no-index", rel],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _secrets_to_stage(cwd, targets, whole, all_flag, force, ctx):
    """Sensitive files (like .env) that this `git add` would stage, relative to cwd.
    Empty when unsure: a .gitignore changed earlier in the same command, say."""
    if ctx.get("gitignore_touched"):
        return []
    code, top = _run_git(cwd, "rev-parse", "--show-toplevel")
    in_repo = code == 0
    if not in_repo and not ctx.get("git_init"):
        return []                    # git add would fail here: nothing gets staged
    found = []
    if whole:
        if force:
            return []
        if in_repo:
            code, out = _run_git(cwd, "status", "--porcelain", "--untracked-files=all")
            if code != 0:
                return []
            root = top.strip()
            for line in out.splitlines():
                path = line[3:].strip().strip('"')
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                full = os.path.join(root, path)
                if not all_flag and not os.path.abspath(full).startswith(os.path.abspath(cwd) + os.sep):
                    continue
                if is_sensitive(path):
                    found.append(os.path.relpath(full, cwd))
        else:
            for name in sorted(os.listdir(cwd)):
                if os.path.isfile(os.path.join(cwd, name)) and is_sensitive(name):
                    if _ignored_fresh(cwd, name) is False:
                        found.append(name)
        return found
    for t in targets:
        full = os.path.normpath(os.path.join(cwd, t))
        if not (os.path.isfile(full) and is_sensitive(full)):
            continue
        rel = os.path.relpath(full, cwd)
        if force:
            found.append(rel)
            continue
        if in_repo:
            tracked, _ = _run_git(cwd, "ls-files", "--error-unmatch", full)
            if tracked == 0:
                continue             # already in the history; staging adds nothing new to ask about
            ignored, _ = _run_git(cwd, "check-ignore", "-q", full)
            if ignored == 1:
                found.append(rel)
        elif os.sep not in rel and _ignored_fresh(cwd, rel) is False:
            found.append(rel)
    return found


def venv_questions(seg, cwd, specs):
    prog, args = seg["prog"], seg["args"]
    target = None
    if re.fullmatch(r"python(3(\.\d+)?)?", prog) and args[:2] == ["-m", "venv"]:
        rest = [a for a in args[2:] if not a.startswith("-")]
        target = rest[0] if len(rest) == 1 else None
    elif prog == "virtualenv":
        rest = [a for a in args if not a.startswith("-")]
        target = rest[0] if len(rest) == 1 else None
    if not target:
        return
    _q(specs, "venv", 3, "action", "What does `{}` create?".format(_shown(seg)),
       "A folder `{}` with a separate Python setup for this project, so packages install there instead of for the whole computer".format(target),
       "A virtual environment keeps each project's packages apart. It is not a security sandbox: code in it can still reach all your files.",
       [("A virtual machine that keeps the project's code away from your files",
         "A virtual environment only separates packages. Code running in it can still read and change your files."),
        ("A backup of the project stored in `{}`".format(target),
         "It holds a Python setup and packages, not a copy of your code."),
        ("A new GitHub repository named `{}`".format(target),
         "It's a local folder; nothing goes online.")])


SEGMENT_DETECTORS = [download_questions, delete_questions, redirect_questions, install_questions,
                     chmod_questions, sudo_questions, git_questions, venv_questions]


def _heredoc_run(command):
    """sh -c "$(curl ...)" style: a download substituted into a shell's command string."""
    m = re.search(r"(?:^|[\s;&|])(?:sudo\s+)?(?:\S*/)?(sh|bash|zsh)\s+-c\s+[\"']?\$\(\s*(curl|wget)\s[^)]*?(https?://[^\s)\"']+)", command)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def command_specs(command, cwd):
    specs = []
    sub = _heredoc_run(command)
    if sub:
        shell, fetcher, url = sub
        host = urlparse(url).hostname or url
        _q(specs, "download", 10, "action",
           "What does `{} -c \"$({} …)\"` do with what it downloads?".format(shell, fetcher),
           "It runs the script from `{}` right away, without saving or showing it".format(host),
           "`$( … )` puts the download into the command string, and `{} -c` runs that string as a program.".format(shell),
           [("It saves the file from `{}` as `{}`; nothing runs".format(host, _url_name(url)),
             "Nothing is saved: the text goes straight into `{} -c`, which runs it.".format(shell)),
            ("It shows the file from `{}` in the terminal; nothing runs".format(host),
             "Nothing is shown to you: the text is handed to `{}` and run.".format(shell)),
            ("Your antivirus scans it first, then {} runs or saves it".format(fetcher),
             "Nothing checks the file on the way.")])
        return specs
    segments = parse_command(command)
    if not segments:
        return specs
    ctx = {}
    for seg in segments:
        seg["ctx"] = ctx
        for inner in seg.get("procsubs") or []:
            inner_seg = _new_segment()
            inner_seg["argv"] = inner
            _normalize(inner_seg)
            if inner_seg["prog"] in ("curl", "wget") and seg["prog"] in INTERPRETERS:
                fake_runner = {"prog": seg["prog"], "redirects": [], "words": [seg["prog"]], "sudo": False}
                inner_seg["pipe_to"] = fake_runner
                download_questions(inner_seg, cwd, specs)
        for detect in SEGMENT_DETECTORS:
            detect(seg, cwd, specs)
        touched = [t for _, t, _ in seg["redirects"]] + seg["words"]
        if any(os.path.basename(t or "") == ".gitignore" for t in touched) and seg["words"][:2] != ["git", "add"]:
            ctx["gitignore_touched"] = True
    return specs


# ----------------------------------------------------------------- file edits

AGENT_CONFIG = re.compile(r"(^|/)\.claude/settings(\.local)?\.json$")
SHELL_RC = {".zshrc", ".bashrc"}


def _new_content(tool_name, tool_input, path):
    if tool_name == "Write":
        return tool_input.get("content")
    old = None
    try:
        with open(path, errors="replace") as f:
            old = f.read()
    except OSError:
        return None
    if tool_name == "Edit":
        o, n = tool_input.get("old_string"), tool_input.get("new_string")
        if o is None or n is None or o not in old:
            return None
        return old.replace(o, n) if tool_input.get("replace_all") else old.replace(o, n, 1)
    return None


def _read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _requirements_names(text):
    names = set()
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m:
            names.add(m.group(0).lower())
    return names


def _package_json_names(text):
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return None
    names = set()
    for k in ("dependencies", "devDependencies", "optionalDependencies"):
        if isinstance(data.get(k), dict):
            names |= set(data[k])
    return names


def _workflow_events(text):
    m = re.search(r"(?m)^(on|\"on\"|'on'):[ \t]*(.*)$", text or "")
    if not m:
        return None
    inline = m.group(2).strip()
    if inline and not inline.startswith("#"):
        inline = inline.strip("[]")
        events = [e.strip().strip("'\"") for e in inline.split(",") if e.strip()]
    else:
        events, indent = [], None
        for line in text[m.end():].splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):
                break
            width = len(line) - len(line.lstrip(" "))
            if indent is None:
                indent = width
            if width != indent:
                continue          # deeper lines belong to an event (branches:, cron:, ...)
            mm = re.match(r"^ +([a-z_]+):", line)
            if not mm:
                return None
            events.append(mm.group(1))
    known = {"push": "someone pushes commits", "pull_request": "a pull request is opened or updated",
             "schedule": "its schedule comes around", "workflow_dispatch": "someone starts it by hand on GitHub",
             "release": "a release is published", "issues": "an issue is opened or changed",
             "pull_request_target": "a pull request is opened or updated"}
    if not events or any(e not in known for e in events):
        return None
    return [known[e] for e in events]


def edit_specs(tool_name, tool_input, cwd):
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return []
    path = os.path.normpath(os.path.join(cwd, path))
    rel = os.path.relpath(path, cwd) if path.startswith(cwd + os.sep) else path
    base = os.path.basename(path)
    specs = []
    new = _new_content(tool_name, tool_input, path) if tool_name in ("Write", "Edit") else None
    old = _read(path)

    if is_sensitive(path):
        if base.startswith(".env"):
            key = "Keeping settings the app reads when it runs, often secrets like API keys and passwords"
            why = "`.env` files hold configuration the program loads at startup, which usually includes secret keys. They should never be shared or committed."
            distractors = [("Storing your editor's colors and fonts",
                            "Editor settings live in places like `.vscode/`. A `.env` file holds values the program reads, often secret ones."),
                           ("Recording the errors the app hits while it runs",
                            "That's a log file. The app reads `.env`; it doesn't write to it."),
                           ("Listing the packages the project needs",
                            "That's `requirements.txt` or `package.json`.")]
        else:
            key = "Holding a secret, such as a private key or a password, that proves who you are"
            why = "Whoever has this file can act as you (or as your server). It must never be shared or committed."
            distractors = [("Storing your editor's colors and fonts",
                            "This kind of file holds credentials, not editor settings."),
                           ("Recording the errors the app hits while it runs",
                            "That's a log file. This file holds a secret."),
                           ("Listing the packages the project needs",
                            "That's `requirements.txt` or `package.json`.")]
        _q(specs, "secret-file", 8, "security",
           "{} wants to {} `{}`. What is this kind of file for?".format(
               AGENT_CAP, "create" if old is None else "change", rel), key, why, distractors)
        code, _ = _run_git(os.path.dirname(path), "rev-parse", "--is-inside-work-tree")
        if code == 0:
            ignored, _ = _run_git(os.path.dirname(path), "check-ignore", "-q", path)
            if ignored == 1:
                _q(specs, "secret-ignored", 10, "security",
                   "Is `{}` kept out of your git commits?".format(rel),
                   "No: nothing in `.gitignore` excludes it, so `git add .` would put it in the next commit",
                   "Only `.gitignore` keeps a file out of git. Add `{}` to it before your next commit.".format(base),
                   [("Yes: git never commits hidden files (names starting with a dot)",
                     "Git treats dot-files like any other file."),
                    ("Yes: git skips files that contain secrets, automatically",
                     "Git never looks inside files to decide what to commit."),
                    ("Yes: `.env` files are saved only on your computer, never in git",
                     "Nothing about the name keeps it out. Only `.gitignore` does.")])
        return specs

    if base.startswith("requirements") and base.endswith(".txt") and new is not None:
        added = sorted(_requirements_names(new) - _requirements_names(old))
        if added:
            pkg = added[0]
            source = "" if _pip_index_configured() else " from PyPI"
            _q(specs, "manifest", 7, "dependencies",
               "This edit adds `{}` to `{}`. What does that change?".format(pkg, rel),
               "Anyone who sets up the project with `pip install -r {}` will download `{}`{}".format(rel, pkg, source),
               "`requirements.txt` is the project's shopping list: pip installs every line. Nothing is installed when the file is saved.",
               [("`{}` is installed right now, as soon as the file is saved".format(pkg),
                 "Saving the file installs nothing. Packages are installed when someone runs pip with this file."),
                ("Nothing: this file is only notes for people, and tools never read it",
                 "`pip install -r` reads it line by line and installs each package."),
                ("Python will download `{}` by itself the first time the code imports it".format(pkg),
                 "Python never downloads packages on its own. The import fails with ModuleNotFoundError until someone installs it.")])
    elif base == "package.json" and new is not None:
        before, after = _package_json_names(old), _package_json_names(new)
        if before is not None and after is not None:
            added = sorted(after - before)
            if added and not _npm_registry_configured(cwd):
                pkg = added[0]
                _q(specs, "manifest", 7, "dependencies",
                   "This edit adds `{}` to `{}`. What does that change?".format(pkg, rel),
                   "Anyone who runs `npm install` in this project will download `{}` from the npm registry".format(pkg),
                   "`package.json` lists what npm installs. Nothing is installed when the file is saved.",
                   [("`{}` is installed right now, as soon as the file is saved".format(pkg),
                     "Saving installs nothing. `npm install` does the downloading."),
                    ("Nothing: `package.json` only describes the project for people",
                     "`npm install` reads it and installs every listed package."),
                    ("Node downloads `{}` by itself the first time the code requires it".format(pkg),
                     "Node never downloads packages on its own. The import fails until someone installs it.")])
    elif re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", path) and new is not None:
        events = _workflow_events(new)
        if events:
            when = " or ".join(events)
            _q(specs, "ci", 6, "action", "When will the steps in `{}` run?".format(rel),
               "On GitHub's computers, automatically, whenever {}".format(when),
               "Workflow files run on GitHub's servers for the events listed under `on:`, with access to the repository's secrets.",
               [("Only when you run the file yourself, on your computer",
                 "Workflows run on GitHub, triggered by events, not by you running the file."),
                ("Every time you save a file in your editor",
                 "Saving doesn't reach GitHub. The events under `on:` do."),
                ("Once, when the file is first created, and never again",
                 "They run every time a listed event happens.")])
    elif AGENT_CONFIG.search(path.replace(os.sep, "/")) and new is not None:
        had = "--sp1" in (old or "")
        has = "--sp1" in new
        if had and not has:
            _q(specs, "agent-config", 10, "security", "Does this edit turn SP1 Gate off?",
               "Yes: it removes SP1 Gate's hooks, so the agent could edit and run things without these questions",
               "SP1 Gate runs through hooks listed in this file. Removing them switches the gate off.",
               [("No: SP1 Gate runs outside Claude Code, so settings can't affect it",
                 "The gate is started by hooks in this very file."),
                ("No: hooks can only be changed from the terminal, not by editing files",
                 "Hooks are just entries in this JSON file. Editing it changes them."),
                ("Only for other projects, not this one",
                 "This file is this project's settings.")])
        else:
            _q(specs, "agent-config", 7, "security", "What is `{}`?".format(rel),
               "Claude Code's settings for this project: what it may do without asking, and which hooks run",
               "Changing it can change what the agent is allowed to do on its own, including turning SP1 Gate off.",
               [("Your editor's color theme and fonts",
                 "Editor appearance lives elsewhere. This file controls the agent's permissions and hooks."),
                ("A list of the files the agent has already changed",
                 "The agent doesn't keep a change list here. This is its settings file."),
                ("Your Anthropic account's billing settings",
                 "Account settings live online, not in a project file.")])
    elif base == ".mcp.json" and new is not None:
        _q(specs, "mcp-config", 7, "security", "What does adding a server to `{}` give the agent?".format(rel),
           "New tools it can call; a local server's code runs on your computer when the agent uses them",
           "MCP servers are programs. A local one runs with your permissions whenever the agent calls its tools.",
           [("Only documentation it can read",
             "MCP servers provide tools that take actions, not just reading material."),
            ("A faster connection to the AI model",
             "MCP adds tools; it doesn't change the model connection."),
            ("Nothing until you restart your computer",
             "Clients load MCP servers when a session starts, not when the computer restarts.")])
    elif base in SHELL_RC and os.path.dirname(path) == os.path.expanduser("~"):
        _q(specs, "shell-rc", 8, "security", "When will the lines {} adds to `~/{}` run?".format(AGENT, base),
           "Every time you open a new terminal window",
           "`~/{}` runs at the start of every interactive shell, so whatever is in it runs again and again.".format(base),
           [("Only once, right after the file is saved",
             "Nothing runs when it's saved. It runs every time a new terminal starts."),
            ("Only when the agent runs a command",
             "It runs for every new terminal you open, agent or not."),
            ("Only when you restart the computer",
             "It runs for each new terminal window, not at startup.")])
    return specs


# ----------------------------------------------------------------- entry point

def questions_for_action(tool_name, tool_input, cwd, rng, limit=2):
    if tool_name == "Bash":
        specs = command_specs(tool_input.get("command") or "", cwd)
        summary = tool_input.get("command") or ""
    elif tool_name in ("Edit", "Write", "NotebookEdit"):
        specs = edit_specs(tool_name, tool_input, cwd)
        summary = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    else:
        return []
    specs.sort(key=lambda s: -s["priority"])
    out, seen = [], set()
    for s in specs:
        if s["qid"] in seen:
            continue
        seen.add(s["qid"])
        q = question("a-" + s["qid"], s["lens"], s["stem"], s["key"], s["why"], s["distractors"], rng,
                     {"action": summary})
        if q:
            out.append(q)
        if len(out) >= limit:
            break
    return out
