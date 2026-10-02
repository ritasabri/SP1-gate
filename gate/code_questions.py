"""Phase 2: questions about the code the agent wrote this turn.

Static only: the engine reads the code but never runs it. Running agent code safely needs
an OS sandbox (docs/DESIGN.md, principle 4), and this version doesn't have one, so it asks
only what the syntax proves. Each family below has a key that follows from the code, and
wrong options that the same code rules out, each tied to a known misconception.

Only lines the agent changed this turn are asked about.
"""
import ast
import difflib
import os
import re

from common import load_stdlib_names, question
from taint import taint_findings

AGENT_CAP = "The AI agent"
CONTAINER_CALLS = {"list": "list", "dict": "dict", "set": "set", "tuple": "tuple"}
COLLECTIONS = {"deque": "deque", "defaultdict": "defaultdict", "Counter": "Counter"}
DESC = {
    "list": "A list: items in order, duplicates allowed, found by position",
    "dict": "A dict: each key maps to a value, found by key",
    "set": "A set: no duplicates and no order, quick to check membership",
    "tuple": "A tuple: items in order that can't be changed afterwards",
    "deque": "A deque: a queue that's quick to add to or remove from at both ends",
    "defaultdict": "A defaultdict: a dict that fills in a default value for missing keys",
    "Counter": "A Counter: a dict that counts how many times each item appears",
}
SYNTAX = {"list": "`[...]` or `list(...)`", "dict": "`{key: value}`, `{}` or `dict(...)`",
          "set": "`{a, b}` or `set(...)`", "tuple": "`(a, b)` or `tuple(...)`"}
HASHED = {"set", "dict", "defaultdict", "Counter"}
SCANNED = {"list", "tuple", "deque"}
PERSISTENT_NAMES = re.compile(r"(cache|memo|seen|visited|history|results?|counts?|total|store|stats|"
                              r"registry|sessions?|users|scores|log)", re.I)
MUTATORS = {"append", "extend", "insert", "add", "update", "setdefault", "pop", "remove",
            "clear", "discard", "popitem", "sort", "reverse", "appendleft", "extendleft"}
STR_METHODS = {"upper", "lower", "strip", "lstrip", "rstrip", "title", "capitalize", "swapcase",
               "casefold", "replace"}
LIMIT_KWARGS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
FILE_KINDS = {
    ".py": "A Python program: code that `python3` runs",
    ".js": "JavaScript: code that a browser (or Node.js) runs",
    ".html": "A web page: the browser shows it",
    ".css": "A stylesheet: it sets how a web page looks",
    ".json": "JSON data: it holds values, not code",
    ".md": "Formatted notes (Markdown) for people to read",
    ".sh": "A shell script: terminal commands that run one after another",
    ".sql": "SQL: commands for a database",
    ".csv": "A table of data, one row per line, commas between columns",
}


# ----------------------------------------------------------------- helpers

def changed_lines(before, after):
    """Line numbers (in the new text) that the agent added or changed."""
    new = after.splitlines()
    if before is None:
        return set(range(1, len(new) + 1))
    sm = difflib.SequenceMatcher(None, before.splitlines(), new, autojunk=False)
    out = set()
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = dotted(node.value)
        return inner + "." + node.attr if inner else ""
    return ""


def src(source_lines, node):
    try:
        return ast.unparse(node)
    except Exception:
        return source_lines[node.lineno - 1].strip()


def short(text, limit=48):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Module:
    """Facts about one Python file that several families share."""

    def __init__(self, source):
        self.source = source
        self.lines = source.splitlines()
        self.tree = ast.parse(source)
        self.collections_names = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ImportFrom) and n.module == "collections":
                for a in n.names:
                    if a.name in COLLECTIONS:
                        self.collections_names.add(a.asname or a.name)
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name == "collections":
                        self.collections_names |= {"{}.{}".format(a.asname or "collections", c) for c in COLLECTIONS}
        self.parents = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent

    def kind_of(self, value):
        if isinstance(value, (ast.List, ast.ListComp)):
            return "list"
        if isinstance(value, (ast.Dict, ast.DictComp)):
            return "dict"
        if isinstance(value, (ast.Set, ast.SetComp)):
            return "set"
        if isinstance(value, ast.Tuple):
            return "tuple"
        if isinstance(value, ast.Call):
            d = dotted(value.func)
            if d in CONTAINER_CALLS:
                return CONTAINER_CALLS[d]
            if d in self.collections_names:
                return COLLECTIONS[d.split(".")[-1]]
        return None

    def scope_of(self, node):
        p = self.parents.get(node)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            p = self.parents.get(p)
        return p if p is not None else self.tree

    def stable_kinds(self, scope):
        """name -> (kind, first line) for names that only ever hold one container kind in
        this scope: every binding is a plain assignment of that kind, and the name isn't a
        parameter, a loop variable, global, nonlocal, or deleted."""
        kinds, first, unstable = {}, {}, set()
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = scope.args
            unstable |= {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            if a.vararg:
                unstable.add(a.vararg.arg)
            if a.kwarg:
                unstable.add(a.kwarg.arg)
        for n in ast.walk(scope):
            if n is not scope and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if self.scope_of(n) is not scope and n is not scope:
                continue
            if isinstance(n, (ast.Global, ast.Nonlocal)):
                unstable |= set(n.names)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        k = self.kind_of(n.value)
                        if k is None or kinds.get(t.id, k) != k:
                            unstable.add(t.id)
                        else:
                            kinds[t.id] = k
                            first.setdefault(t.id, n.lineno)
                    else:
                        unstable |= _rebound_names(t)
            elif isinstance(n, (ast.AnnAssign,)) and isinstance(n.target, ast.Name):
                k = self.kind_of(n.value) if n.value is not None else None
                if k is None or kinds.get(n.target.id, k) != k:
                    unstable.add(n.target.id)
                else:
                    kinds[n.target.id] = k
                    first.setdefault(n.target.id, n.lineno)
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                unstable |= {x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        unstable |= {x.id for x in ast.walk(item.optional_vars) if isinstance(x, ast.Name)}
            elif isinstance(n, ast.Delete):
                unstable |= {x.id for x in ast.walk(n) if isinstance(x, ast.Name)}
            elif isinstance(n, ast.NamedExpr):
                unstable.add(n.target.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                unstable |= {(a.asname or a.name).split(".")[0] for a in n.names}
            elif isinstance(n, ast.ExceptHandler) and n.name:
                unstable.add(n.name)
        return {name: (k, first[name]) for name, k in kinds.items() if name not in unstable}

    def inside_loop(self, node, stop=None):
        p = self.parents.get(node)
        while p is not None and p is not stop and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(p, (ast.For, ast.AsyncFor, ast.While, ast.comprehension, ast.ListComp,
                              ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                return p
            p = self.parents.get(p)
        return None


def _rebound_names(target):
    """Names that an assignment target rebinds. `x[i] = v` and `x.a = v` change the
    object x refers to, but x itself still names the same object."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for e in target.elts:
            out |= _rebound_names(e)
        return out
    if isinstance(target, ast.Starred):
        return _rebound_names(target.value)
    return set()


def _spec(specs, family, priority, lens, line, stem, key, why, distractors, file_rel):
    specs.append({"family": family, "priority": priority, "lens": lens, "line": line, "file": file_rel,
                  "stem": stem, "key": key, "why": why, "distractors": distractors})


# ----------------------------------------------------------------- families

def fam_container(m, changed, rel, specs):
    for scope in [m.tree] + [n for n in ast.walk(m.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for name, (kind, line) in m.stable_kinds(scope).items():
            if line not in changed or kind not in DESC:
                continue
            code = short(m.lines[line - 1].strip(), 60)
            distractors = []
            key_syntax = SYNTAX.get(kind, "`{}(...)` from `collections`".format(kind))
            for other in ("list", "dict", "set", "tuple"):
                if other == kind or len(distractors) == 3:
                    continue
                if kind == "dict" and other == "set" and re.search(r"=\s*\{\s*\}", m.lines[line - 1]):
                    why = "`{}` makes an empty dict. An empty set has to be written `set()`."
                else:
                    why = "{} is written {}. Line {} makes a {}.".format(
                        "A " + other, SYNTAX[other], line, kind)
                distractors.append((DESC[other], why))
            _spec(specs, "container", 4, "data structures", line,
                  "Line {} runs `{}`. What kind of data structure is `{}`?".format(line, code, name),
                  DESC[kind],
                  "The syntax on line {} ({}) builds {}.".format(line, key_syntax, "a " + kind),
                  distractors, rel)


def fam_membership(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))):
            continue
        if node.lineno not in changed or not isinstance(node.comparators[0], ast.Name):
            continue
        name = node.comparators[0].id
        scope = m.scope_of(node)
        kinds = m.stable_kinds(scope)
        if name not in kinds:
            continue
        kind = kinds[name][0]
        if kind not in HASHED | SCANNED:
            continue
        expr = short(src(m.lines, node), 40)
        loop = m.inside_loop(node)
        scan = "It may compare with every item: up to k comparisons"
        hashed = "One hash lookup on average, however big it gets"
        log = ("About log₂ k comparisons, like a binary search",
               "Binary search needs sorted data, and `in` never does it. A {} is {}.".format(
                   kind, "scanned from the start" if kind in SCANNED else "looked up by hash"))
        copy = ("It copies `{}` first, then searches the copy".format(name),
                "`in` reads `{}` directly; nothing is copied.".format(name))
        if kind in SCANNED:
            key = scan
            why = "`in` on a {} checks items one by one until it finds a match, so the work grows with k.{}".format(
                kind, " It's inside a loop, so that cost repeats on every pass." if loop else "")
            distractors = [(hashed, "That's how sets and dicts work. A {} is scanned item by item.".format(kind)), log, copy]
        else:
            key = hashed
            why = "A {} stores items by their hash, so `in` jumps straight to where the item would be. (On average: many hash collisions could make it slower.)".format(kind)
            distractors = [(scan, "That's how lists work. A {} uses hashing to jump straight to the item.".format(kind)), log, copy]
        _spec(specs, "membership", 6 if loop else 4, "algorithms", node.lineno,
              "Line {} checks `{}`. As `{}` grows to k items, how much work is that one check?".format(node.lineno, expr, name),
              key, why, distractors, rel)


def fam_lifetime(m, changed, rel, specs):
    for fn in (n for n in ast.walk(m.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        kinds = m.stable_kinds(fn)
        nested_refs = set()
        for inner in ast.walk(fn):
            if inner is not fn and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                nested_refs |= {x.id for x in ast.walk(inner) if isinstance(x, ast.Name)}
        for stmt in fn.body:
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                continue
            name = stmt.targets[0].id
            if stmt.lineno not in changed or name not in kinds or name in nested_refs:
                continue
            kind = kinds[name][0]
            if kind == "tuple":
                continue
            empty = isinstance(stmt.value, (ast.List, ast.Dict, ast.Set)) and not getattr(stmt.value, "elts", getattr(stmt.value, "keys", None)) \
                or (isinstance(stmt.value, ast.Call) and not stmt.value.args and not stmt.value.keywords)
            key = "Each call makes a new{} `{}`; nothing carries over".format(", empty" if empty else "", name)
            prio = 7 if PERSISTENT_NAMES.search(name) else 3
            _spec(specs, "lifetime", prio, "data structures", stmt.lineno,
                  "Line {} creates `{}` inside `{}`. What happens to `{}` between calls to `{}`?".format(
                      stmt.lineno, name, fn.name, name, fn.name),
                  key,
                  "Line {} runs every time `{}` is called, and a local variable disappears when the call returns. To remember things between calls, `{}` has to live outside the function.".format(stmt.lineno, fn.name, name),
                  [("It keeps everything added so far, because a {} lasts until the program ends".format(kind),
                    "A local variable belongs to one call. When the call returns, nothing refers to that {} anymore, and the next call starts over on line {}.".format(kind, stmt.lineno)),
                   ("Every function in the file can use it, because it's a {}".format(kind),
                    "Where a variable is assigned decides its scope, not its type. `{}` is local to `{}`.".format(name, fn.name)),
                   ("It keeps only what the most recent call put in it",
                    "Nothing is kept: line {} runs again at the start of every call.".format(stmt.lineno))],
                  rel)


def fam_mutable_default(m, changed, rel, specs):
    for fn in (n for n in ast.walk(m.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        if fn.lineno not in changed:
            continue
        a = fn.args
        positional = a.posonlyargs + a.args
        pairs = list(zip(positional[len(positional) - len(a.defaults):], a.defaults))
        pairs += [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        for arg, default in pairs:
            kind = m.kind_of(default)
            if kind not in ("list", "dict", "set"):
                continue
            p = arg.arg
            mutated = reassigned = False
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == p:
                    mutated = True
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) \
                        and n.value.id == p:
                    mutated = True
                if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == p:
                    mutated = True
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == p for t in n.targets):
                    reassigned = True  # the shared default may never be touched: too subtle to ask
            if reassigned:
                continue
            key = ("The same {}, made once at `def`, so the first call's additions are still there".format(kind)
                   if mutated else "The same {}, made once when `def` ran, shared by both calls".format(kind))
            _spec(specs, "mutable-default", 8 if mutated else 5, "correctness", fn.lineno,
                  "Line {} defines `{}` with `{}={}`. If `{}` is called twice without passing `{}`, what do the two calls get?".format(
                      fn.lineno, fn.name, p, src(m.lines, default), fn.name, p),
                  key,
                  "Python creates default values once, when `def` runs, not on every call. Every call that doesn't pass `{}` shares that one {}. The usual fix is `{}=None`, then create the {} inside the function.".format(p, kind, p, kind),
                  [("A brand-new, empty {} each time, since defaults are rebuilt on every call".format(kind),
                    "Defaults are created once, when the function is defined. That's why `None` is the usual default."),
                   ("An error on the second call, because a default can't be a {}".format(kind),
                    "It's allowed, which is exactly why this bug is so common."),
                   ("A copy of the default {}, so the second call can't see the first one's changes".format(kind),
                    "No copy is made: both calls share one object.")],
                  rel)


SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "venv", "env", "dist", "build"}


def _found_elsewhere(root, mod, max_depth=4):
    base = root.rstrip(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(base):].count(os.sep)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and depth < max_depth]
        if mod + ".py" in filenames or mod in dirnames:
            return True
    return False


def fam_imports(m, changed, rel, specs, file_dir, root):
    stable, version_dependent = load_stdlib_names()
    done = set()
    for node in ast.walk(m.tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module.split(".")[0]]
        else:
            continue
        if node.lineno not in changed:
            continue
        for mod in mods:
            if mod in done or mod in version_dependent or mod == "__future__":
                continue
            done.add(mod)
            local = any(os.path.exists(os.path.join(d, mod + ".py")) or os.path.isdir(os.path.join(d, mod))
                        for d in {file_dir, root})
            if not local and mod not in stable and _found_elsewhere(root, mod):
                continue          # it may be a local package in another folder (src/ layout): can't be sure
            if local and mod in stable:
                continue          # a local file shadowing the standard library: too subtle to ask
            std = "Python's standard library, installed along with Python"
            third = "A third-party package that has to be installed first, usually from PyPI with pip"
            here = "A file in this project (`{}.py` or a `{}/` folder)".format(mod, mod)
            auto = ("Python downloads it automatically the first time this line runs",
                    "Python never downloads anything on import. If the module isn't there, the import fails with ModuleNotFoundError.")
            if local:
                key, prio = here, 2
                why = "Python looks in the script's own folder first, and `{}` is there.".format(mod)
                distractors = [(std, "`{}` isn't a standard-library module; it's a file in this project.".format(mod)),
                               (third, "Nothing needs installing: `{}` is right here in the project.".format(mod)), auto]
            elif mod in stable:
                key, prio = std, 2
                why = "`{}` is part of Python's standard library, which comes with every Python install.".format(mod)
                distractors = [(third, "`{}` comes with Python; no install needed.".format(mod)),
                               (here, "There's no `{}.py` or `{}/` in this project. Python finds it in its own library.".format(mod, mod)),
                               auto]
            else:
                key, prio = third, 5
                why = "`{}` isn't in the standard library or in this project, so someone has to install it, or the import fails. Installing means trusting its authors.".format(mod)
                distractors = [(std, "`{}` isn't part of Python's standard library.".format(mod)),
                               (here, "There's no `{}.py` or `{}/` folder in this project.".format(mod, mod)), auto]
            _spec(specs, "import", prio, "dependencies", node.lineno,
                  "Line {} imports `{}`. Where does `{}` come from?".format(node.lineno, mod, mod),
                  key, why, distractors, rel)


def fam_taint(m, changed, rel, specs):
    try:
        findings = taint_findings(m.source)
    except (SyntaxError, RecursionError):
        return
    for f in findings:
        if not any(line in changed for line, _ in f["path"]):
            continue
        root = f["param"] or f["root"]
        who = ("Whoever calls `{}` controls `{}`".format(f["function"], f["param"])
               if f["function"] and f["param"]
               else "Whoever runs this program controls the input on line {}".format(f["path"][0][0]))
        sink = f["line"]
        if f["kind"] == "shell" and f["via_model"]:
            key = "It can shape the shell command that line {} runs, via the model's reply".format(sink)
            why = "The input goes into the prompt, the model's reply becomes the command, and line {} runs it through the shell. Text in a prompt can steer the reply (OWASP LLM01), and running a reply unchecked is improper output handling (LLM05).".format(sink)
            distractors = [("It reaches the model, but the model's reply doesn't depend on it",
                            "A model's reply depends on everything in its prompt, including this input."),
                           ("It reaches line {} as text that the shell displays but doesn't run".format(sink),
                            "The shell runs the whole string as a command."),
                           ("It can't reach line {}: a limit like `max_tokens` blocks it".format(sink),
                            "`max_tokens` caps the reply's length, not what it says.")]
        elif f["kind"] == "shell":
            key = "Run extra shell commands of their own, by adding something like `; rm -rf ~`"
            why = "The input becomes part of a command string that line {} hands to the shell, and the shell treats `;`, `|` and `$( )` as more commands. Pass a list of arguments without `shell=True` instead.".format(sink)
            distractors = [("Nothing: `{}` escapes the text before running it".format(f["call"]),
                            "Nothing is escaped: the shell sees the whole string, special characters included."),
                           ("Only change what line {} prints, not what it runs".format(sink),
                            "The input becomes part of the command itself, so it changes what runs."),
                           ("Nothing, because `{}` is only used inside this function".format(root if root != "__source__" else "it"),
                            "Where a value ends up matters, not where it's stored: it flows into the command on line {}.".format(sink))]
        elif f["kind"] == "code":
            key = "Run any Python code they like, with this program's permissions"
            why = "`{}` runs its argument as Python code, so input that reaches it can do anything the program can.".format(f["call"])
            distractors = [("Nothing: `{}` only does arithmetic".format(f["call"]),
                            "`{}` runs any Python expression, including ones that call `os.system`.".format(f["call"])),
                           ("Only produce a number or a string, never run commands",
                            "Python code can import modules and run commands."),
                           ("Nothing, because the input arrives as a string",
                            "Turning strings into running code is exactly what `{}` does.".format(f["call"]))]
        else:
            key = "Change the SQL query itself, for example to read or delete other rows"
            why = "The input is pasted into the SQL text on line {}, so it can add SQL of its own (SQL injection). Pass values separately: `execute(\"... WHERE name = ?\", (name,))`.".format(sink)
            distractors = [("Nothing: `execute` escapes values inside f-strings automatically",
                            "By the time `execute` sees it, the f-string is already one finished string. Nothing is escaped."),
                           ("Only change which value is searched for, never the query's structure",
                            "Quotes and semicolons in the input change the query's structure."),
                           ("Nothing, as long as the database has a password",
                            "The program already has the password; injected SQL runs with its access.")]
        _spec(specs, "taint", 10, "security", sink,
              "{}. What can they do through line {}?".format(who, sink), key, why, distractors, rel)
        return


def fam_open_mode(m, changed, rel, specs):
    for n in ast.walk(m.tree):
        if isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == "open" for a in n.names):
            return
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "open":
            return
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "open" for t in n.targets):
            return
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and dotted(node.func) == "open" and node.lineno in changed):
            continue
        mode = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for k in node.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = k.value.value
        if not isinstance(mode, str) or "+" in mode:
            continue
        letter = next((c for c in mode if c in "rwax"), "r")
        if letter == "r":
            continue
        target = short(src(m.lines, node.args[0]), 30) if node.args else "the file"
        texts = {
            "w": "It's erased the moment the file opens, before anything is written",
            "a": "It's kept; everything written goes after it",
            "x": "Nothing is touched: the open fails with FileExistsError",
            "ask": "Python asks first, then replaces it if you agree",
        }
        feedback = {
            "w": "That's mode `\"w\"`, which empties the file as soon as it opens.",
            "a": "That's mode `\"a\"` (append), which keeps the old contents.",
            "x": "That's mode `\"x\"`, which refuses to open a file that already exists.",
            "ask": "Python never asks. Mode `\"{}\"` decides on its own.".format(mode),
        }
        others = [o for o in ("w", "a", "x", "ask") if o != letter][:3]
        why = {"w": "Mode `\"w\"` truncates: the old contents are gone as soon as `open` runs, even if nothing is ever written.",
               "a": "Mode `\"a\"` appends: the old contents stay, and writes go at the end.",
               "x": "Mode `\"x\"` creates a new file and refuses to overwrite one that exists."}[letter]
        _spec(specs, "open-mode", 6 if letter == "w" else 4, "systems", node.lineno,
              "Line {} opens {} with mode `\"{}\"`. If that file already exists, what happens to what's in it?".format(
                  node.lineno, "`{}`".format(target) if node.args else target, mode),
              texts[letter], why, [(texts[o], feedback[o]) for o in others], rel)


def _loop_has_exit(loop):
    for n in ast.walk(loop):
        if isinstance(n, (ast.Break, ast.Return, ast.Raise)):
            return True
    return False


def fam_iteration(m, changed, rel, specs):
    for loop in (n for n in ast.walk(m.tree) if isinstance(n, ast.For)):
        if loop.lineno not in changed:
            continue
        it, target, line = loop.iter, loop.target, loop.lineno
        kinds = m.stable_kinds(m.scope_of(loop))
        if isinstance(it, ast.Name) and isinstance(target, ast.Name) and kinds.get(it.id, (None,))[0] in ("dict", "defaultdict", "Counter"):
            d, t = it.id, target.id
            _spec(specs, "iteration", 5, "logic", line,
                  "On line {}, `for {} in {}:` loops over a dict. What is `{}` on each pass?".format(line, t, d, t),
                  "Each key of `{}`, one per pass".format(d),
                  "Looping over a dict gives its keys. `.values()` gives values, and `.items()` gives (key, value) pairs.",
                  [("Each value in `{}`".format(d), "Looping over a dict gives its keys. Use `.values()` for values."),
                   ("Each (key, value) pair", "Pairs come from `.items()`. A plain loop over a dict gives keys."),
                   ("Each position: 0, 1, 2, …", "Dicts are looked up by key, not position. The loop gives keys.")], rel)
        elif isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr == "items" and not it.args \
                and isinstance(target, ast.Tuple) and len(target.elts) == 2 and all(isinstance(e, ast.Name) for e in target.elts):
            k, v = target.elts[0].id, target.elts[1].id
            _spec(specs, "iteration", 5, "logic", line,
                  "On line {}, what are `{}` and `{}` on each pass of `for {}, {} in {}:`?".format(line, k, v, k, v, short(src(m.lines, it), 30)),
                  "`{}` is a key and `{}` is that key's value".format(k, v),
                  "`.items()` gives (key, value) pairs, in that order.",
                  [("`{}` is a position (0, 1, 2, …) and `{}` is the value".format(k, v),
                    "That's `enumerate`. `.items()` pairs each key with its value."),
                   ("`{}` is the value and `{}` is the key".format(k, v), "`.items()` gives (key, value), key first."),
                   ("`{}` and `{}` are two keys, taken two at a time".format(k, v), "Each pass gives one key and its value.")], rel)
        elif isinstance(it, ast.Call) and dotted(it.func) == "enumerate" and len(it.args) == 1 \
                and isinstance(target, ast.Tuple) and len(target.elts) == 2 and all(isinstance(e, ast.Name) for e in target.elts):
            start = 0
            for kw in it.keywords:
                if kw.arg == "start" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                    start = kw.value.value
                elif kw.arg == "start":
                    start = None
            if start is None:
                continue
            i, x = target.elts[0].id, target.elts[1].id
            seq = short(src(m.lines, it.args[0]), 24)
            counts = "{}, {}, {}, …".format(start, start + 1, start + 2)
            other = 1 if start == 0 else 0
            _spec(specs, "iteration", 5, "logic", line,
                  "On line {}, what are `{}` and `{}` on each pass of `for {}, {} in enumerate({}{}):`?".format(
                      line, i, x, i, x, seq, "" if start == 0 else ", start={}".format(start)),
                  "`{}` counts {} and `{}` is the next item of `{}`".format(i, counts, x, seq),
                  "`enumerate` pairs a running count (starting at {}) with each item.".format(start),
                  [("`{}` is the item and `{}` is its position".format(i, x), "`enumerate` gives (count, item): the count comes first."),
                   ("`{}` counts {}, {}, {}, … and `{}` is the next item of `{}`".format(i, other, other + 1, other + 2, x, seq),
                    "The count starts at {} here.".format(start)),
                   ("`{}` and `{}` are two neighbouring items".format(i, x), "One of them is a count, not an item.")], rel)
        elif isinstance(it, ast.Call) and dotted(it.func) == "range" and isinstance(target, ast.Name):
            args = it.args
            if len(args) == 1 and isinstance(args[0], ast.Call) and dotted(args[0].func) == "len" and len(args[0].args) == 1:
                seq = short(src(m.lines, args[0].args[0]), 24)
                t = target.id
                _spec(specs, "iteration", 5, "logic", line,
                      "On line {}, what values does `{}` take in `for {} in range(len({})):`?".format(line, t, t, seq),
                      "Each position in `{}`: 0, 1, …, up to one less than its length".format(seq),
                      "`range(n)` counts from 0 up to n − 1, which are exactly the positions of a sequence with n items.",
                      [("Each item of `{}`".format(seq), "`range(len(...))` gives numbers, the positions, not the items."),
                       ("The positions 1, 2, …, up to its length", "`range` starts at 0 and stops before its argument."),
                       ("Just one number: the length of `{}`".format(seq), "`range(n)` produces n numbers, from 0 to n − 1.")], rel)
            elif all(isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool) for a in args) \
                    and 1 <= len(args) <= 3 and not _loop_has_exit(loop) and not loop.orelse:
                values = [a.value for a in args]
                if len(values) == 3 and values[2] == 0:
                    continue
                n = len(range(*values))
                if n > 1000:
                    continue
                shown = "range({})".format(", ".join(str(v) for v in values))
                alts = [x for x in (n + 1, n - 1, n + 2, values[-1] if len(values) == 1 else values[1]) if x >= 0 and x != n]
                seen, opts = set(), []
                for x in alts:
                    if x not in seen:
                        seen.add(x)
                        opts.append(x)
                if len(opts) < 3:
                    continue
                fmt = lambda k: "{} time{}".format(k, "" if k == 1 else "s")
                _spec(specs, "loop-count", 5, "algorithms", line,
                      "How many times does the body of the loop on line {} (`for … in {}`) run?".format(line, shown),
                      fmt(n),
                      "`{}` produces {}: it starts at {} and stops before {}.".format(
                          shown, ", ".join(str(v) for v in list(range(*values))[:6]) + (", …" if n > 6 else ""),
                          values[0] if len(values) > 1 else 0, values[1] if len(values) > 1 else values[0]),
                      [(fmt(x), "Count again: `range` includes its start and stops just before its end.") for x in opts[:3]], rel)
        elif isinstance(it, ast.Call) and dotted(it.func) == "zip" and len(it.args) == 2 and not it.keywords \
                and isinstance(target, ast.Tuple) and len(target.elts) == 2:
            a, b = (short(src(m.lines, x), 20) for x in it.args)
            _spec(specs, "iteration", 5, "logic", line,
                  "On line {}, what does `zip({}, {})` give the loop?".format(line, a, b),
                  "Items of `{}` and `{}` side by side, stopping at the shorter one".format(a, b),
                  "`zip` pairs the first items, then the second items, and so on, and stops when either runs out.",
                  [("Every combination of an item from `{}` with one from `{}`".format(a, b),
                    "That's `itertools.product`. `zip` pairs items at the same position."),
                   ("All of `{}` first, then all of `{}`".format(a, b), "That's `itertools.chain`. `zip` goes side by side."),
                   ("Items side by side, filling the shorter one with `None`",
                    "That's `itertools.zip_longest`. Plain `zip` stops at the shorter one.")], rel)


def fam_swallowed(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            broad = h.type is None or dotted(h.type) in ("Exception", "BaseException")
            body_is_nothing = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                                  or isinstance(s, ast.Continue) for s in h.body)
            if not (broad and body_is_nothing) or h.lineno not in changed:
                continue
            cont = any(isinstance(s, ast.Continue) for s in h.body)
            key = ("The error is silently ignored, and the loop moves on to the next item" if cont
                   else "The error is silently ignored, and the code after the `try` runs")
            _spec(specs, "swallowed", 7, "correctness", h.lineno,
                  "If the code in the `try` block (line {}) raises an error, what happens?".format(node.lineno),
                  key,
                  "Line {} catches every error and does nothing with it. The program carries on with missing or wrong data, and nobody sees why.".format(h.lineno),
                  [("The program stops and shows the error",
                    "That's what happens without `try`. This `except` catches the error and throws it away."),
                   ("Python retries the failed line until it works",
                    "`except` never retries. It runs its own block, which here does nothing."),
                   ("The error is saved to a log file automatically",
                    "Nothing is logged unless the code logs it.")], rel)


def fam_llm_limit(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and dotted(node.func).endswith(("messages.create", "chat.completions.create", "responses.create"))):
            continue
        if node.lineno not in changed and not any(k.value.lineno in changed for k in node.keywords):
            continue
        for kw in node.keywords:
            if kw.arg in LIMIT_KWARGS and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                k = kw.value.value
                _spec(specs, "llm-limit", 5, "llm integration", node.lineno,
                      "What caps the length of the reply from the model call on line {}?".format(node.lineno),
                      "`{}={}`: the reply stops after at most {} tokens".format(kw.arg, k, k),
                      "`{}` is a hard limit: the model is cut off when it reaches {} tokens. It limits length and cost, not what the reply says.".format(kw.arg, k),
                      [("Nothing: the model always writes until it's done",
                        "`{}` cuts the reply off when it reaches the limit.".format(kw.arg)),
                       ("The prompt's length: a reply can't be longer than the question",
                        "Reply length doesn't depend on prompt length; only the token limit caps it."),
                       ("`temperature`, which sets the maximum length",
                        "`temperature` changes how random the wording is, not how long it is.")], rel)
                break


def fam_flow(m, changed, rel, specs):
    for fn in (n for n in ast.walk(m.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for node in ast.walk(fn):
            if isinstance(node, (ast.Return, ast.Break, ast.Continue)) and node.lineno in changed:
                loop = m.inside_loop(node, stop=fn)
                if not isinstance(loop, (ast.For, ast.While)) or m.scope_of(node) is not fn:
                    continue
                p = m.parents.get(node)
                in_try = False
                while p is not None and p is not fn:
                    if isinstance(p, ast.Try) and p.finalbody:
                        in_try = True
                    p = m.parents.get(p)
                if in_try:
                    continue
                code = short(src(m.lines, node), 30)
                if isinstance(node, ast.Return):
                    key = "It ends `{}` right away, skipping the rest of the loop".format(fn.name)
                    why = "`return` leaves the whole function immediately, loop and all."
                    ds = [("It skips to the next pass of the loop", "That's `continue`. `return` leaves the whole function."),
                          ("It stops the loop, and the code after the loop still runs", "That's `break`. `return` leaves the function, so nothing after the loop runs."),
                          ("It saves the value and keeps looping; the function returns it at the end",
                           "`return` hands the value back immediately. The loop doesn't continue.")]
                elif isinstance(node, ast.Break):
                    key = "It stops the loop right away; the code after the loop runs next"
                    why = "`break` exits the innermost loop only. The function carries on after it."
                    ds = [("It skips the rest of this pass and goes on with the loop", "That's `continue`. `break` ends the loop."),
                          ("It ends the whole function", "That's `return`. `break` only leaves the loop."),
                          ("It pauses the loop until the function is called again", "Loops don't pause. `break` ends this one.")]
                else:
                    if isinstance(loop, ast.For):
                        key = "It skips the rest of this pass and moves on to the next item"
                        why = "`continue` jumps back to the top of the loop for the next item."
                        restart = ("It restarts the loop from the first item", "It moves on to the next item, not back to the first.")
                    else:
                        key = "It skips the rest of this pass; the loop's condition is checked again"
                        why = "`continue` jumps back to the `while` line, which decides whether to run another pass."
                        restart = ("It restarts the loop and resets its variables", "Nothing is reset. It just jumps back to the `while` check.")
                    ds = [("It stops the loop entirely", "That's `break`. `continue` keeps looping."),
                          ("It ends the whole function", "That's `return`. `continue` stays in the loop."),
                          restart]
                _spec(specs, "flow", 4, "logic", node.lineno,
                      "What does `{}` on line {} do, inside the loop on line {}?".format(code, node.lineno, loop.lineno),
                      key, why, ds, rel)


def fam_discarded_str(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in STR_METHODS and isinstance(node.value.func.value, ast.Name)):
            continue
        if node.lineno not in changed:
            continue
        name, meth = node.value.func.value.id, node.value.func.attr
        scope = m.scope_of(node)
        is_str = False
        bound_other = False
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                v = n.value
                if isinstance(v, (ast.JoinedStr,)) or (isinstance(v, ast.Constant) and isinstance(v.value, str)) \
                        or (isinstance(v, ast.Call) and dotted(v.func) in ("input", "str")):
                    is_str = True
                else:
                    bound_other = True
        if not is_str or bound_other:
            continue
        code = short(src(m.lines, node.value), 30)
        _spec(specs, "immutable-str", 7, "correctness", node.lineno,
              "Line {} runs `{}` on its own. What happens to `{}`?".format(node.lineno, code, name),
              "Nothing: `{}()` returns a new string, which is thrown away".format(meth),
              "Strings can't be changed in place. To keep the result, write `{} = {}`.".format(name, code),
              [("`{}` now holds the {} version".format(name, meth),
                "Strings are immutable. Without `{} = ...`, the new string is lost.".format(name)),
               ("Python raises an error because the result isn't saved",
                "Python lets you ignore a result. It just does nothing useful here."),
               ("The change is saved into `{}` when the function ends".format(name),
                "Nothing is saved later either. The new string is discarded right away.")], rel)


def fam_file_kind(path, rel, specs):
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    if base == "requirements.txt":
        key = "The list of Python packages this project needs"
    elif ext in FILE_KINDS:
        key = FILE_KINDS[ext]
    else:
        return
    pool = [(e, t) for e, t in FILE_KINDS.items() if t != key and e in (".py", ".html", ".css", ".json", ".md", ".js")]
    ds = [(t, "That describes a `{}` file. The ending `{}` tells you what this one is.".format(e, ext or base)) for e, t in pool[:3]]
    _spec(specs, "file-kind", 1, "action", 1,
          "{} created `{}`. What kind of file is it?".format(AGENT_CAP, rel), key,
          "The file's ending (`{}`) says what kind of file it is.".format(ext or base), ds, rel)


# ----------------------------------------------------------------- entry point

PY_FAMILIES = [fam_taint, fam_mutable_default, fam_swallowed, fam_discarded_str, fam_lifetime, fam_membership,
               fam_open_mode, fam_iteration, fam_llm_limit, fam_flow, fam_container]


def specs_for_file(path, rel, before, after, root):
    specs = []
    lines = changed_lines(before, after)
    if not lines:
        return specs
    if path.endswith(".py"):
        try:
            m = Module(after)
        except (SyntaxError, ValueError, RecursionError):
            m = None
        if m is not None:
            for fam in PY_FAMILIES:
                try:
                    fam(m, lines, rel, specs)
                except Exception:
                    continue      # one family failing never blocks the others
            try:
                fam_imports(m, lines, rel, specs, os.path.dirname(path), root)
            except Exception:
                pass
    if before is None:
        fam_file_kind(path, rel, specs)
    return specs


def questions_for_turn(changes, root, rng, limit=3):
    """changes: [{"path", "before" (None if created), "after"}]. Returns up to `limit`
    questions, highest priority first, at most one per family."""
    specs = []
    for ch in changes:
        rel = os.path.relpath(ch["path"], root) if ch["path"].startswith(root.rstrip(os.sep) + os.sep) else os.path.basename(ch["path"])
        specs.extend(specs_for_file(ch["path"], rel, ch.get("before"), ch["after"], root))
    specs.sort(key=lambda s: (-s["priority"], s["file"], s["line"]))
    out, families, files = [], set(), {}
    for s in specs:
        if s["family"] in families:
            continue
        if s["family"] == "file-kind" and any(q["evidence"]["file"] == s["file"] for q in out):
            continue
        q = question("t-{}-{}".format(s["family"], len(out)), s["lens"], s["stem"], s["key"], s["why"],
                     s["distractors"], rng, {"file": s["file"], "line": s["line"]})
        if q:
            families.add(s["family"])
            files[s["file"]] = files.get(s["file"], 0) + 1
            out.append(q)
        if len(out) >= limit:
            break
    return out
