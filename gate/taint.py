"""Where can outside input end up? A small, conservative taint analysis for Python.

Sources: a function's parameters (the caller controls them), `input()`, `sys.argv`,
`argparse` results (`args = parser.parse_args()`), and web request data (`request.args`,
`request.form`, flask-restful's `reqparse`, ...). An argparse option declared with `type=int`,
`type=float` (or a function or lambda that returns `int(...)`), `choices=`, or a flag action
can't carry text, so it is clean; so is an attribute reassigned through a sanitizer
(`args.port = int(args.port)`). `parse_args([...])` with a fixed list isn't the user's input.

Sinks, each a weakness class in MITRE's CWE list:
- shell commands: `os.system`, `os.popen`, `subprocess.getoutput`, `shell=True` (CWE-78).
  With `shell=True` and a list, only the list's first item is the command string.
- code evaluation: `eval`, `exec` (CWE-95)
- SQL text built from strings, recognizably SQL: `cursor.execute(f"SELECT ... {name}")` (CWE-89)
- file paths under a fixed folder (CWE-22): `open("notes/" + name)`, `os.remove(os.path.join(BASE,
  name))`, `(NOTES / name).read_text()`, `upload.save(os.path.join(UPLOADS, upload.filename))`.
  Only for input straight from a user, and only when the code joins it to a folder: a helper
  that opens whatever path its caller passes in, or a tool that opens the file you name, is
  ordinary code, not a finding. One level of helper is followed: when `main()` passes
  `sys.argv[1]` to `read_note(name)`, and `read_note` joins `name` to a folder and opens it,
  that is a finding, with the data flow running through the call.
  The effect is read from the code: read, overwrite (`"w"`), add to (`"a"`), create (`"x"`),
  change (`"r+"`), delete, or delete a folder tree. A mode the code computes is just "open".
  pathlib methods count only on a real Path (built with `Path(...)`, `/` or `joinpath`).

Conservative on purpose: a finding becomes the answer key of a question, so false alarms are
worse than misses.
- A value that goes through int()/float()/len()/shlex.quote(), `secure_filename`, `slugify`,
  a date or UUID parser, or a filter that keeps only letters and digits is clean.
- For file paths, a value that can no longer hold "/" can't climb folders: `os.path.basename`,
  `Path(x).name` / `.stem`, `x.split("/")[-1]`, `x.replace("/", "_")`, `re.sub(r"[^a-z0-9.-]", ...)`.
  It can still be "..", so deleting a whole folder tree still counts; without "." too
  (`re.sub(r"[^a-z0-9-]", ...)`, `safe_join`) it can't leave the folder at all.
- Every variable the value passes through counts. If any of them, or a value computed from
  them (`ok = re.fullmatch(..., host)`), is tested in a way that could limit what it holds, the
  finding is dropped: an allowlist, a pattern, `isalnum()`, `== "ls"` around the sink, a
  `match`, a `relative_to` inside a `try` whose handler exits, or a call to a validator (a
  function named like one, or one defined here that can raise or exit). Tests that can't limit
  it don't count, judged by where the sink is:
    `if not name: break`, `len(name) > 40`        emptiness and length say nothing about content
    `if name == "q": break` then the sink          afterwards `name` is anything but "q"
    `if name in seen: continue` then the sink      afterwards `name` is anything not in `seen`
    `while cmd != "quit": os.system(cmd)`          inside the loop `cmd` is anything but "quit"
  The same tests do limit a sink placed where only the matching value can arrive (after a
  `while True` loop that ends with that `break`, or after `while cmd not in ALLOWED: ...`).
- Checking that a file exists (`os.path.isfile(path)`, `path.exists()`) says nothing about where
  a path points, so for paths it isn't validation. `if os.path.exists(path): sys.exit()` does
  mean only new files get past it: reading or deleting is then impossible, writing only creates.
- An argparse namespace is tracked per option: testing `args.verbose` says nothing about
  `args.host`. For any other object, testing one attribute counts as testing the object.
"""
import ast
import copy
import re
import string

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
MODEL_CALLS = ("messages.create", "chat.completions.create", "responses.create", "generate_content")
SHELL_ALWAYS = ("os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput")
SHELL_IF_FLAG = ("subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_output",
                 "subprocess.check_call")
CODE_SINKS = ("eval", "exec")
SQL_METHODS = ("execute", "executemany", "executescript")
SQL_WORDS = re.compile(r"(?i)\b(select|insert|update|delete|create|drop|alter|replace|where|values|from|into|join)\b")
# file sinks: call -> effect. "open" means the mode argument decides (its position is in OPEN_MODE_ARG).
PATH_SINKS = {"open": "open", "io.open": "open", "codecs.open": "open", "gzip.open": "open", "bz2.open": "open",
              "lzma.open": "open", "tarfile.open": "open", "zipfile.ZipFile": "open", "ZipFile": "open",
              "send_file": "read", "flask.send_file": "read", "Image.open": "read_image", "PIL.Image.open": "read_image",
              "os.open": "access", "os.remove": "delete", "os.unlink": "delete", "shutil.rmtree": "delete_tree"}
# pathlib methods, called on the path itself: `(NOTES / name).read_text()`
PATH_METHODS = {"read_text": "read", "read_bytes": "read", "write_text": "overwrite", "write_bytes": "overwrite",
                "open": "open", "unlink": "delete"}
SAVE_METHODS = ("save",)          # `upload.save(path)`: Flask's FileStorage, Pillow images, workbooks
# `.save(path)` writes a file only on some objects: an upload from `request.files`, or one built by these
FILE_SAVERS = ("Workbook", "load_workbook", "Image.open", "Image.new", "Image.fromarray", "Document", "Presentation")
PATH_PRESERVING = ("resolve", "absolute", "expanduser", "with_suffix", "with_name", "with_stem")
EXISTENCE_CALLS = ("os.path.exists", "os.path.isfile", "os.path.isdir", "os.path.lexists", "os.path.islink")
EXISTENCE_METHODS = ("exists", "is_file", "is_dir", "is_symlink")
ANYTHING_THERE = ("os.path.exists", "os.path.lexists", "exists")      # true for files and folders alike
ARGPARSE_CALLS = ("parse_args", "parse_known_args")
FLAG_ACTIONS = ("store_true", "store_false", "store_const", "append_const", "count", "version", "help")
JOINS = ("os.path.join", "posixpath.join", "Path", "pathlib.Path", "PurePath")
SANITIZERS = ("int", "float", "bool", "len", "abs", "round", "hash", "id", "ord",
              "secure_filename", "werkzeug.utils.secure_filename", "uuid.UUID", "UUID", "decimal.Decimal", "Decimal")
SHELL_SANITIZERS = ("shlex.quote", "pipes.quote", "shlex.join")    # safe for a shell, not for a path or SQL
CHAR_FILTERS = ("str.isalnum", "str.isalpha", "str.isdigit", "str.isdecimal", "str.isnumeric")
PARSED = (".strptime", ".fromisoformat", ".fromtimestamp")
PATH_TYPES = ("Path", "pathlib.Path", "PurePath", "PurePosixPath", "pathlib.PurePath", "pathlib.PurePosixPath")
PATH_SANITIZERS = ("os.path.basename", "posixpath.basename")       # no "/" left, but ".." survives
PATH_SAFE = ("safe_join", "werkzeug.utils.safe_join", "werkzeug.security.safe_join", "flask.safe_join")
REQUEST_ATTRS = ("args", "form", "values", "json", "files", "cookies", "headers", "data",
                 "GET", "POST", "get_json", "query_params")
NORMALIZERS = ("lower", "upper", "strip", "lstrip", "rstrip", "casefold", "title")
VALIDATOR_NAME = re.compile(r"(?i)(valid|check|verify|saniti|ensure|assert|allow|guard|require|clean)")
LOGGING = ("print", "pprint", "pprint.pprint", "warnings.warn", "click.echo", "sys.stderr.write", "sys.stdout.write",
           "logging.debug", "logging.info", "logging.warning", "logging.error", "logging.exception", "logging.critical",
           "logger.debug", "logger.info", "logger.warning", "logger.error", "logger.exception", "logger.critical",
           "log.debug", "log.info", "log.warning", "log.error", "log.exception", "console.print")
EXIT_CALLS = ("sys.exit", "exit", "quit", "os._exit", "abort", "flask.abort")
IS_FILE = ("os.path.isfile", "is_file")          # true only for regular files (following links)
STRING_SETS = {"string." + k: getattr(string, k) for k in ("ascii_letters", "ascii_lowercase", "ascii_uppercase",
                                                          "digits", "hexdigits", "octdigits", "punctuation", "whitespace")}
SHELL_META = set(";|&$`'\"\\<>(){}*?[]!~#\n\r\t ")
WORD_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = dotted(node.value)
        return inner + "." + node.attr if inner else ""
    return ""


def _str_constant(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _constant_tables(tree):
    """Module-level dicts of constants that nothing ever changes: looking a value up in
    one can only produce one of its constants (an allowlist)."""
    tables = {
        n.targets[0].id for n in tree.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Dict)
        and all(isinstance(v, (ast.Constant, ast.List, ast.Tuple)) for v in n.value.values)
    }
    mutators = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            tables -= set(n.names)
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) and isinstance(n.value, ast.Name):
            tables.discard(n.value.id)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in mutators \
                and isinstance(n.func.value, ast.Name):
            tables.discard(n.func.value.id)
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and n not in tree.body:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    tables.discard(t.id)
    bindings = [t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)]
    return {name for name in tables if bindings.count(name) == 1}


def _bindings(scope, name):
    """Every value bound to `name` in scope (None where the value isn't a plain expression)."""
    out = []
    for n in ast.walk(scope):
        if isinstance(n, ast.Assign):
            if any(_stores(tg, name) for tg in n.targets):
                out.append(n.value if any(isinstance(tg, ast.Name) and tg.id == name for tg in n.targets) else None)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.NamedExpr)):
            if _stores(n.target, name):
                out.append(n.value if isinstance(n, (ast.AnnAssign, ast.NamedExpr)) else None)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None and _stores(item.optional_vars, name):
                    out.append(None)
    return out


def _stores(target, name):
    return any(isinstance(t, ast.Name) and t.id == name and isinstance(t.ctx, ast.Store) for t in ast.walk(target))


def _numeric_function(fn):
    """A function whose every return is int(...) or float(...), directly or through one variable:
    as an argparse `type=`, it can't hand back text."""
    rets = [r.value for r in ast.walk(fn) if isinstance(r, ast.Return)]
    if not rets:
        return False
    for v in rets:
        if isinstance(v, ast.Call) and dotted(v.func) in ("int", "float"):
            continue
        if isinstance(v, ast.Name):
            vals = _bindings(fn, v.id)
            if vals and all(isinstance(x, ast.Call) and dotted(x.func) in ("int", "float") for x in vals):
                continue
        return False
    return True


def _typed_options(tree, clean_functions=None):
    """argparse options whose values can't carry text: `type=int`/`type=float` (or a function or
    lambda that returns int(...)), a fixed `choices=` list, or a flag action. Returns their names."""
    funcs = {n.name: n for n in tree.body if isinstance(n, FUNCS)}
    out = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument"):
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        t = kw.get("type")
        typed = t is not None and (
            dotted(t) in ("int", "float")
            or (isinstance(t, ast.Lambda) and isinstance(t.body, ast.Call) and dotted(t.body.func) in ("int", "float"))
            or (dotted(t) in funcs and _numeric_function(funcs[dotted(t)]))
            or (clean_functions or {}).get(dotted(t)) == "clean")      # a type= that checks the text
        a = kw.get("action")
        flag = a is not None and (_str_constant(a) in FLAG_ACTIONS or dotted(a).endswith("BooleanOptionalAction"))
        if not (typed or flag or "choices" in kw):
            continue
        dest = _str_constant(kw["dest"]) if "dest" in kw else None
        if dest is None:
            names = [_str_constant(x) for x in n.args if _str_constant(x)]
            longs = [x for x in names if x.startswith("--")]
            pick = longs[0] if longs else (names[0] if names else None)
            if pick:
                dest = pick.lstrip("-").replace("-", "_")
        if dest:
            out.add(dest)
    return out


def _module_context(tree):
    """Facts about the whole file that every scope's scan uses."""
    funcs = [n for n in tree.body if isinstance(n, FUNCS)]
    by_name = {n.name: n for n in funcs}
    arg_functions = set()
    for fn in funcs:
        for r in (x for x in ast.walk(fn) if isinstance(x, ast.Return) and isinstance(x.value, ast.Call)):
            if isinstance(r.value.func, ast.Attribute) and r.value.func.attr in ARGPARSE_CALLS:
                arg_functions.add(fn.name)
    stores = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            stores[n.id] = stores.get(n.id, 0) + 1
    web_parsers, non_fs, compiled, consts, parsers = set(), set(), {}, {}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            d = dotted(n.value.func)
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
            if d.endswith("RequestParser"):
                web_parsers.update(names)
            if d.endswith("ArgumentParser"):
                parsers.update(names)
            if d == "zipfile.Path" or d.endswith(".zipfile.Path"):
                non_fs.update(names)
            if d == "re.compile" and n.value.args and _str_constant(n.value.args[0]) is not None:
                for name in names:
                    if stores.get(name) == 1:          # one pattern per name in the whole file
                        compiled[name] = _str_constant(n.value.args[0])
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and stores.get(n.targets[0].id) == 1:
            consts[n.targets[0].id] = n.value          # PATTERN = r"[^a-z0-9]", SAFE_CHARS = "..."
    # calls that end the program: sys.exit(), parser.error(), and helpers like `def die(m): ...; sys.exit(1)`
    exit_names = {p + ".error" for p in parsers} | {p + ".exit" for p in parsers}
    changed = True
    while changed:
        changed = False
        for fn in funcs:
            if fn.name not in exit_names and fn.body and _ends_program(fn.body[-1], exit_names):
                exit_names.add(fn.name)
                changed = True
    # functions that can stop the program, so calling one can be a check (`validate_name(x)`)
    stopping, changed = set(), True
    while changed:
        changed = False
        for fn in funcs:
            if fn.name in stopping:
                continue
            for x in ast.walk(fn):
                if isinstance(x, (ast.Raise, ast.Assert)) or (isinstance(x, ast.Call) and (
                        _is_exit_call(x, exit_names) or dotted(x.func) in stopping)):
                    stopping.add(fn.name)
                    changed = True
                    break
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            imported |= {(a.asname or a.name).split(".")[0] for a in n.names}
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    names = [n.name for n in funcs]
    simple_returns = {}
    for fn in funcs:
        body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and _str_constant(fn.body[0].value) \
            else fn.body
        if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None \
                and not fn.decorator_list and names.count(fn.name) == 1 and not fn.args.vararg and not fn.args.kwarg:
            simple_returns[fn.name] = (fn, body[0].value)     # `def note_path(n): return NOTES / n`
    class_patterns = {}
    for c in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for b in c.body:
            if isinstance(b, ast.Assign) and len(b.targets) == 1 and isinstance(b.targets[0], ast.Name) \
                    and isinstance(b.value, ast.Call) and dotted(b.value.func) == "re.compile" and b.value.args \
                    and _str_constant(b.value.args[0]) is not None:
                key = b.targets[0].id
                class_patterns[key] = None if key in class_patterns else _str_constant(b.value.args[0])
    return {"typed_options": _typed_options(tree), "arg_functions": arg_functions, "stopping": stopping,
            "web_parsers": web_parsers, "non_fs": non_fs, "compiled": compiled, "consts": consts,
            "module_functions": set(by_name), "tree": tree, "callees": {}, "clean_functions": {},
            "exit_names": exit_names, "imported": imported, "classes": classes, "class_patterns": class_patterns,
            "simple_returns": simple_returns}


def _is_exit_call(call, exit_names=()):
    """sys.exit(), exit(), abort(...), parser.error(...), or a helper here that always ends the program."""
    d = dotted(call.func)
    return d in EXIT_CALLS or d in exit_names or (isinstance(call.func, ast.Attribute) and call.func.attr == "error"
                                                   and "pars" in dotted(call.func.value).lower())


def _ends_program(stmt, exit_names=()):
    return isinstance(stmt, ast.Raise) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                                           and _is_exit_call(stmt.value, exit_names))


def _always_leaves(body, exit_names=()):
    """A block whose last statement leaves: return (any value), raise, break, continue, exit()."""
    if not body:
        return False
    last = body[-1]
    return isinstance(last, (ast.Return, ast.Raise, ast.Break, ast.Continue)) or _ends_program(last, exit_names)


def _source_call(node, ctx=None):
    """input(), sys.argv[...], parser.parse_args(), request.args[...] / request.form.get(...) and friends."""
    ctx = ctx or {}
    d = dotted(node.func) if isinstance(node, ast.Call) else dotted(node)
    if d == "input":
        return "`input()` reads whatever the user types"
    if d.startswith("sys.argv"):
        return "`sys.argv` holds the command-line arguments"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ARGPARSE_CALLS:
        if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            return None                                # parse_args(["--x", "1"]): a fixed list
        receiver = dotted(node.func.value)
        if receiver in ctx.get("web_parsers", ()) or "reqparse" in receiver:
            return "`{}()` reads the web request".format(d)
        return "`{}()` returns the command-line arguments".format(d)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and d in ctx.get("arg_functions", ()):
        return "`{}()` returns the command-line arguments".format(d)
    parts = d.split(".")
    if len(parts) >= 2 and parts[0] == "request" and parts[1] in REQUEST_ATTRS:
        return "`request.{}` comes from the web request".format(parts[1])
    return None


# ----------------------------------------------------------------- values that can't carry a payload

LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGITS = set("0123456789")
CHAR_TESTS = {"isalnum": LETTERS | DIGITS, "isalpha": LETTERS, "isdigit": DIGITS, "isdecimal": DIGITS,
              "isnumeric": DIGITS}


def _class_chars(body):
    """The characters a regex character class allows: single characters plus the markers
    "\\w", "\\d", "\\s" and their opposites "\\W", "\\D", "\\S". None when it can't be read simply."""
    items = re.findall(r"\\.|.", body, re.S)
    out, i = set(), 0
    while i < len(items):
        it = items[i]
        if i + 2 < len(items) and items[i + 1] == "-":
            lo, hi = items[i], items[i + 2]
            if lo.startswith("\\") or hi.startswith("\\") or ord(hi) < ord(lo):
                return None
            out |= {chr(c) for c in range(ord(lo), ord(hi) + 1)}
            i += 3
            continue
        if it.startswith("\\"):
            e = it[1]
            if e in "wdsWDS":
                out.add("\\" + e)
            elif e.isalnum():
                return None                            # \x41, \b, \u...: don't guess
            else:
                out.add(e)                             # \. \- \/ \\ stand for themselves
        else:
            out.add(it)
        i += 1
    return out


def _char_flags(allowed):
    """For a value made only of `allowed` characters: "clean" if that makes it harmless everywhere,
    else the flags it earns (path_clean: no "/"; path_safe: no "/" and no "."; shell_safe)."""
    markers = {x for x in allowed if len(x) > 1}
    chars = {x for x in allowed if len(x) == 1}
    if markers - {"\\w", "\\d", "\\s"}:
        return frozenset()                             # \W and friends: almost anything
    if chars <= (LETTERS | DIGITS | {"_"}) and markers <= {"\\w", "\\d"}:
        return "clean"
    flags = set()
    if "/" not in chars:
        flags.add("path_clean")
        if "." not in chars:
            flags.add("path_safe")
    if not (chars & SHELL_META) and "\\s" not in markers:
        flags.add("shell_safe")
    return frozenset(flags)


def _sub_flags(pattern, repl):
    r"""What `re.sub(pattern, repl, x)` guarantees about its result, when the pattern is one character
    class, `\W` or `\D`, or a lone "/": "clean", a set of flags, or None."""
    if pattern is None or repl is None or "\\" in repl:
        return None
    if pattern in ("/", "\\/"):
        return None if "/" in repl else frozenset({"path_clean"})
    m = re.fullmatch(r"\\([WD])[+*]?", pattern)
    if m:
        return _char_flags({"\\" + m.group(1).lower()} | set(repl))     # everything but \w (or \d) is replaced
    m = re.fullmatch(r"\[(\^?)((?:\\.|[^\]\\])+)\][+*]?", pattern)
    if not m:
        return None
    chars = _class_chars(m.group(2))
    if chars is None:
        return None
    if m.group(1):                                     # [^...]: only the class's characters remain
        return _char_flags(chars | set(repl))
    if "\\W" in chars:                                 # [\W_]: only word characters remain
        return _char_flags({"\\w"} | set(repl))
    if "\\D" in chars:
        return _char_flags({"\\d"} | set(repl))
    if "/" in chars and "/" not in repl:               # [/\\]: every "/" is replaced
        return frozenset({"path_clean", "path_safe"} if "." in chars and "." not in repl else {"path_clean"})
    return None


def _const_chars(expr, resolve, depth=0):
    """The characters a constant string expression can contain: "abc", string.digits,
    "-_.() %s%s" % (string.ascii_letters, string.digits), a name bound to one of those. None if unknown."""
    if depth > 4 or expr is None:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return set(expr.value)
    if isinstance(expr, ast.Attribute) and dotted(expr) in STRING_SETS:
        return set(STRING_SETS[dotted(expr)])
    if isinstance(expr, ast.Name):
        return _const_chars(resolve(expr.id), resolve, depth + 1)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        a, b = _const_chars(expr.left, resolve, depth + 1), _const_chars(expr.right, resolve, depth + 1)
        return None if a is None or b is None else a | b
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod) and _str_constant(expr.left) is not None:
        parts = expr.right.elts if isinstance(expr.right, ast.Tuple) else [expr.right]
        sets = [_const_chars(x, resolve, depth + 1) for x in parts]
        if any(x is None for x in sets):
            return None
        return set(re.sub(r"%[sr]", "", _str_constant(expr.left))).union(*sets)
    if isinstance(expr, (ast.Set, ast.Tuple, ast.List)):
        sets = [_const_chars(x, resolve, depth + 1) for x in expr.elts]
        return None if any(x is None for x in sets) else set().union(*sets)
    if isinstance(expr, ast.JoinedStr):
        out = set()
        for v in expr.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out |= set(v.value)
            elif isinstance(v, ast.FormattedValue) and v.conversion == -1 and v.format_spec is None:
                part = _const_chars(v.value, resolve, depth + 1)
                if part is None:
                    return None
                out |= part
            else:
                return None
        return out
    if isinstance(expr, ast.Call) and dotted(expr.func) in ("set", "frozenset", "list", "tuple", "str") \
            and len(expr.args) == 1:
        return _const_chars(expr.args[0], resolve, depth + 1)
    return None


def _cond_chars(t, c, resolve):
    """The characters `t` lets through, as a test on the single character `c`; None if unknown."""
    if isinstance(t, ast.BoolOp):
        parts = [_cond_chars(v, c, resolve) for v in t.values]
        if isinstance(t.op, ast.Or):
            return None if any(p is None for p in parts) else set().union(*parts)
        known = [p for p in parts if p is not None]
        return set.intersection(*known) if known else None
    if isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute) and t.func.attr in CHAR_TESTS \
            and dotted(t.func.value) == c and not t.args:
        return set(CHAR_TESTS[t.func.attr])
    if isinstance(t, ast.Compare) and len(t.ops) == 1 and dotted(t.left) == c:
        if isinstance(t.ops[0], ast.In):
            return _const_chars(t.comparators[0], resolve)
        if isinstance(t.ops[0], ast.Eq) and _str_constant(t.comparators[0]) is not None:
            return set(_str_constant(t.comparators[0]))
    return None


def _is_char(x, c):
    """`c`, or `c.lower()` / `c.upper()`: case doesn't change what kind of character it is."""
    if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr in ("lower", "upper") and not x.args:
        x = x.func.value
    return dotted(x) == c


def _blocked_flags(blocked, repl):
    """Characters in `blocked` are all replaced by `repl`: what can't be in the result any more."""
    gone = set(blocked) - set(repl)
    flags = set()
    if "/" in gone:
        flags.add("path_clean")
        if "." in gone:
            flags.add("path_safe")
    if SHELL_META <= gone:
        flags.add("shell_safe")
    return frozenset(flags) if flags else None


def _ifexp_flags(elt, c, resolve):
    """`c if c.isalnum() else "_"` (a keep-list) or `"_" if c in BAD else c` (a block-list)."""
    test, body, orelse = elt.test, elt.body, elt.orelse
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, body, orelse = test.operand, orelse, body
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.NotIn) \
            and dotted(test.left) == c:
        test = ast.Compare(left=test.left, ops=[ast.In()], comparators=test.comparators)
        body, orelse = orelse, body                    # `c if c not in BAD else "_"` is `"_" if c in BAD else c`
    if _is_char(body, c) and _str_constant(orelse) is not None:
        kept = _cond_chars(test, c, resolve)
        return _char_flags(kept | set(_str_constant(orelse))) if kept is not None else None
    if _is_char(orelse, c) and _str_constant(body) is not None:
        blocked = _cond_chars(test, c, resolve)
        return _blocked_flags(blocked, _str_constant(body)) if blocked is not None else None
    return None


def _filter_flags(gen, resolve):
    """`"".join(c for c in name if c.isalnum() or c in "-_")`, `"".join(c if c.isalnum() else "_" for c in name)`:
    what the kept characters guarantee ("clean", a set of flags), or None."""
    if not isinstance(gen, (ast.GeneratorExp, ast.ListComp)) or len(gen.generators) != 1:
        return None
    g = gen.generators[0]
    if not isinstance(g.target, ast.Name):
        return None
    c, elt = g.target.id, gen.elt
    allowed = None
    if isinstance(elt, ast.IfExp):
        return _ifexp_flags(elt, c, resolve)
    if _is_char(elt, c):
        for cond in g.ifs:                             # every `if` must hold, so each one bounds the result
            a = _cond_chars(cond, c, resolve)
            if a is not None:
                allowed = a if allowed is None else allowed & a
    return _char_flags(allowed) if allowed is not None else None


def _leftmost(e):
    """The start of a path expression: NOTES in `(NOTES / a).resolve() / b`."""
    while True:
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Div):
            e = e.left
        elif isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr in PATH_PRESERVING + ("joinpath",):
            e = e.func.value
        elif isinstance(e, ast.Attribute) and e.attr == "parent":
            e = e.value
        else:
            return e


def _is_file_system(e, non_fs):
    """False for paths inside an archive (`zipfile.Path`), which `..` can't leave."""
    e = _leftmost(e)
    if isinstance(e, ast.Call):
        d = dotted(e.func)
        return not (d == "zipfile.Path" or d.endswith(".zipfile.Path"))
    if isinstance(e, ast.Name):
        return e.id not in non_fs
    return True


def _with_flags(o, *flags, drop=()):
    if o is None:
        return None
    return o[0], o[1], (o[2] - frozenset(drop)) | frozenset(flags), o[3]


# ----------------------------------------------------------------- string-built SQL

def _sql_text(expr):
    """The constant text of an f-string / concatenation / %-format / .format() expression, or None."""
    if isinstance(expr, ast.JoinedStr):
        return "".join(v.value for v in expr.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        parts = []
        for n in ast.walk(expr):
            s = _str_constant(n)
            if s:
                parts.append(s)
        return " ".join(parts) if parts else None
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "format":
        return _str_constant(expr.func.value)
    return None


def _flatten_add(expr):
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _flatten_add(expr.left) + _flatten_add(expr.right)
    return [expr]


def _sql_position(expr, is_tainted):
    """Where the first tainted piece sits in the SQL text: inside quotes ("value"), after an
    operator such as `=` ("value"), or as a table/column name ("identifier")."""
    before = None
    if isinstance(expr, ast.JoinedStr):
        text = ""
        for v in expr.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                text += v.value
            elif isinstance(v, ast.FormattedValue) and is_tainted(v.value):
                before = text
                break
            else:
                text += "x"
    elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        text = ""
        for part in _flatten_add(expr):
            s = _str_constant(part)
            if s is not None:
                text += s
            elif is_tainted(part):
                before = text
                break
            else:
                text += "x"
    elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod) and _str_constant(expr.left):
        t = _str_constant(expr.left)
        args = expr.right.elts if isinstance(expr.right, ast.Tuple) else [expr.right]
        k = next((i for i, a in enumerate(args) if is_tainted(a)), None)
        spots = [mm.start() for mm in re.finditer(r"%[sdifr]", t)]
        before = t[:spots[k]] if k is not None and k < len(spots) else None
    elif isinstance(expr, ast.Call) and _str_constant(getattr(expr.func, "value", None)):
        t = _str_constant(expr.func.value)
        k = next((i for i, a in enumerate(expr.args) if is_tainted(a)), None)
        spots = [mm.start() for mm in re.finditer(r"\{[^{}]*\}", t)]
        before = t[:spots[k]] if k is not None and k < len(spots) else None
    if before is None:
        return None
    b = before.rstrip().rstrip("%")
    if b.endswith(("'", '"')):
        return "value"
    if re.search(r"(?i)(=|<>|!=|<|>|\blike|\bin\s*\(|\bvalues\s*\(|,|\blimit|\boffset|\bbetween|\band)\s*$", b):
        return "value"
    return "identifier"


# ----------------------------------------------------------------- which tests count as checks

def _exits_only(body, tainted_names=(), exit_names=()):
    """A block that only leaves: optional prints, then break/continue/return/raise/exit().
    A `return` that computes something (`return os.system(cmd)`) doesn't count."""
    if not body:
        return None
    *head, last = body
    kind = None
    if isinstance(last, ast.Break):
        kind = "break"
    elif isinstance(last, ast.Continue):
        kind = "continue"
    elif isinstance(last, ast.Raise):
        kind = "raise"
    elif isinstance(last, ast.Return):
        v = last.value
        if v is None or isinstance(v, ast.Constant) or (isinstance(v, (ast.Name, ast.Attribute))
                                                        and dotted(v) not in tainted_names):
            kind = "return"
    elif isinstance(last, ast.Expr) and isinstance(last.value, ast.Call) and _is_exit_call(last.value, exit_names):
        kind = "raise"                                 # sys.exit(...), parser.error(...), die(...)
    if kind is None:
        return None
    for s in head:
        if isinstance(s, ast.Pass):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and dotted(s.value.func) in LOGGING:
            continue
        return None
    return kind


def _constant_set(node):
    return isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(isinstance(e, ast.Constant) for e in node.elts)


def _subject(node, name):
    """True if node is `name`, or `name` passed through string normalizers (`name.strip().lower()`).
    `name` may be an attribute such as `args.host`."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in NORMALIZERS \
            and not node.args:
        node = node.func.value
    return isinstance(node, (ast.Name, ast.Attribute)) and dotted(node) == name


def _parent_map(expr):
    parents = {}
    for p in ast.walk(expr):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    return parents


def _mentions(expr):
    """(node, name) for each variable mentioned in expr. An attribute read such as `args.lines`
    is reported as "args.lines"; a method call (`args.get(...)`) as "args"."""
    parents = _parent_map(expr)
    for x in ast.walk(expr):
        if not isinstance(x, ast.Name):
            continue
        p = parents.get(x)
        if isinstance(p, ast.Attribute) and p.value is x \
                and not (isinstance(parents.get(p), ast.Call) and parents[p].func is p):
            yield p, dotted(p)
        else:
            yield x, x.id


def _attribute_names(expr, tainted):
    """`args.host`-style names in expr whose base variable is tainted (`opts["host"]` counts as `opts.host`)."""
    out = set()
    for x in ast.walk(expr):
        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id in tainted:
            out.add(dotted(x))
        elif isinstance(x, ast.Subscript) and isinstance(x.value, ast.Name) and x.value.id in tainted \
                and _str_constant(x.slice) is not None:
            out.add(x.value.id + "." + _str_constant(x.slice))
    return out


def _direct_names(expr):
    """Variables that expr uses as themselves, not only through an attribute or a constant key."""
    parents = _parent_map(expr)
    return {x.id for x in ast.walk(expr) if isinstance(x, ast.Name)
            and not (isinstance(parents.get(x), ast.Attribute) and parents[x].value is x)
            and not (isinstance(parents.get(x), ast.Subscript) and parents[x].value is x
                     and _str_constant(parents[x].slice) is not None)}


def _classify(test, occ, stmt, loop, name=None, exit_names=()):
    """What a mention of a name inside a test proves about that name.
    Returns "limit", "nolimit", "exists", "exists_exit", ("quit", exit kind, enclosing loop),
    ("while_neg", stmt) or ("while_pos", stmt)."""
    name = name or dotted(occ)
    parents = _parent_map(test)
    node = occ
    while parents.get(node) is not None and isinstance(parents[node], ast.Attribute) \
            and parents[node].attr in NORMALIZERS and isinstance(parents.get(parents[node]), ast.Call) \
            and not parents[parents[node]].args:
        node = parents[parents[node]]

    def logic_path(n):
        """(only BoolOp/not between n and the test root, number of `not`s)."""
        nots, q = 0, parents.get(n)
        while q is not None:
            if isinstance(q, ast.UnaryOp) and isinstance(q.op, ast.Not):
                nots += 1
            elif not isinstance(q, ast.BoolOp):
                return False, 0
            q = parents.get(q)
        return True, nots

    p = parents.get(node)
    ok, _ = logic_path(node)
    if node is test or (ok and isinstance(p, (ast.BoolOp, ast.UnaryOp))):
        return "nolimit"                       # truthiness: empty or not
    # `os.path.isfile(path)`, `path.exists()`, `(NOTES / name).is_file()`: something is there, wherever it is
    child, q = node, p
    while (isinstance(q, ast.Call) and dotted(q.func) in JOINS and child in q.args) \
            or (isinstance(q, ast.BinOp) and isinstance(q.op, (ast.Add, ast.Div))) \
            or isinstance(q, (ast.JoinedStr, ast.FormattedValue)):
        child, q = q, parents.get(q)
    check = None
    if isinstance(q, ast.Call) and dotted(q.func) in EXISTENCE_CALLS and q.args and q.args[0] is child:
        check, what, arg = q, dotted(q.func), q.args[0]
    elif isinstance(q, ast.Attribute) and q.attr in EXISTENCE_METHODS and q.value is child \
            and isinstance(parents.get(q), ast.Call) and parents[q].func is q and not parents[q].args:
        check, what, arg = parents[q], q.attr, q.value
    if check is not None:
        mode = None
        top = parents.get(check)
        whole = check is test or (isinstance(top, ast.BoolOp) and isinstance(top.op, ast.Or) and top is test)
        if what in ANYTHING_THERE + IS_FILE and isinstance(stmt, ast.If) and stmt.test is test:
            if whole and _always_leaves(stmt.body, exit_names):
                mode = "exit"                  # `if os.path.exists(p): return ...`: past here nothing is at p
            elif check is test and stmt.orelse:
                mode = "orelse"                # `if p.exists(): ... else: <nothing is at p>`
            elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and test.operand is check:
                mode = "body"                  # `if not p.exists(): <nothing is at p>`
        return ("exists", {"arg": arg, "what": what, "mode": mode, "stmt": stmt})
    if isinstance(p, ast.Call) and dotted(p.func) == "len" and len(p.args) == 1 and p.args[0] is node:
        cmp = parents.get(p)
        if isinstance(cmp, ast.Compare) and logic_path(cmp)[0]:
            return "nolimit"                   # length says nothing about the characters
        return "limit"
    if not (isinstance(p, ast.Compare) and len(p.ops) == 1 and p.left is node and _subject(node, name)):
        return "limit"
    ok, nots = logic_path(p)
    if not ok:
        return "limit"
    op, other = p.ops[0], p.comparators[0]
    if isinstance(op, (ast.Eq, ast.NotEq)):
        if not isinstance(other, ast.Constant):
            return "limit"
    elif isinstance(op, (ast.In, ast.NotIn)):
        if not (_constant_set(other) or isinstance(other, (ast.Name, ast.Attribute))):
            return "limit"
    else:
        return "limit"
    positive = isinstance(op, (ast.Eq, ast.In)) != (nots % 2 == 1)
    if isinstance(stmt, ast.If) and test is stmt.test and not stmt.orelse:
        exit_kind = _exits_only(stmt.body, exit_names=exit_names)
        if exit_kind:
            return ("quit", exit_kind, loop) if positive else "limit"
        return "limit"
    if isinstance(stmt, ast.While) and test is stmt.test:
        return ("while_pos", stmt) if positive else ("while_neg", stmt)
    return "limit"


def _is_sink_name(d):
    return d in SHELL_ALWAYS or d in SHELL_IF_FLAG or d in CODE_SINKS or d in PATH_SINKS \
        or d.split(".")[-1] in SQL_METHODS


def _catches_value_error(handler):
    t = handler.type
    if t is None:
        return True
    names = [dotted(e) for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
    return any(n in ("ValueError", "Exception", "BaseException") for n in names)


def _occurrences(scope, parents, ctx):
    """Every test of a name in scope, classified. Also `match` subjects, validator calls, and
    `relative_to` inside a `try` whose handler leaves."""
    module_functions, stopping, exit_names = ctx["module_functions"], ctx["stopping"], ctx["exit_names"]
    out = []
    for n in ast.walk(scope):
        tests = []
        if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            tests = [n.test]
        elif isinstance(n, ast.comprehension):
            tests = n.ifs
        elif getattr(ast, "Match", None) is not None and isinstance(n, ast.Match):
            for _x, name in _mentions(n.subject):
                out.append({"name": name, "kind": "limit", "line": n.lineno, "stmt": n})
            continue
        elif isinstance(n, ast.Try):
            if any(_catches_value_error(h) and _always_leaves(h.body, exit_names) for h in n.handlers):
                for c in ast.walk(ast.Module(body=n.body, type_ignores=[])):
                    if not isinstance(c, ast.Call):
                        continue
                    d = dotted(c.func)
                    if isinstance(c.func, ast.Attribute) and c.func.attr == "relative_to":
                        checked = [c.func.value]       # `path.relative_to(BASE)`: escapes raise
                    elif d in SANITIZERS or d.endswith(PARSED):
                        checked = list(c.args)         # `int(port)`, `uuid.UUID(x)`: bad text raises
                    else:
                        continue
                    for t in checked:
                        for _x, name in _mentions(t):
                            out.append({"name": name, "kind": "limit", "line": n.lineno, "stmt": n, "guard": True})
            continue
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            f = dotted(n.value.func)
            if not f or f in LOGGING or _is_sink_name(f):
                continue
            # a call whose result is thrown away can only check something by raising or exiting
            counts = f in stopping if f in module_functions else VALIDATOR_NAME.search(f.split(".")[-1])
            if counts:
                for a in list(n.value.args) + [k.value for k in n.value.keywords]:
                    for _x, name in _mentions(a):
                        out.append({"name": name, "kind": "limit", "call": n.value, "func": f, "line": n.lineno,
                                    "stmt": n, "guard": True})
            continue
        else:
            continue
        loop = _enclosing_loop(n, parents)
        line = getattr(n, "lineno", None) or getattr(parents.get(n), "lineno", 0)   # a comprehension has none
        # a guard rejects by raising or exiting: `if not re.fullmatch(...): raise ValueError(...)`
        guard = isinstance(n, ast.If) and not n.orelse and bool(n.body) and _ends_program(n.body[-1], exit_names)
        for t in tests:
            for occ, name in _mentions(t):
                out.append({"name": name, "kind": _classify(t, occ, n, loop, name, exit_names), "line": line,
                            "stmt": n, "guard": guard})
    return out


def _enclosing_loop(node, parents):
    p = parents.get(node)
    while p is not None and not isinstance(p, FUNCS):
        if isinstance(p, (ast.For, ast.AsyncFor, ast.While)):
            return p
        p = parents.get(p)
    return None


def _in_stmts(node, stmts):
    if not stmts:
        return False
    end = getattr(stmts[-1], "end_lineno", None) or stmts[-1].lineno
    return stmts[0].lineno <= node.lineno <= end


def _in_body(node, container):
    """Is node inside the body (not the else) of a loop?"""
    if container is None or not container.body:
        return False
    start = container.body[0].lineno
    end = getattr(container.body[-1], "end_lineno", None) or container.body[-1].lineno
    return start <= node.lineno <= end


WRITES = ("overwrite", "append", "modify")


def _same_path(a, b, facts):
    """The same plain variable, bound only once in this scope: the existence check is about the sink's path."""
    return isinstance(a, ast.Name) and isinstance(b, ast.Name) and a.id == b.id \
        and facts["bind_count"].get(a.id, 0) <= 1


def _existence(f, o, info, facts):
    """What an existence check before a path sink means for it: "drop" (nothing is there to read or
    delete), "unsure" (any other check before it: a write may then only create files, or not) or None."""
    stmt = info["stmt"]
    if o.get("line", 0) > f["node"].lineno:
        return None                               # checked after the sink
    if info["mode"] and _same_path(info["arg"], f.get("arg"), facts):
        if info["mode"] == "exit":
            applies = (getattr(stmt, "end_lineno", None) or stmt.lineno) < f["node"].lineno
        else:
            applies = _in_stmts(f["node"], stmt.body if info["mode"] == "body" else stmt.orelse)
        if applies:
            if info["what"] in IS_FILE and f["effect"] == "delete_tree":
                return None                       # folders still get past an `isfile` check
            if f["effect"] in ("read", "read_image", "modify", "delete", "delete_tree"):
                return "drop"                     # nothing is there to read or delete
    return "unsure"


def _suppressed(f, occurrences, facts, helpers=(), guards=None):
    """Is the finding's value checked somewhere in a way that could limit it? Any check of any part of
    it counts (a miss is better than a question about the wrong input). For a path that's written,
    existence checks narrow the effect: "create" when only new files get through, "write" when unsure."""
    chain = f["chain"]
    chain_of, normal_of, flags_of, alias_of = facts["chain_of"], facts["normal_of"], facts["flags_of"], facts["alias_of"]
    is_path = f["kind"] == "path"
    narrowed = set()
    for o in occurrences:
        if guards is not None and not (o.get("guard") and id(o.get("stmt")) in guards
                                       and o.get("line", 0) < f["node"].lineno):
            continue                              # for a cleaner: only checks that reject before it returns
        if f.get("via_call") is not None and o.get("call") is f["via_call"]:
            continue                              # the call that carries the value to the sink isn't a check
        if o.get("func") in helpers:
            continue                              # neither is a call that hands it to another file helper
        x, kind = o["name"], o["kind"]
        exists = isinstance(kind, tuple) and kind[0] == "exists"
        if "." in x:
            base, attr = x.split(".", 1)
            if "argparse" in flags_of.get(base, ()):
                x = alias_of.get(base, base) + "." + attr      # `opts = args`, then `opts.host`
            else:
                x = base
                if kind in ("limit", "nolimit"):
                    kind = "limit"                # `if not cfg.valid:` may be a check made elsewhere
        elif x in alias_of:
            x = alias_of[x]
        if normal_of.get(x, x) in chain or ("argparse" in flags_of.get(x, ())
                                            and any(c.startswith(x + ".") for c in chain)):
            pass                                  # a test of the value itself (or of the whole namespace)
        elif chain_of.get(x, frozenset()) & chain:
            if not (exists and is_path):
                return True                       # a test on a value computed from the input: a validation
        else:
            continue
        if exists:
            if not is_path:
                return True                       # for a command, naming an existing file is a real limit
            effect = _existence(f, o, kind[1], facts)
            if effect == "drop":
                return True
            if effect:
                narrowed.add(effect)
            continue                              # existence says nothing about where the path points
        if kind == "nolimit":
            continue
        if kind == "limit":
            return True
        tag = kind[0]
        sink = f["node"]
        if tag == "quit":
            if kind[1] == "break" and not _in_body(sink, kind[2]):
                return True                       # the `break` is how the value reaches the sink
        elif tag == "while_neg":
            if not _in_body(sink, kind[1]):
                return True                       # after `while x not in ALLOWED:`, x is allowed
        elif tag == "while_pos":
            if _in_body(sink, kind[1]):
                return True                       # inside `while x == "y":`, x is "y"
    if is_path and f["effect"] in WRITES and narrowed:
        f["effect"] = "write"                     # it may only create files, or not: "write" is true either way
    return False


# ----------------------------------------------------------------- the scan

NOT_HANDLED = object()


def _mode_effect(node, resolve=None):
    if node is None:
        return "read"
    if isinstance(node, ast.IfExp):
        a, b = _mode_effect(node.body, resolve), _mode_effect(node.orelse, resolve)
        return a if a == b else ("read" if {a, b} == {"read"} else "access")
    if isinstance(node, ast.Name) and resolve is not None and resolve(node.id) is not None:
        return _mode_effect(resolve(node.id))
    mode = _str_constant(node)
    if mode is None:
        return "access"                         # computed somewhere else: read or write, we can't tell
    if "w" in mode:
        return "overwrite"
    if "a" in mode:
        return "append"
    if "x" in mode:
        return "create"
    if "+" in mode:
        return "modify"
    return "read"


def _open_effect(call, position, resolve=None):
    """The effect of an open-style call whose mode is positional argument `position`, or `mode=`."""
    node = None
    if any(isinstance(a, ast.Starred) for a in call.args[:position + 1]):
        return "access"
    if len(call.args) > position:
        node = call.args[position]
    for k in call.keywords:
        if k.arg == "mode":
            node = k.value
        elif k.arg is None:
            return "access"                     # **options may hold the mode
    return _mode_effect(node, resolve)


def _os_open_effect(call):
    """`os.open(path, flags)`: the effect its flags allow."""
    if len(call.args) < 2:
        return "access"
    names = {x.attr if isinstance(x, ast.Attribute) else getattr(x, "id", "") for x in ast.walk(call.args[1])
             if isinstance(x, (ast.Attribute, ast.Name))} - {"os"}
    if not names:
        return "access"
    if any(isinstance(x, ast.IfExp) for x in ast.walk(call.args[1])):
        return "access"                        # `O_TRUNC if force else O_EXCL`: depends on the run
    if "O_EXCL" in names and "O_CREAT" in names:
        return "create"                        # fails if anything is there
    if "O_TRUNC" in names:
        return "overwrite"
    if "O_APPEND" in names:
        return "append"
    if names & {"O_WRONLY", "O_RDWR", "O_CREAT"}:
        return "modify" if not names & {"O_CREAT"} else "access"
    if names <= {"O_RDONLY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK", "O_BINARY", "O_NOCTTY", "O_DIRECTORY"}:
        return "read"
    return "access"


def _int_index(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, int):
        return -node.operand.value
    return None


def _receivers(t):
    """(names that receive data, whether the target is a plain name that is rebound)."""
    if isinstance(t, ast.Name):
        return [t.id], True
    if isinstance(t, (ast.Tuple, ast.List)):
        out = []
        for e in t.elts:
            out += _receivers(e)[0]
        return out, False
    if isinstance(t, ast.Starred):
        return _receivers(t.value)[0], False
    if isinstance(t, (ast.Attribute, ast.Subscript)):
        base = t.value
        while isinstance(base, (ast.Attribute, ast.Subscript)):
            base = base.value
        return ([base.id] if isinstance(base, ast.Name) else []), False
    return [], False


def _scan(scope_node, body, params, tables, lines, fn_name, ctx):
    """Findings in one scope. Returns (findings, facts, param_sinks). param_sinks are path findings
    whose value is one of this function's parameters: not findings here (a helper may be called
    with trusted values), but a caller that passes user input makes them one."""
    typed_options, callees, non_fs, compiled = ctx["typed_options"], ctx["callees"], ctx["non_fs"], ctx["compiled"]
    clean_functions = ctx["clean_functions"]
    local_names = set()
    if isinstance(scope_node, FUNCS):
        for n in ast.walk(scope_node):
            if n is not scope_node and isinstance(n, FUNCS + (ast.ClassDef,)):
                local_names.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local_names.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                local_names |= {(a.asname or a.name).split(".")[0] for a in n.names}
    bind_count, own_values = {}, {}
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, FUNCS + (ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bind_count[n.id] = bind_count.get(n.id, 0) + 1
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            own_values[n.targets[0].id] = n.value
        stack.extend(ast.iter_child_nodes(n))

    def resolve(name):
        """The one value a constant-looking name was given here, or at module level."""
        if bind_count.get(name) == 1 and name in own_values:
            return own_values[name]
        if not bind_count.get(name) and name not in params:
            return ctx["consts"].get(name)
        return None
    tainted, root_of, flags_of, chain_of, normal_of, sql_expr, seq_literal = {}, {}, {}, {}, {}, {}, {}
    alias_of, clean_attrs = {}, set()
    for p in params:
        tainted[p] = [(scope_node.lineno, "`{}` is a parameter the caller controls".format(p))]
        root_of[p] = p
        flags_of[p] = frozenset()
        chain_of[p] = frozenset([p])
    findings, param_sinks, returns, inlining = [], [], [], set()

    def first_child(expr):
        """The first tainted part of an expression whose result is a new value (not the same Path)."""
        for child in ast.iter_child_nodes(expr):
            o = origin(child)
            if o:
                return _with_flags(o, drop=("pathobj",))
        return None

    def attribute_origin(expr):
        if expr.attr in ("name", "stem", "suffix"):
            v = expr.value
            if isinstance(v, ast.Call) and dotted(v.func) in PATH_TYPES and v.args:
                return _with_flags(origin(v.args[0]), "path_clean", drop=("pathobj",))
            o = origin(v)
            if o and "pathobj" in o[2]:
                return _with_flags(o, "path_clean", drop=("pathobj",))   # a Path's last part has no "/"
        if expr.attr == "parent":
            o = origin(expr.value)
            if o:
                return o
        if isinstance(expr.value, ast.Name):
            key = dotted(expr)
            if key in clean_attrs:
                return None                      # `args.port = int(args.port)` earlier
            if "argparse" in flags_of.get(expr.value.id, ()):
                if expr.attr in typed_options:
                    return None                  # declared with type=int, choices= or a flag action
                o = origin(expr.value)
                return (o[0], o[1], o[2], frozenset({key})) if o else None
        return NOT_HANDLED

    def subscript_origin(expr):
        v, idx = expr.value, expr.slice
        key = _str_constant(idx)
        if isinstance(v, ast.Call) and dotted(v.func) == "vars" and len(v.args) == 1 and isinstance(v.args[0], ast.Name) \
                and "argparse" in flags_of.get(v.args[0].id, ()) and key is not None:
            name = v.args[0].id + "." + key
            if key in typed_options or name in clean_attrs:
                return None
            o = origin(v.args[0])
            return (o[0], o[1], o[2], frozenset({name})) if o else None
        if isinstance(v, ast.Call) and dotted(v.func) == "re.subn" and _int_index(idx) == 0:
            return regex_origin(v)                 # `re.subn(...)[0]` is the substituted text
        if isinstance(v, ast.Name) and "argparse" in flags_of.get(v.id, ()) and key is not None:
            name = v.id + "." + key              # `opts = vars(parser.parse_args())`, then `opts["count"]`
            if key in typed_options or name in clean_attrs:
                return None
            o = origin(v)
            return (o[0], o[1], o[2], frozenset({name})) if o else None
        n = _int_index(idx)
        if isinstance(v, ast.Call) and n is not None:
            d = dotted(v.func)
            if d in ("os.path.split", "posixpath.split") and n in (1, -1) and v.args:
                return _with_flags(origin(v.args[0]), "path_clean", drop=("pathobj",))
            if isinstance(v.func, ast.Attribute) and v.func.attr in ("split", "rsplit") and v.args \
                    and _str_constant(v.args[0]) == "/":
                limit = v.args[1] if len(v.args) > 1 else next((k.value for k in v.keywords if k.arg == "maxsplit"), None)
                lim = -1 if limit is None else _int_index(limit)
                if lim is not None and lim < 0:
                    one_part = True                # no limit: every part is a single name
                elif lim is None:
                    one_part = False
                elif v.func.attr == "split":
                    one_part = 0 <= n < lim        # the last part keeps the rest, slashes included
                else:
                    one_part = n >= 1 or -lim <= n <= -1   # rsplit: the first part keeps the rest
                if one_part:
                    return _with_flags(origin(v.func.value), "path_clean", drop=("pathobj",))
        return NOT_HANDLED

    def compiled_pattern(receiver):
        """The pattern text of `pat` in `pat.sub(...)`, when `pat = re.compile("...")` here (or in the file),
        or of `self.SAFE` when a class says `SAFE = re.compile("...")`."""
        if isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name) \
                and (receiver.value.id in ("self", "cls") or receiver.value.id in ctx["classes"]):
            return ctx["class_patterns"].get(receiver.attr)
        if not isinstance(receiver, ast.Name):
            return None
        v = resolve(receiver.id)
        if isinstance(v, ast.Call) and dotted(v.func) == "re.compile" and v.args and _str_constant(v.args[0]):
            return _str_constant(v.args[0])
        return compiled.get(receiver.id) if not bind_count.get(receiver.id) else None

    def regex_origin(expr):
        d = dotted(expr.func)
        if d in ("re.sub", "re.subn"):
            pattern = _str_constant(expr.args[0]) if expr.args else None
            if pattern is None and expr.args and isinstance(expr.args[0], ast.Name):
                pattern = _str_constant(resolve(expr.args[0].id))      # PATTERN = r"[^a-z0-9]"
            repl, target, count_at = (expr.args[1] if len(expr.args) > 1 else None,
                                      expr.args[2] if len(expr.args) > 2 else None, 3)
        elif isinstance(expr.func, ast.Attribute) and expr.func.attr == "sub" and compiled_pattern(expr.func.value):
            pattern = compiled_pattern(expr.func.value)
            repl, target, count_at = (expr.args[0] if expr.args else None,
                                      expr.args[1] if len(expr.args) > 1 else None, 2)
        else:
            return NOT_HANDLED
        kw = {k.arg: k.value for k in expr.keywords if k.arg}
        repl, target = kw.get("repl", repl), kw.get("string", target)
        count = expr.args[count_at] if len(expr.args) > count_at else kw.get("count")
        every = count is None or (isinstance(count, ast.Constant) and count.value == 0)
        flags = _sub_flags(pattern, _str_constant(repl)) if every and repl is not None else None
        o = origin(target) if target is not None else None
        if flags is None or o is None:
            return first_child(expr)
        if flags == "clean":
            return None
        return _with_flags(o, *flags, drop=("pathobj",))

    def inline(expr, d):
        """`sanitize(title, STRICT)` where `def sanitize(text, pattern): return pattern.sub("_", text)`:
        judge the helper's one returned expression with the caller's arguments put in."""
        fn, ret = ctx["simple_returns"][d]
        a = fn.args
        positional = [x.arg for x in a.posonlyargs + a.args]
        defaults = dict(zip(positional[len(positional) - len(a.defaults):], a.defaults))
        defaults.update({x.arg: v for x, v in zip(a.kwonlyargs, a.kw_defaults) if v is not None})
        mapping = {}
        for i, arg in enumerate(expr.args):
            if isinstance(arg, ast.Starred) or i >= len(positional):
                return NOT_HANDLED
            mapping[positional[i]] = arg
        for k in expr.keywords:
            if k.arg is None:
                return NOT_HANDLED
            mapping[k.arg] = k.value
        for name in positional + [x.arg for x in a.kwonlyargs]:
            if name not in mapping:
                if name not in defaults:
                    return NOT_HANDLED
                mapping[name] = defaults[name]
        free = {x.id for x in ast.walk(ret) if isinstance(x, ast.Name)} - set(mapping)
        if any(bind_count.get(x) for x in free):
            return NOT_HANDLED                     # the caller has its own variable with that name

        class Put(ast.NodeTransformer):
            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Load) and node.id in mapping:
                    return copy.deepcopy(mapping[node.id])
                return node

        new = Put().visit(copy.deepcopy(ret))
        for x in ast.walk(new):
            if hasattr(x, "lineno"):
                x.lineno, x.end_lineno = expr.lineno, getattr(expr, "end_lineno", expr.lineno)
        inlining.add(d)
        try:
            return origin(new)
        finally:
            inlining.discard(d)

    def call_origin(expr):
        d = dotted(expr.func)
        if isinstance(expr.func, ast.Name) and d in ctx["simple_returns"] and d not in local_names \
                and d not in inlining:
            o = inline(expr, d)
            if o is not NOT_HANDLED:
                return o
        if d in SANITIZERS or d.endswith(PARSED):
            return None
        if d.split(".")[-1] == "slugify" and d not in ctx["module_functions"]:
            return None                            # python-slugify / Django: only [a-z0-9-] remain
        if d in clean_functions and d not in local_names:
            summary = clean_functions[d]           # a function in this file that cleans or checks its input
            return None if summary == "clean" else _with_flags(first_child(expr), *summary)
        if d in SHELL_SANITIZERS:
            return _with_flags(first_child(expr), "shell_safe")
        if isinstance(expr.func, ast.Attribute) and expr.func.attr in ARGPARSE_CALLS:
            return _with_flags(first_child(expr), "argparse")   # `parse_args(["--n", input()])`
        if d == "filter" and len(expr.args) == 2 and dotted(expr.args[0]) in CHAR_FILTERS:
            return None                            # `filter(str.isalnum, name)`
        if d == "filter" and len(expr.args) == 2 and isinstance(expr.args[0], ast.Lambda) \
                and len(expr.args[0].args.args) == 1:
            lam = expr.args[0]
            kept = _cond_chars(lam.body, lam.args.args[0].arg, resolve)
            flags = _char_flags(kept) if kept is not None else None
            if flags == "clean":
                return None
            if flags:
                return _with_flags(origin(expr.args[1]), *flags)
        f0 = expr.func
        if isinstance(f0, ast.Attribute) and f0.attr == "get" and isinstance(f0.value, ast.Name) \
                and "argparse" in flags_of.get(f0.value.id, ()) and expr.args and _str_constant(expr.args[0]):
            key = _str_constant(expr.args[0])      # `opts = vars(args)`, then `opts.get("count")`
            name = f0.value.id + "." + key
            if key in typed_options or name in clean_attrs:
                return None
            o = origin(f0.value)
            return (o[0], o[1], o[2], frozenset({name})) if o else None
        if d in PATH_SANITIZERS or d in PATH_SAFE:
            extra = ("path_clean", "path_safe") if d in PATH_SAFE else ("path_clean",)
            for arg in expr.args:
                o = origin(arg)
                if o:
                    return _with_flags(o, *extra, drop=("pathobj",))
            return None
        if d.endswith(MODEL_CALLS):
            for arg in list(expr.args) + [k.value for k in expr.keywords]:
                o = origin(arg)
                if o:
                    return o
            return None
        r = regex_origin(expr)
        if r is not NOT_HANDLED:
            return r
        f = expr.func
        if isinstance(f, ast.Attribute) and f.attr == "replace" and len(expr.args) == 2 \
                and _str_constant(expr.args[0]) == "/" and _str_constant(expr.args[1]) is not None \
                and "/" not in _str_constant(expr.args[1]):
            return _with_flags(origin(f.value), "path_clean", drop=("pathobj",))
        if d in JOINS and len(expr.args) >= 2 and origin(expr.args[0]) is None:
            for arg in expr.args[1:]:
                o = origin(arg)
                if o:
                    if d in PATH_TYPES:
                        return _with_flags(o, "prefixed", "pathobj")
                    return _with_flags(o, "prefixed", drop=("pathobj",))
            return None
        if d in PATH_TYPES and len(expr.args) == 1:
            return _with_flags(origin(expr.args[0]), "pathobj")      # Path(name): the file they name
        if isinstance(f, ast.Attribute) and f.attr == "joinpath" and expr.args and origin(f.value) is None:
            fs = _is_file_system(f.value, non_fs)
            for arg in expr.args:
                o = origin(arg)
                if o:
                    return _with_flags(o, "prefixed", "pathobj") if fs else _with_flags(o, drop=("pathobj",))
            return None
        if isinstance(f, ast.Attribute) and f.attr in PATH_PRESERVING:
            o = origin(f.value)
            if o:
                return o                         # `(NOTES / name).resolve()` is still that path
        return first_child(expr)

    def origin(expr):
        """(path, root name, flags, chain) if expr carries tainted data, else None."""
        if expr is None or isinstance(expr, (ast.Compare, ast.BoolOp, ast.Constant)):
            return None
        if isinstance(expr, ast.IfExp):
            return origin(expr.body) or origin(expr.orelse)          # the test only picks one of them
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) and expr.value.id in tables:
            return None
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "get" \
                and isinstance(expr.func.value, ast.Name) and expr.func.value.id in tables \
                and all(isinstance(a, ast.Constant) for a in expr.args[1:]):
            return None                          # every possible value is a constant from the table
        if isinstance(expr, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            elt = expr.elt
            if isinstance(elt, ast.Constant) or (isinstance(elt, ast.Call) and dotted(elt.func) in SANITIZERS):
                return None                      # `",".join("?" for _ in ids)`, `int(p) for p in ports`
            if isinstance(elt, ast.Call) and dotted(elt.func) in SHELL_SANITIZERS:
                return _with_flags(first_child(expr), "shell_safe")      # `shlex.quote(p) for p in paths`
            kept = _filter_flags(expr, resolve)
            if kept == "clean":
                return None                      # `"".join(c for c in name if c.isalnum())`
            if kept:
                return _with_flags(first_child(expr), *kept)
        if isinstance(expr, ast.Attribute):
            o = attribute_origin(expr)
            if o is not NOT_HANDLED:
                return o
        if isinstance(expr, ast.Name):
            if expr.id in tainted:
                return tainted[expr.id], root_of.get(expr.id), flags_of.get(expr.id, frozenset()), chain_of[expr.id]
            return None
        if isinstance(expr, ast.Subscript):
            o = subscript_origin(expr)
            if o is not NOT_HANDLED:
                return o
        src = _source_call(expr, ctx) if isinstance(expr, (ast.Call, ast.Attribute, ast.Subscript)) else None
        if isinstance(expr, ast.Subscript) and src is None:
            src = _source_call(expr.value, ctx) if isinstance(expr.value, (ast.Attribute, ast.Name)) else None
        if src:
            parsed = isinstance(expr, ast.Call) and (
                (isinstance(expr.func, ast.Attribute) and expr.func.attr in ARGPARSE_CALLS)
                or (isinstance(expr.func, ast.Name) and expr.func.id in ctx["arg_functions"]))
            return [(expr.lineno, src)], "__source__", frozenset({"argparse"}) if parsed else frozenset(), frozenset()
        if isinstance(expr, ast.Call):
            return call_origin(expr)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Div)):
            left, right = origin(expr.left), origin(expr.right)
            if right and not left:
                if isinstance(expr.op, ast.Div):
                    if _is_file_system(expr.left, non_fs):
                        return _with_flags(right, "prefixed", "pathobj")
                    return _with_flags(right, drop=("pathobj",))     # inside an archive: `..` can't leave it
                lead = _str_constant(expr.left) or ""
                prefixed = "/" in lead or (isinstance(expr.left, ast.BinOp) and "/" in (_sql_text(expr.left) or ""))
                return _with_flags(right, *(("prefixed",) if prefixed else ()), drop=("pathobj",))
            if left:
                return left if isinstance(expr.op, ast.Div) else _with_flags(left, drop=("pathobj",))
            return None
        if isinstance(expr, ast.JoinedStr):
            lead = ""
            for v in expr.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    lead += v.value
                elif isinstance(v, ast.FormattedValue):
                    o = origin(v.value)
                    if o:
                        return _with_flags(o, *(("prefixed",) if "/" in lead else ()), drop=("pathobj",))
                    lead += "x"
            return None
        return first_child(expr)

    def is_tainted(e):
        return origin(e) is not None

    def finding(kind, effect, position, call, arg, o, d):
        path, root, flags, chain = o
        names = {x.id for x in ast.walk(arg) if isinstance(x, ast.Name) and x.id in tainted} | _attribute_names(arg, tainted)
        full = path + [(call.lineno, lines[call.lineno - 1].strip())]
        return {
            "kind": kind, "effect": effect, "sql_position": position, "line": call.lineno, "node": call,
            "sink": ast.unparse(call) if hasattr(ast, "unparse") else d,
            "call": d, "root": root, "function": fn_name, "param": root if root in params else None,
            "via_model": any(m + "(" in text for _, text in full for m in MODEL_CALLS),
            "path": full, "chain": frozenset(chain) | names, "flags": flags, "direct": _direct_names(arg),
            "arg": arg,
        }

    def check_helper(call):
        """`read_note(sys.argv[1])`, where read_note joins its parameter to a folder and opens it."""
        positional, sinks = callees[call.func.id]
        passed = {}
        for i, a in enumerate(call.args):
            if isinstance(a, ast.Starred):
                break
            if i < len(positional):
                passed[positional[i]] = a
        for k in call.keywords:
            if k.arg:
                passed[k.arg] = k.value
        for param, sink in sinks:
            a = passed.get(param)
            o = origin(a) if a is not None else None
            if not o:
                continue
            path, root, flags, chain = o
            if root in params:
                continue                         # one level only: the value has to come from the user here
            if "path_safe" in flags or ("path_clean" in flags and sink["effect"] != "delete_tree"):
                continue
            attrs = {c[len(param) + 1:] for c in sink["chain"] if c.startswith(param + ".")}
            if attrs and "argparse" in flags and attrs <= typed_options and param not in sink["direct"]:
                continue                         # the helper only reads options that can't carry text
            holder = a.args[0] if isinstance(a, ast.Call) and dotted(a.func) == "vars" and a.args else a
            mapped = {holder.id + "." + x for x in attrs} if isinstance(holder, ast.Name) else set()
            names = {x.id for x in ast.walk(a) if isinstance(x, ast.Name) and x.id in tainted} \
                | _attribute_names(a, tainted) | mapped
            full = path + [(call.lineno, lines[call.lineno - 1].strip())] + sink["path"][1:]
            findings.append(dict(sink, root=root, function=fn_name, param=None, node=call, via_call=call,
                                 via_model=any(m + "(" in text for _, text in full for m in MODEL_CALLS),
                                 path=full, chain=frozenset(chain) | names, flags=flags))

    def saver(receiver):
        """What `receiver.save(path)` writes: "upload" (from `request.files`), "image" (Pillow), "file"
        (a workbook or document from an imported library), or None (`store.save(key)` and the like)."""
        o = origin(receiver)
        if o and any("request.files" in text for _, text in o[0][:1]):
            return "upload"
        value = receiver
        if isinstance(receiver, ast.Name):
            value = own_values.get(receiver.id) if bind_count.get(receiver.id) == 1 else (
                ctx["consts"].get(receiver.id) if not bind_count.get(receiver.id) else None)
        if not isinstance(value, ast.Call):
            return None
        d = dotted(value.func)
        head = d.split(".")[0]
        if not d.endswith(FILE_SAVERS) or head not in ctx["imported"] or head in ctx["classes"]:
            return None
        return "image" if d.split(".")[-2:-1] == ["Image"] or d.startswith("Image.") else "file"

    def check_sinks(node):
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            d = dotted(call.func)
            if isinstance(call.func, ast.Name) and call.func.id in callees and call.func.id not in local_names:
                check_helper(call)
                continue
            kind, effect, position = None, None, None
            if isinstance(call.func, ast.Attribute) and call.func.attr in PATH_METHODS and d not in PATH_SINKS:
                arg = call.func.value
                o = origin(arg)
                if not o or "pathobj" not in o[2]:
                    continue                     # `zf.open(member)` on a zip file isn't a file path
                kind, effect = "path", PATH_METHODS[call.func.attr]
                if effect == "open":
                    effect = _open_effect(call, 0, resolve)
            else:
                if not call.args or isinstance(call.args[0], ast.Starred):
                    continue
                arg = call.args[0]
                if d in SHELL_ALWAYS:
                    kind = "shell"
                elif d in SHELL_IF_FLAG and any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                                                and k.value.value is True for k in call.keywords):
                    kind = "shell"
                    if isinstance(arg, ast.Name) and arg.id in seq_literal:
                        arg = seq_literal[arg.id]
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        if not arg.elts:
                            continue
                        arg = arg.elts[0]          # with shell=True, only the first item is the command
                elif d in CODE_SINKS:
                    kind = "code"
                elif d in PATH_SINKS:
                    kind, effect = "path", PATH_SINKS[d]
                    if effect == "open":
                        effect = _open_effect(call, 1, resolve)
                    elif d == "os.open":
                        effect = _os_open_effect(call)
                elif isinstance(call.func, ast.Attribute) and call.func.attr in SAVE_METHODS:
                    what = saver(call.func.value)
                    if what is None:
                        continue                   # `store.save(key)`, `cache.save(...)`: not a file
                    kind, effect = "path", "write" if what == "image" else "overwrite"
                elif isinstance(call.func, ast.Attribute) and call.func.attr in SQL_METHODS:
                    expr = arg if _sql_text(arg) is not None else (sql_expr.get(arg.id) if isinstance(arg, ast.Name) else None)
                    text = _sql_text(expr) if expr is not None else None
                    if not text or not SQL_WORDS.search(text):
                        continue
                    kind = "sql"
                    position = _sql_position(expr, is_tainted)
            if not kind:
                continue
            o = origin(arg)
            if not o:
                continue
            _path, root, flags, _chain = o
            if kind == "shell" and "shell_safe" in flags:
                continue                           # only characters a shell treats as plain text
            if kind == "path":
                if "prefixed" not in flags:
                    continue                       # a tool that opens the file you name
                if "path_safe" in flags or ("path_clean" in flags and effect != "delete_tree"):
                    continue                       # rmtree("uploads/..") still reaches the parent folder
                if root in params:
                    param_sinks.append(finding(kind, effect, position, call, arg, o, d))
                    continue                       # a helper: its callers decide (see check_helper)
            findings.append(finding(kind, effect, position, call, arg, o, d))

    def assign(target_names, value, lineno, kill=False):
        o = origin(value) if value is not None else None
        text = _sql_text(value) if value is not None else None
        for name in target_names:
            if text and SQL_WORDS.search(text):
                sql_expr[name] = value
            if isinstance(value, (ast.List, ast.Tuple)):
                seq_literal[name] = value
            else:
                seq_literal.pop(name, None)
            if o:
                path, root, flags, chain = o
                tainted[name] = path + [(lineno, lines[lineno - 1].strip())]
                root_of[name] = name if root == "__source__" else root
                flags_of[name] = flags
                chain_of[name] = frozenset(chain) | {name} | _attribute_names(value, tainted)
                v = value
                while isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in NORMALIZERS and not v.args:
                    v = v.func.value
                if isinstance(v, ast.Name) and v.id in tainted and v is not value:
                    normal_of[name] = normal_of.get(v.id, v.id)
                if isinstance(value, ast.Name) and "argparse" in flags_of.get(value.id, ()):
                    alias_of[name] = alias_of.get(value.id, value.id)      # `opts = args`
            elif kill and value is not None and name in tainted:
                for table in (tainted, root_of, flags_of, chain_of, normal_of, alias_of):
                    table.pop(name, None)          # `port = int(port)` in straight-line code: clean from here on

    def clean_assigned(stmts):
        """Names whose last plain assignment in this block gives them a clean value."""
        last = {}
        for b in stmts:
            if isinstance(b, ast.Assign) and len(b.targets) == 1 and isinstance(b.targets[0], ast.Name):
                last[b.targets[0].id] = origin(b.value) is None
        return {n for n, clean in last.items() if clean}

    def settles(handler_body, try_body):
        names = {b.targets[0].id for b in try_body if isinstance(b, ast.Assign) and len(b.targets) == 1
                 and isinstance(b.targets[0], ast.Name)}
        return bool(names) and names <= clean_assigned(handler_body)

    def clean_on_both_sides(stmt):
        """`if x: port = int(a)` / `else: port = 80`: clean whichever way the test goes."""
        if not stmt.orelse:
            return set()
        return clean_assigned(stmt.body) & clean_assigned(stmt.orelse)

    def clean_when_set(stmt):
        """`if args.port: args.port = int(args.port)`: when the test fails the value is empty or None,
        so after the `if` it carries no text either way."""
        test = stmt.test
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot) \
                and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None:
            test = test.left
        if stmt.orelse or not isinstance(test, (ast.Name, ast.Attribute)):
            return
        key = dotted(test)
        for b in stmt.body:
            if isinstance(b, ast.Assign) and len(b.targets) == 1 and dotted(b.targets[0]) == key \
                    and origin(b.value) is None:
                if isinstance(test, ast.Name):
                    assign([key], b.value, b.lineno, kill=True)
                else:
                    clean_attrs.add(key)

    def visit(stmts, top):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                        key = dotted(t)
                        if s.value is not None and not isinstance(s, ast.AugAssign) and origin(s.value) is None:
                            if top:
                                clean_attrs.add(key)       # `args.port = int(args.port)`, not inside an `if`
                            continue
                        clean_attrs.discard(key)
                    names, plain = _receivers(t)
                    assign(names, s.value, s.lineno, kill=top and plain and not isinstance(s, ast.AugAssign))
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                names = [n.id for n in ast.walk(s.target) if isinstance(n, ast.Name)]
                assign(names, s.iter, s.lineno)
                visit(s.body, False)
                visit(s.body, False)       # a value assigned late in the body reaches the top next pass
                visit(s.orelse, False)
            elif isinstance(s, ast.While):
                visit(s.body, False)
                visit(s.body, False)
                visit(s.orelse, False)
            elif isinstance(s, ast.If):
                visit(s.body, False)
                visit(s.orelse, False)
                if top:
                    clean_when_set(s)
                    for name in clean_on_both_sides(s):
                        assign([name], ast.Constant(value=0), s.lineno, kill=True)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                visit(s.body, top)
            elif isinstance(s, ast.Try):
                # when every handler leaves (or sets the same names to clean values), what the try body
                # cleaned stays clean after the `try`
                visit(s.body, top and bool(s.handlers) and all(
                    _always_leaves(h.body, ctx["exit_names"]) or settles(h.body, s.body) for h in s.handlers))
                for h in s.handlers:
                    visit(h.body, False)
                visit(s.orelse, False)
                visit(s.finalbody, False)
            elif isinstance(s, ast.Return) and s.value is not None:
                o = origin(s.value)
                returns.append(finding("return", None, None, s, s.value, o, "return") if o else None)
            elif getattr(ast, "Match", None) is not None and isinstance(s, ast.Match):
                for case in s.cases:
                    visit(case.body, False)
            if isinstance(s, (ast.If, ast.While)):
                heads = [s.test]
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                heads = [s.iter]
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                heads = [item.context_expr for item in s.items]
            elif isinstance(s, ast.Try) or (getattr(ast, "Match", None) is not None and isinstance(s, ast.Match)):
                heads = []
            else:
                heads = [s]
            for h in heads:
                check_sinks(h)

    visit(body, True)
    unique, helper_sinks = {}, {}
    for f in findings:
        unique.setdefault((f["line"], f["kind"], f["root"]), f)
    for f in param_sinks:
        helper_sinks.setdefault((f["line"], f["root"]), f)
    facts = {"chain_of": chain_of, "normal_of": normal_of, "flags_of": flags_of, "alias_of": alias_of,
             "root_of": root_of, "bind_count": bind_count, "returns": returns}
    return list(unique.values()), facts, list(helper_sinks.values())


def _return_summary(returns, occ, facts, params, guards):
    """"clean" if every value the function returns is clean or checked first (each parameter in it,
    not just one), a set of flags if each one at least can't hold "/" (or shell syntax), None otherwise."""
    if not returns:
        return None
    flags = None
    for f in returns:
        if f is None:
            continue
        inputs = [p for p in params if p in f["chain"]]
        if inputs and all(_suppressed(dict(f, chain=frozenset({p} | {c for c in f["chain"] if c.startswith(p + ".")})),
                                      occ, facts, guards=guards) for p in inputs):
            continue
        kept = f["flags"] & {"path_clean", "path_safe", "shell_safe"}
        if not kept:
            return None
        flags = kept if flags is None else flags & kept
        if not flags:
            return None
    return "clean" if flags is None else frozenset(flags)


def taint_findings(source):
    """All findings in a module, one list for every function plus the module's own code."""
    tree = ast.parse(source)
    lines = source.splitlines()
    tables = _constant_tables(tree)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    ctx = _module_context(tree)
    scopes = []
    for node in ast.walk(tree):
        if isinstance(node, FUNCS):
            numeric = set()
            for d in node.decorator_list:
                for c in ast.walk(d):
                    t = _str_constant(c)
                    if t:
                        numeric |= set(re.findall(r"<(?:int|float):(\w+)>", t))
            params = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                      if a.arg not in ("self", "cls") and a.arg not in numeric
                      and dotted(a.annotation) not in ("int", "float", "bool")]
            scopes.append((node, node.body, params, node.name, _occurrences(node, parents, ctx)))
    module_body = [s for s in tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    module = ast.Module(body=module_body, type_ignores=[])
    fake = ast.parse("0").body[0]
    fake.lineno = 1
    scopes.append((fake, module_body, [], None, _occurrences(module, parents, ctx)))

    # Pass 0: module functions that clean or check the value they return
    # (`def safe_name(s): return re.sub(r"[^a-z0-9]", "", s)`, or one that raises on bad input first).
    defined = [n.name for n in tree.body if isinstance(n, FUNCS)]
    for _round in range(2):                       # twice, so a cleaner may call one defined below it
        for node, body, params, name, occ in scopes:
            if name is None or not params or node not in tree.body or defined.count(name) != 1 \
                    or any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in ast.walk(node)) \
                    or any(isinstance(x, ast.Try) and not all(_always_leaves(h.body, ctx["exit_names"])
                                                              for h in x.handlers) for x in ast.walk(node)):
                continue                           # a `try` that swallows errors may swallow the check too
            _found, facts, _ = _scan(node, body, params, tables, lines, name, ctx)
            summary = _return_summary(facts["returns"], occ, facts, params, {id(st) for st in node.body})
            if summary is None:
                ctx["clean_functions"].pop(name, None)
            else:
                ctx["clean_functions"][name] = summary

    ctx["typed_options"] = _typed_options(tree, ctx["clean_functions"])

    # Pass 1: module-level helpers that join a parameter to a folder and use it as a path.
    for node, body, params, name, occ in scopes:
        if name is None or node not in tree.body or defined.count(name) != 1:
            continue
        _found, facts, helper_sinks = _scan(node, body, params, tables, lines, name, ctx)
        kept = [(f["root"], f) for f in helper_sinks if not _suppressed(f, occ, facts)]
        if kept:
            ctx["callees"][name] = ([a.arg for a in node.args.posonlyargs + node.args.args], kept)

    # Pass 2: every scope, now following calls into those helpers.
    helpers, typed = set(ctx["callees"]), ctx["typed_options"]
    findings = []
    for node, body, params, name, occ in scopes:
        found, facts, _ = _scan(node, body, params, tables, lines, name, ctx)
        for f in found:
            if _suppressed(f, occ, facts, helpers):
                continue
            if f["param"] and typed:
                attrs = {c[len(f["param"]) + 1:] for c in f["chain"] if c.startswith(f["param"] + ".")}
                if attrs and attrs <= typed and f["param"] not in f["direct"]:
                    continue                       # parsed options declared as numbers, choices or flags
            findings.append(f)
    findings.sort(key=lambda f: f["line"])
    return findings
