"""Phase 2: questions about the code the agent wrote this turn.

Static only: the engine reads the code but never runs it. Running agent code safely needs
an OS sandbox (docs/DESIGN.md, principle 4), and this version doesn't have one, so it asks
only what the syntax proves. Each family has a key that follows from the code and wrong
options that the same code rules out, each tied to a known misconception.

The families live in four files, one per area:
- this file: data structures, scope, control flow, correctness, dependencies, LLM calls;
- complexity.py: running time (Θ) and design choices ("what does a set buy over a list?");
- security.py: weaknesses tied to CWE entries (injection, unsafe loading, weak choices);
- files.py: file access (open modes, `with`, permissions, races, temporary files).

Only lines the agent changed this turn are asked about. At most one question per family,
and the first picks cover different lenses, so one turn mixes security, running time and
data structures instead of asking three variations of one idea.
"""
import ast
import os
import re

import complexity
import files
import security
from common import load_stdlib_names, question
from pyfacts import (AGENT_CAP, ALGORITHMS, CORRECTNESS, DATA, DEPENDENCIES, LLM, LOGIC, ACTION, Module,
                     add_spec, changed_lines, dotted, short, src)

__all__ = ["changed_lines", "questions_for_turn", "specs_for_file"]

_spec = add_spec
DESC = {
    "list": "A list (a dynamic array): ordered, duplicates allowed, Θ(1) access by position",
    "dict": "A dict (a hash table): each key maps to a value, Θ(1) average lookup by key",
    "set": "A set (a hash table): unordered, no duplicates, Θ(1) average membership test",
    "frozenset": "A frozenset: an immutable set, hashed, no duplicates",
    "tuple": "A tuple: an ordered sequence that can't be changed after it's made",
    "deque": "A deque (double-ended queue): Θ(1) appends and pops at both ends",
    "defaultdict": "A defaultdict: a dict that creates a default value for any missing key",
    "Counter": "A Counter: a dict that maps each item to how many times it appears",
}
SYNTAX = {"list": "`[...]` or `list(...)`", "dict": "`{key: value}`, `{}` or `dict(...)`",
          "set": "`{a, b}` or `set(...)`", "tuple": "`(a, b)` or `tuple(...)`"}
HASHED = {"set", "frozenset", "dict", "defaultdict", "Counter"}
SCANNED = {"list", "tuple", "deque"}
PERSISTENT_NAMES = re.compile(r"(cache|memo|seen|visited|history|results?|counts?|total|store|stats|"
                              r"registry|sessions?|users|scores|log)", re.I)
MUTATORS = {"append", "extend", "insert", "add", "update", "setdefault", "pop", "remove",
            "clear", "discard", "popitem", "sort", "reverse", "appendleft", "extendleft"}
ADDERS = {"append", "extend", "insert", "add", "update", "setdefault", "appendleft", "extendleft"}
PLATFORM_ONLY = {"msvcrt", "winreg", "winsound", "curses", "fcntl", "termios", "pwd", "grp", "resource", "tty", "pty",
                 "syslog", "readline", "tkinter", "turtle", "venv", "ensurepip", "idlelib", "crypt", "spwd", "nis"}
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


# ----------------------------------------------------------------- data structures and scope

def fam_container(m, changed, rel, specs):
    for scope in [m.tree] + m.functions():
        for name, (kind, line) in m.stable_kinds(scope).items():
            if line not in changed or kind not in DESC or kind == "frozenset":
                continue
            code = short(m.lines[line - 1].strip(), 60)
            distractors = []
            key_syntax = SYNTAX.get(kind, "`{}(...)` from `collections`".format(kind))
            for other in ("list", "dict", "set", "tuple"):
                if other == kind or len(distractors) == 3:
                    continue
                if other == "dict" and kind in ("defaultdict", "Counter"):
                    continue                     # both are dict subclasses, so "a dict" would be true too
                if kind == "dict" and other == "set" and re.search(r"=\s*\{\s*\}", m.lines[line - 1]):
                    why = "`{}` makes an empty dict. An empty set has to be written `set()`."
                else:
                    why = "{} is written {}. Line {} makes a {}.".format("A " + other, SYNTAX[other], line, kind)
                distractors.append((DESC[other], why))
            add_spec(specs, "container", 4, DATA, line,
                     "Line {} runs `{}`. Which data structure is `{}`, and what is it good at?".format(line, code, name),
                     DESC[kind],
                     "The syntax on line {} ({}) builds a {}. Costs from the Python wiki's TimeComplexity page.".format(
                         line, key_syntax, kind),
                     distractors, rel)


def fam_membership(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))):
            continue
        if node.lineno not in changed or not isinstance(node.comparators[0], ast.Name):
            continue
        name = node.comparators[0].id
        kinds = m.stable_kinds(m.scope_of(node))
        if name not in kinds:
            continue
        kind = kinds[name][0]
        if kind not in HASHED | SCANNED:
            continue
        expr = short(src(m.lines, node), 40)
        loop = m.inside_loop(node)
        scan = "Θ(k) in the worst case: a linear scan, up to k comparisons"
        hashed = "Θ(1) on average: one hash lookup, however big it gets"
        log = ("Θ(log k): a binary search, halving the range each step",
               "Binary search needs sorted data, and `in` never does it. A {} is {}.".format(
                   kind, "scanned from the start" if kind in SCANNED else "looked up by hash"))
        copy = ("Θ(k log k): it sorts `{}` first, then searches".format(name),
                "`in` never sorts or copies `{}`; it reads it as it is.".format(name))
        if kind in SCANNED:
            key = scan
            why = ("`in` on a {} compares items one by one until it finds a match, so the work grows linearly with k.{} "
                   "(Python wiki, TimeComplexity: `x in s` is O(n) for a list.)").format(
                       kind, " It's inside a loop, so that cost repeats on every pass." if loop else "")
            distractors = [(hashed, "That's how sets and dicts work. A {} is scanned item by item.".format(kind)), log, copy]
        else:
            key = hashed
            why = ("A {} is a hash table: `in` computes the item's hash and jumps to where it would be. That's Θ(1) on "
                   "average; many hash collisions could make it slower.").format(kind)
            distractors = [(scan, "That's how lists work. A {} uses hashing to jump straight to the item.".format(kind)), log, copy]
        add_spec(specs, "membership", 6 if loop else 4, ALGORITHMS, node.lineno,
                 "Line {} checks `{}`. When `{}` holds k items, what does that one check cost?".format(node.lineno, expr, name),
                 key, why, distractors, rel)


def fam_lifetime(m, changed, rel, specs):
    for fn in m.functions():
        if fn.decorator_list or isinstance(fn, ast.AsyncFunctionDef) or any(
                isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)) for n in m.own_nodes(fn)):
            continue                             # a cache can hand back the same object; a generator keeps its frame
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
            if kind in ("tuple", "frozenset"):
                continue
            empty = isinstance(stmt.value, (ast.List, ast.Dict, ast.Set)) and not getattr(stmt.value, "elts", getattr(stmt.value, "keys", None)) \
                or (isinstance(stmt.value, ast.Call) and not stmt.value.args and not stmt.value.keywords)
            key = "Each call makes a new{} `{}` in its own frame; nothing carries over".format(", empty" if empty else "", name)
            prio = 7 if PERSISTENT_NAMES.search(name) else 3
            add_spec(specs, "lifetime", prio, DATA, stmt.lineno,
                     "Line {} creates `{}` inside `{}`. What happens to `{}` between calls to `{}`?".format(
                         stmt.lineno, name, fn.name, name, fn.name),
                     key,
                     "Line {} runs every time `{}` is called. `{}` is a local name: it exists only in that call's frame, and "
                     "the next call runs line {} again and binds a brand-new object. (An object the function returned or "
                     "stored elsewhere survives, but it's no longer this name.) To remember things between calls, `{}` has to "
                     "live outside the function.".format(stmt.lineno, fn.name, name, stmt.lineno, name),
                     [("It keeps everything added so far, because a {} lasts until the program ends".format(kind),
                       "A local name belongs to one call. The next call runs line {} again and gets a new {}.".format(stmt.lineno, kind)),
                      ("Every function in the file can use it, because it's a {}".format(kind),
                       "Where a variable is assigned decides its scope, not its type. `{}` is local to `{}`.".format(name, fn.name)),
                      ("It keeps only what the most recent call put in it",
                       "Nothing is kept: line {} runs again at the start of every call.".format(stmt.lineno))],
                     rel)


def fam_mutable_default(m, changed, rel, specs):
    for fn in m.functions():
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
            mutated = added = reassigned = False
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == p:
                    mutated = True
                    added = added or n.func.attr in ADDERS
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) \
                        and n.value.id == p:
                    mutated = added = True
                if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == p:
                    mutated = added = True
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == p for t in n.targets):
                    reassigned = True  # the shared default may never be touched: too subtle to ask
            if reassigned:
                continue
            key = ("The same {}, made once at `def`, so the first call's {} still there".format(
                       kind, "additions are" if added else "changes are")
                   if mutated else "The same {}, made once when `def` ran, shared by both calls".format(kind))
            add_spec(specs, "mutable-default", 8 if mutated else 5, CORRECTNESS, fn.lineno,
                     "Line {} defines `{}` with `{}={}`. If `{}` is called twice without passing `{}`, what do the two calls get?".format(
                         fn.lineno, fn.name, p, src(m.lines, default), fn.name, p),
                     key,
                     "Default values are evaluated once, when the `def` statement runs, not on every call. Every call that "
                     "doesn't pass `{}` shares that one {} object, so changes accumulate. The idiom is `{}=None`, then "
                     "create the {} inside the function.".format(p, kind, p, kind),
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
            if mod in done or mod in version_dependent or mod == "__future__" or mod.startswith("_") or mod in PLATFORM_ONLY:
                continue
            done.add(mod)
            local = os.path.exists(os.path.join(file_dir, mod + ".py")) or os.path.isdir(os.path.join(file_dir, mod))
            if not local and (mod not in stable or os.path.exists(os.path.join(root, mod + ".py"))) \
                    and _found_elsewhere(root, mod):
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
                why = "Python searches the script's own folder first (`sys.path[0]`), and `{}` is there.".format(mod)
                distractors = [(std, "`{}` isn't a standard-library module; it's a file in this project.".format(mod)),
                               (third, "Nothing needs installing: `{}` is right here in the project.".format(mod)), auto]
            elif mod in stable:
                key, prio = std, 2
                why = "`{}` is part of Python's standard library, so it comes with Python; nothing has to be installed.".format(mod)
                distractors = [(third, "`{}` comes with Python; no install needed.".format(mod)),
                               (here, "There's no `{}.py` or `{}/` in this project. Python finds it in its own library.".format(mod, mod)),
                               auto]
            else:
                key, prio = third, 5
                why = ("`{}` isn't in the standard library or in this project, so someone has to install it, or the import "
                       "fails. That brings in its authors' code and its dependencies' code, which runs when it's imported "
                       "(and can run at install time for packages built from source): the software supply chain.").format(mod)
                distractors = [(std, "`{}` isn't part of Python's standard library.".format(mod)),
                               (here, "There's no `{}.py` or `{}/` folder in this project.".format(mod, mod)), auto]
            add_spec(specs, "import", prio, DEPENDENCIES, node.lineno,
                     "Line {} imports `{}`. Where does `{}` come from?".format(node.lineno, mod, mod),
                     key, why, distractors, rel)


# ----------------------------------------------------------------- control flow and correctness

def _loop_has_exit(loop):
    for n in ast.walk(loop):
        if isinstance(n, (ast.Break, ast.Return, ast.Raise)):
            return True
        if isinstance(n, ast.Call) and dotted(n.func) in ("sys.exit", "exit", "quit", "os._exit"):
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
            add_spec(specs, "iteration", 5, LOGIC, line,
                     "On line {}, `for {} in {}:` loops over a dict. What is `{}` on each pass?".format(line, t, d, t),
                     "Each key of `{}`, one per pass, in insertion order".format(d),
                     "Iterating over a dict yields its keys, in the order they were inserted (guaranteed since Python 3.7). "
                     "`.values()` gives values, and `.items()` gives (key, value) pairs.",
                     [("Each value in `{}`".format(d), "Looping over a dict gives its keys. Use `.values()` for values."),
                      ("Each (key, value) pair", "Pairs come from `.items()`. A plain loop over a dict gives keys."),
                      ("Each position: 0, 1, 2, …", "Dicts are looked up by key, not position. The loop gives keys.")], rel)
        elif isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr == "items" and not it.args \
                and isinstance(target, ast.Tuple) and len(target.elts) == 2 and all(isinstance(e, ast.Name) for e in target.elts):
            k, v = target.elts[0].id, target.elts[1].id
            add_spec(specs, "iteration", 5, LOGIC, line,
                     "On line {}, what are `{}` and `{}` on each pass of `for {}, {} in {}:`?".format(line, k, v, k, v, short(src(m.lines, it), 30)),
                     "`{}` is a key and `{}` is that key's value".format(k, v),
                     "`.items()` yields (key, value) pairs, key first, and the loop unpacks each pair into the two names.",
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
            add_spec(specs, "iteration", 5, LOGIC, line,
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
                add_spec(specs, "iteration", 5, LOGIC, line,
                         "On line {}, what values does `{}` take in `for {} in range(len({})):`?".format(line, t, t, seq),
                         "Each index of `{}`: 0, 1, …, up to one less than its length".format(seq),
                         "`range(n)` counts from 0 up to n − 1, which are exactly the valid indices of a sequence of length n.",
                         [("Each item of `{}`".format(seq), "`range(len(...))` gives numbers, the indices, not the items."),
                          ("The indices 1, 2, …, up to its length", "`range` starts at 0 and stops before its argument."),
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
                add_spec(specs, "loop-count", 5, ALGORITHMS, line,
                         "How many times does the body of the loop on line {} (`for … in {}`) run?".format(line, shown),
                         fmt(n),
                         "`{}` produces {}: it starts at {} and stops before {}.".format(
                             shown, ", ".join(str(v) for v in list(range(*values))[:6]) + (", …" if n > 6 else ""),
                             values[0] if len(values) > 1 else 0, values[1] if len(values) > 1 else values[0]),
                         [(fmt(x), "Count again: `range` includes its start and stops just before its end.") for x in opts[:3]], rel)
        elif isinstance(it, ast.Call) and dotted(it.func) == "zip" and len(it.args) == 2 and not it.keywords \
                and isinstance(target, ast.Tuple) and len(target.elts) == 2:
            a, b = (short(src(m.lines, x), 20) for x in it.args)
            add_spec(specs, "iteration", 5, LOGIC, line,
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
        if len(node.handlers) != 1:
            continue                             # another handler may treat some errors differently
        for h in node.handlers:
            broad = h.type is None or dotted(h.type) in ("Exception", "BaseException")
            body_is_nothing = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                                  or isinstance(s, ast.Continue) for s in h.body)
            if not (broad and body_is_nothing) or h.lineno not in changed:
                continue
            cont = any(isinstance(s, ast.Continue) for s in h.body)
            key = ("The error is silently ignored, and the loop moves on to the next item" if cont
                   else "The error is silently ignored, and the code after the `try` runs")
            add_spec(specs, "swallowed", 7, CORRECTNESS, h.lineno,
                     "If the code in the `try` block (line {}) raises an error, what happens?".format(node.lineno),
                     key,
                     "Line {} catches {} and does nothing with it. The program carries on with missing or wrong data, and "
                     "nobody sees why: CWE-1069, an empty exception block. Catch the specific exception you expect, and log "
                     "or handle it.".format(h.lineno, "every ordinary error (any subclass of `Exception`)"
                                            if dotted(h.type) == "Exception" else
                                            "every exception, even KeyboardInterrupt and SystemExit"),
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
                add_spec(specs, "llm-limit", 5, LLM, node.lineno,
                         "What caps the length of the reply from the model call on line {}?".format(node.lineno),
                         "`{}={}`: the reply stops after at most {} tokens".format(kw.arg, k, k),
                         "`{}` is a hard limit: generation is cut off at {} tokens. It caps the length, and so the output "
                         "cost, of this one reply, not what the reply says. Limits like it are one defense against unbounded "
                         "consumption (OWASP LLM10).".format(kw.arg, k),
                         [("Nothing: the model always writes until it's done",
                           "`{}` cuts the reply off when it reaches the limit.".format(kw.arg)),
                          ("The prompt's length: a reply can't be longer than the question",
                           "Reply length doesn't depend on prompt length; only the token limit caps it."),
                          ("`temperature`, which sets the maximum length",
                           "`temperature` changes how random the wording is, not how long it is.")], rel)
                break


def fam_flow(m, changed, rel, specs):
    for fn in m.functions():
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
                add_spec(specs, "flow", 4, LOGIC, node.lineno,
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
        add_spec(specs, "immutable-str", 7, CORRECTNESS, node.lineno,
                 "Line {} runs `{}` on its own. What happens to `{}`?".format(node.lineno, code, name),
                 "Nothing: `{}()` returns a new string, which is thrown away".format(meth),
                 "Python strings are immutable: their methods return a new string instead of changing the old one. To keep "
                 "the result, write `{} = {}`.".format(name, code),
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
    add_spec(specs, "file-kind", 1, ACTION, 1,
             "{} created `{}`. What kind of file is it?".format(AGENT_CAP, rel), key,
             "The file's ending (`{}`) says what kind of file it is.".format(ext or base), ds, rel)


# ----------------------------------------------------------------- entry point

LENS_PENALTY = 3
PY_FAMILIES = ([fam_mutable_default, fam_swallowed, fam_discarded_str, fam_lifetime, fam_membership,
                fam_iteration, fam_llm_limit, fam_flow, fam_container]
               + security.FAMILIES + complexity.FAMILIES + files.FAMILIES)


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
    covered = {(fam, line) for s in specs for fam, line in s.get("supersedes", [])}
    return [s for s in specs if (s["family"], s["line"]) not in covered]


def questions_for_turn(changes, root, rng, limit=3):
    """changes: [{"path", "before" (None if created), "after"}]. Returns up to `limit`
    questions, highest priority first, at most one per family, spread across lenses so a
    turn covers different kinds of understanding."""
    specs = []
    for ch in changes:
        rel = os.path.relpath(ch["path"], root) if ch["path"].startswith(root.rstrip(os.sep) + os.sep) else os.path.basename(ch["path"])
        specs.extend(specs_for_file(ch["path"], rel, ch.get("before"), ch["after"], root))
    specs.sort(key=lambda s: (-s["priority"], s["file"], s["line"]))
    out, families, lens_count = [], set(), {}
    candidates = list(specs)
    while candidates and len(out) < limit:
        # Highest priority first, but each question already asked in a lens costs that lens
        # LENS_PENALTY points, so a strong question in a new lens beats a third variation.
        best = max(candidates, key=lambda s: s["priority"] - LENS_PENALTY * lens_count.get(s["lens"], 0))
        candidates.remove(best)
        if best["family"] in families:
            continue
        if best["family"] == "file-kind" and any(q["evidence"]["file"] == best["file"] for q in out):
            continue
        q = question("t-{}-{}".format(best["family"], len(out)), best["lens"], best["stem"], best["key"], best["why"],
                     best["distractors"], rng, {"file": best["file"], "line": best["line"], "family": best["family"]})
        if q:
            families.add(best["family"])
            lens_count[best["lens"]] = lens_count.get(best["lens"], 0) + 1
            out.append(q)
    return out
