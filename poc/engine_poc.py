#!/usr/bin/env python3
"""SP1 Gate question-engine proof of concept (Python only, throwaway).

Shows, on real code, the three moves docs/QUESTION_ENGINE.md depends on:

  1. Facts come from the code (Python's ast module): which data structures,
     how loops nest, what is local to a call, where untrusted text flows.
  2. Answer keys come from running or analyzing the code: the right option
     is what the code actually does, never what a model says it does.
  3. Wrong options come from executed mutants and a misconception catalog.
     A mutant is the same code with one small, realistic bug (a flipped
     test, a dropped +1, a moved line), run on the same input. Outputs equal
     to the key are thrown away, so a distractor can never be right.

    python3 poc/engine_poc.py              # every example
    python3 poc/engine_poc.py find_index   # one example

This is evidence for the design, not the design: no adapters, no UI, no
learner model, one language, deliberately small analyses.

WARNING: this script runs code with your own permissions. It only uses a
separate process, a timeout and a temporary working folder, which is NOT a
sandbox. Run it on the included examples only. The real engine must run
every probe inside an OS sandbox (docs/DESIGN.md, principle 4).
"""
import ast
import copy
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, "examples")
OUT = os.path.join(HERE, "out")
LETTERS = "ABCD"


def read(name):
    with open(os.path.join(EXAMPLES, name)) as f:
        return f.read()


# =============================================================================
# 1. Running code: keys come from what the code actually does
# =============================================================================

RUNNER = r'''
import ast, importlib.util, sys

AGENT = sys.argv[1]
PLAIN = (int, float, str, bool, bytes, type(None))


class Cycle(Exception):
    """A while loop reached the exact same state twice: in deterministic code it can never finish."""


def plain(value, depth=0):
    if isinstance(value, PLAIN):
        return True
    if depth < 4 and isinstance(value, (list, tuple)):
        return all(plain(v, depth + 1) for v in value)
    if depth < 4 and isinstance(value, dict):
        return all(plain(k, depth + 1) and plain(v, depth + 1) for k, v in value.items())
    return False


def unordered(value, depth=0):
    if isinstance(value, (set, frozenset)):
        return True
    if depth < 4 and isinstance(value, (list, tuple)):
        return any(unordered(v, depth + 1) for v in value)
    if depth < 4 and isinstance(value, dict):
        return any(unordered(v, depth + 1) for v in value.values())
    return False


def call_free_while_lines(path):
    # Only while loops whose code calls nothing (no clocks, no randomness, no I/O)
    # are candidates: there, a repeated state proves the loop never ends.
    tree = ast.parse(open(path).read())
    return {n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.While) and not any(isinstance(c, ast.Call) for c in ast.walk(n))}


def main():
    try:
        spec = importlib.util.spec_from_file_location("agent_code", AGENT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    except Exception as e:
        return "!raises " + type(e).__name__
    ns = {}
    exec(open(sys.argv[2]).read(), ns)

    headers = call_free_while_lines(AGENT)
    seen = set()

    def local_trace(frame, event, arg):
        if event == "line" and frame.f_lineno in headers:
            values = dict(frame.f_locals)
            if all(plain(v) for v in values.values()):
                state = (frame.f_lineno, repr(sorted(values.items())))
                if state in seen:
                    raise Cycle()
                if len(seen) < 100000:
                    seen.add(state)
        return local_trace

    def global_trace(frame, event, arg):
        return local_trace if frame.f_code.co_filename == AGENT else None

    try:
        sys.settrace(global_trace)
        result = ns["probe"](m) if "probe" in ns else eval(ns["SHOW"], vars(m))
    except Cycle:
        return "!cycle"
    except Exception as e:
        return "!raises " + type(e).__name__
    finally:
        sys.settrace(None)
    if unordered(result):
        return "!unordered"
    return repr(result)

print(main())
'''

TIMER = r'''
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location("agent_code", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ns = {}
exec(open(sys.argv[2]).read(), ns)
times = []
for n in json.loads(sys.argv[3]):
    args = ns["sized_input"](n)
    start = time.perf_counter()
    ns["call"](m, args)  # warm-up run, not counted
    once = time.perf_counter() - start
    # Best of at least 5 runs; fast sizes get more runs (up to 50), so one hiccup can't skew them.
    runs = min(50, max(5, int(0.2 / max(once, 1e-9))))
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        ns["call"](m, args)
        best = min(best, time.perf_counter() - start)
    times.append(best)
print(json.dumps(times))
'''

AGREE = r'''
import importlib.util, json, sys

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

a, b = load(sys.argv[1], "version_a"), load(sys.argv[2], "version_b")
ns = {}
exec(open(sys.argv[3]).read(), ns)
cases = ns["random_inputs"]()
differ = sum(1 for c in cases if ns["call"](a, list(c)) != ns["call"](b, list(c)))
print(json.dumps({"cases": len(cases), "differ": differ}))
'''


def python(script, files, args, seed=0, timeout=3.0):
    """Run a script in a fresh process and temp folder. Returns its last stdout line."""
    with tempfile.TemporaryDirectory() as d:
        paths = {}
        for name, text in files.items():
            paths[name] = os.path.join(d, name)
            with open(paths[name], "w") as f:
                f.write(text)
        runner = os.path.join(d, "_run.py")
        with open(runner, "w") as f:
            f.write(script)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": str(seed)}
        argv = [sys.executable, "-s", "-B", runner] + [paths.get(a, a) for a in args]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=d)
        except subprocess.TimeoutExpired:
            return "!timeout"
        lines = r.stdout.strip().splitlines()
        if lines:
            return lines[-1]
        err = r.stderr.strip().splitlines()
        return "!error " + (err[-1] if err else "no output")


def run(source, probe_text, seed):
    files = {"agent_code.py": source, "probe.py": probe_text}
    return python(RUNNER, files, ["agent_code.py", "probe.py"], seed=seed, timeout=2.0)


SEEDS = range(1, 9)


def run_stable(source, probe_text):
    """Run with eight different hash seeds. Output that changes between runs can't be
    a key or a distractor, so it returns None. Results that contain a set are refused
    outright ('!unordered'), because a lucky run of seeds can hide their ordering."""
    first = run(source, probe_text, SEEDS[0])
    for seed in SEEDS[1:]:
        if run(source, probe_text, seed) != first:
            return None
    return first


def slope(xs, ys):
    """Least-squares slope of log(y) against log(x): the measured growth exponent."""
    lx, ly = [math.log(x) for x in xs], [math.log(y) for y in ys]
    mx, my = sum(lx) / len(lx), sum(ly) / len(ly)
    return sum((a - mx) * (b - my) for a, b in zip(lx, ly)) / sum((a - mx) ** 2 for a in lx)


def growth(source, probe_text, sizes=(1000, 2000, 4000, 8000)):
    files = {"agent_code.py": source, "probe.py": probe_text}
    out = python(TIMER, files, ["agent_code.py", "probe.py", json.dumps(list(sizes))], timeout=120)
    times = json.loads(out)
    return round(slope(sizes, times), 2), dict(zip(sizes, times))


TOLERANCE = 0.3


def confirmed_growth(source, probe_text, degree, tries=3):
    """Measure up to `tries` times, because a busy computer can slow one run. Returns the
    first (exponent, timings) within TOLERANCE of `degree`, or None. None means the rule
    wasn't confirmed, so the question isn't asked: fewer questions, never a shaky key."""
    for _ in range(tries):
        exponent, times = growth(source, probe_text)
        if abs(exponent - degree) < TOLERANCE:
            return exponent, times
    return None


def agree(source_a, source_b, probe_text):
    files = {"a.py": source_a, "b.py": source_b, "probe.py": probe_text}
    return json.loads(python(AGREE, files, ["a.py", "b.py", "probe.py"], timeout=30))


# =============================================================================
# 2. Mutants: one small, realistic bug at a time
# =============================================================================

def slots(tree):
    """Every (parent, field, index, node) in a fixed order, so a copy of the
    tree lines up with the original position by position."""
    out = []

    def visit(parent):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                out.append((parent, field, None, value))
                visit(value)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        out.append((parent, field, i, item))
                        visit(item)

    visit(tree)
    return out


def put(parent, field, index, new):
    if index is None:
        setattr(parent, field, new)
    else:
        getattr(parent, field)[index] = new


def kind_of(expr):
    """'list', 'set' or 'dict' when the expression builds that container."""
    if isinstance(expr, ast.List):
        return "list"
    if isinstance(expr, ast.Set):
        return "set"
    if isinstance(expr, ast.Dict):
        return "dict"
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in ("list", "set", "dict"):
        return expr.func.id
    return None


def is_empty(expr):
    if isinstance(expr, (ast.List, ast.Set)):
        return not expr.elts
    if isinstance(expr, ast.Dict):
        return not expr.keys
    if isinstance(expr, ast.Call):
        return not expr.args and not expr.keywords
    return False


class FlipBoundary:
    bug = "a boundary (off-by-one) bug"
    SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}

    def matches(self, parent, field, node):
        return isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in self.SWAP

    def apply(self, tree, parent, field, index, node):
        before = ast.unparse(node)
        node.ops = [self.SWAP[type(node.ops[0])]()]
        return f"line {node.lineno} read `{ast.unparse(node)}` instead of `{before}`", None


class FlipMembership:
    bug = "an inverted test"
    SWAP = {ast.In: ast.NotIn, ast.NotIn: ast.In}

    def matches(self, parent, field, node):
        return isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in self.SWAP

    def apply(self, tree, parent, field, index, node):
        before = ast.unparse(node)
        node.ops = [self.SWAP[type(node.ops[0])]()]
        return f"line {node.lineno} read `{ast.unparse(node)}` instead of `{before}`", None


class DropOne:
    bug = "an off-by-one bug"

    def matches(self, parent, field, node):
        return (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub))
                and isinstance(node.right, ast.Constant) and node.right.value == 1)

    def apply(self, tree, parent, field, index, node):
        before = ast.unparse(node)
        put(parent, field, index, node.left)
        return f"line {node.lineno} used `{ast.unparse(node.left)}` instead of `{before}`", None


class DropGuard:
    bug = "a missing check"

    def matches(self, parent, field, node):
        return (isinstance(node, ast.If) and not node.orelse and len(node.body) == 1
                and isinstance(getattr(parent, field, None), list))

    def apply(self, tree, parent, field, index, node):
        getattr(parent, field)[index:index + 1] = node.body
        return (f"line {node.body[0].lineno} ran without the check `if {ast.unparse(node.test)}:` "
                f"on line {node.lineno}"), None


class SwapContainer:
    bug = "a different data structure"
    METHODS = {"set": ("append", "add"), "list": ("add", "append")}

    def matches(self, parent, field, node):
        return (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and kind_of(node.value) in ("list", "set") and is_empty(node.value))

    def apply(self, tree, parent, field, index, node):
        name, old = node.targets[0].id, kind_of(node.value)
        new = "set" if old == "list" else "list"
        node.value = ast.parse("set()" if new == "set" else "[]", mode="eval").body
        method_from, method_to = self.METHODS[new]
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == name and n.func.attr == method_from):
                n.func.attr = method_to
        return f"`{name}` on line {node.lineno} were a {new} instead of a {old}", name


class HoistLocal:
    bug = "state that lasts between calls"

    def matches(self, parent, field, node):
        return (isinstance(parent, ast.FunctionDef) and field == "body" and isinstance(node, ast.Assign)
                and kind_of(node.value) is not None and is_empty(node.value))

    def apply(self, tree, parent, field, index, node):
        del parent.body[index]
        tree.body.insert(tree.body.index(parent), node)
        return f"`{ast.unparse(node)}` sat at module level instead of inside `{parent.name}`", node.targets[0].id


OPERATORS = [FlipBoundary(), FlipMembership(), DropOne(), DropGuard(), SwapContainer(), HoistLocal()]


def mutants(source):
    tree = ast.parse(source)
    base = slots(tree)
    out = []
    for op in OPERATORS:
        for i, (parent, field, index, node) in enumerate(base):
            if not op.matches(parent, field, node):
                continue
            copy_tree = copy.deepcopy(tree)
            change, target = op.apply(copy_tree, *slots(copy_tree)[i])
            ast.fix_missing_locations(copy_tree)
            text = ast.unparse(copy_tree)
            try:
                compile(text, "<mutant>", "exec")
            except SyntaxError:
                continue
            out.append({"op": type(op).__name__, "bug": op.bug, "change": change, "target": target, "source": text})
    return out


# =============================================================================
# 3. Static facts: data structures, scope, running time, taint
# =============================================================================

def function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def containers(fn):
    """Local names bound to a container: name -> (kind, line)."""
    found = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            k = kind_of(n.value)
            if k:
                found[n.targets[0].id] = (k, n.lineno)
    return found


def created_per_call(fn):
    """Containers assigned at the top of a function body (not declared global): new on every call."""
    declared = {name for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal)) for name in n.names}
    return [(n.targets[0].id, kind_of(n.value), n.lineno) for n in fn.body
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
            and kind_of(n.value) and n.targets[0].id not in declared]


class RunningTime:
    """Worst-case running time for simple functions, as (power of n, power of log n).

    It knows a handful of patterns (loops over the input, halving loops, list
    vs hash membership, sorting) and returns None for anything else, so the
    engine never asks a running-time question it can't back up.
    """
    CONSTANT_CALLS = {"len", "append", "add", "get", "pop", "strip", "items", "keys", "values", "range"}

    def __init__(self, fn):
        self.params = {a.arg for a in fn.args.args}
        self.kinds = {name: k for name, (k, _) in containers(fn).items()}
        self.fn = fn
        self.rules = []  # (line, rule id, explanation)

    def estimate(self):
        return self.block(self.fn.body)

    @staticmethod
    def dominant(*costs):
        if any(c is None for c in costs):
            return None
        return max(costs) if costs else (0, 0)

    def expr(self, node):
        if node is None:
            return (0, 0)
        cost = (0, 0)
        for n in ast.walk(node):
            if isinstance(n, ast.Compare):
                for op, right in zip(n.ops, n.comparators):
                    if not isinstance(op, (ast.In, ast.NotIn)):
                        continue
                    if not isinstance(right, ast.Name):
                        return None
                    kind = self.kinds.get(right.id, "list" if right.id in self.params else None)
                    if kind == "list":
                        cost = max(cost, (1, 0))
                        self.rules.append((n.lineno, "list-scan",
                                           f"`{ast.unparse(n)}` checks the list `{right.id}` item by item"))
                    elif kind in ("set", "dict"):
                        self.rules.append((n.lineno, "hash-lookup",
                                           f"`{ast.unparse(n)}` is a hash lookup: O(1) on average"))
                    else:
                        return None
            elif isinstance(n, ast.Call):
                name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                if name in ("sorted", "sort"):
                    cost = max(cost, (1, 1))
                elif name == "insert":
                    cost = max(cost, (1, 0))
                elif name in ("list", "set", "dict", "tuple"):
                    cost = max(cost, (1, 0) if n.args else (0, 0))  # empty is O(1); copying is O(n)
                elif name not in self.CONSTANT_CALLS:
                    return None
        return cost

    def block(self, stmts):
        return self.dominant(*[self.stmt(s) for s in stmts])

    def stmt(self, s):
        if isinstance(s, ast.For):
            times = self.loop_times(s)
            body, head = self.block(s.body + s.orelse), self.expr(s.iter)
            if None in (times, body, head):
                return None
            self.rules.append((s.lineno, "loop", f"the loop on line {s.lineno} runs once per item of `{ast.unparse(s.iter)}`"))
            return self.dominant(head, (body[0] + times[0], body[1] + times[1]))
        if isinstance(s, ast.While):
            if not self.halving(s):
                return None
            body, test = self.block(s.body), self.expr(s.test)
            if None in (body, test):
                return None
            self.rules.append((s.lineno, "halving",
                               f"each pass through the loop on line {s.lineno} halves the range it searches"))
            return self.dominant(test, (body[0], body[1] + 1))
        if isinstance(s, ast.If):
            return self.dominant(self.expr(s.test), self.block(s.body), self.block(s.orelse))
        if isinstance(s, (ast.Return, ast.Expr, ast.Assign, ast.AugAssign)):
            return self.expr(s.value)
        if isinstance(s, ast.Pass):
            return (0, 0)
        return None

    def loop_times(self, s):
        it = s.iter
        if isinstance(it, ast.Name) and (it.id in self.params or self.kinds.get(it.id) == "list"):
            return (1, 0)
        if isinstance(it, ast.Call) and getattr(it.func, "id", None) == "range" and it.args:
            last = it.args[-1]
            if isinstance(last, ast.Call) and getattr(last.func, "id", None) == "len":
                return (1, 0)
        return None

    @staticmethod
    def halving(s):
        """lo/hi search pattern: mid = (lo + hi) // 2, then one bound moves strictly above
        mid (lo = mid + k) and the other strictly below it (hi = mid - k), with k >= 1.
        Without that progress (e.g. lo = mid) the range can stop shrinking and loop forever."""
        mid, above, below = None, set(), set()
        for n in ast.walk(s):
            if not (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)):
                continue
            v, target = n.value, n.targets[0].id
            if (isinstance(v, ast.BinOp) and isinstance(v.op, ast.FloorDiv)
                    and isinstance(v.right, ast.Constant) and v.right.value == 2):
                mid = target
            elif (mid and isinstance(v, ast.BinOp) and isinstance(v.left, ast.Name) and v.left.id == mid
                    and isinstance(v.right, ast.Constant) and isinstance(v.right.value, int) and v.right.value >= 1):
                (above if isinstance(v.op, ast.Add) else below if isinstance(v.op, ast.Sub) else set()).add(target)
        return mid is not None and bool(above) and bool(below) and not (above & below)


SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def theta(cost):
    """Tight bound. Options use Θ, not O: O is only an upper bound, so for a Θ(log n)
    function, O(n) and O(n log n) would also be true and the item would have three keys."""
    p, q = cost
    parts = []
    if p == 1:
        parts.append("n")
    elif p > 1:
        parts.append("n" + str(p).translate(SUPERSCRIPT))
    if q == 1:
        parts.append("log n")
    elif q > 1:
        parts.append("log" + str(q).translate(SUPERSCRIPT) + " n")
    return "Θ(" + (" ".join(parts) or "1") + ")"


MODEL_CALLS = ("messages.create", "chat.completions.create", "responses.create", "generate_content")
COMMAND_SINKS = ("subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_output",
                 "subprocess.check_call", "os.system", "os.popen", "eval", "exec")


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return ""


def taint(source, fn_name):
    """Follow a function's parameters (treated as user-controlled) through assignments,
    f-strings and model calls to anything that runs a command. Returns findings: each is
    the sink line plus the path of lines that led there. A lookup in a module-level dict
    of constants (an allowlist) cleans the value: the result can only be one of the constants."""
    tree = ast.parse(source)
    fn = function(tree, fn_name)
    lines = source.splitlines()
    constant_tables = {
        n.targets[0].id for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Dict)
        and all(isinstance(v, (ast.Constant, ast.List, ast.Tuple)) for v in n.value.values)
    }
    # A table only counts as an allowlist if nothing can change it: bound once, at module
    # level, and never assigned, deleted, subscripted for writing or mutated anywhere.
    mutators = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            constant_tables -= set(n.names)
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) and isinstance(n.value, ast.Name):
            constant_tables.discard(n.value.id)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in mutators \
                and isinstance(n.func.value, ast.Name):
            constant_tables.discard(n.func.value.id)
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and n not in tree.body:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    constant_tables.discard(t.id)
    bindings = [t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)]
    constant_tables = {name for name in constant_tables if bindings.count(name) == 1}

    tainted = {a.arg: [(fn.lineno, f"`{a.arg}` is a parameter the caller controls")] for a in fn.args.args}
    findings = []

    def origin(expr):
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) and expr.value.id in constant_tables:
            return None
        if isinstance(expr, ast.Name):
            return tainted.get(expr.id)
        if isinstance(expr, ast.Call) and dotted(expr.func).endswith(MODEL_CALLS):
            for arg in list(expr.args) + [k.value for k in expr.keywords]:
                path = origin(arg)
                if path:
                    return path
            return None
        for child in ast.iter_child_nodes(expr):
            path = origin(child)
            if path:
                return path
        return None

    def visit(stmts):
        for s in stmts:
            if isinstance(s, (ast.Assign, ast.AugAssign)):
                path = origin(s.value)
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                if path:
                    for t in targets:
                        for name in (n for n in ast.walk(t) if isinstance(n, ast.Name)):
                            tainted[name.id] = path + [(s.lineno, lines[s.lineno - 1].strip())]
            elif isinstance(s, ast.For):
                path = origin(s.iter)
                if path:
                    for name in (n for n in ast.walk(s.target) if isinstance(n, ast.Name)):
                        tainted[name.id] = path + [(s.lineno, lines[s.lineno - 1].strip())]
                visit(s.body)
                visit(s.orelse)
            elif isinstance(s, (ast.If, ast.While)):
                visit(s.body)
                visit(s.orelse)
            elif isinstance(s, ast.With):
                visit(s.body)
            # Compound statements: only their own header is checked here; their bodies were
            # visited above, so every sink is checked exactly once.
            if isinstance(s, (ast.If, ast.While)):
                heads = [s.test]
            elif isinstance(s, ast.For):
                heads = [s.iter]
            elif isinstance(s, ast.With):
                heads = [item.context_expr for item in s.items]
            else:
                heads = [s]
            for head in heads:
                for call in (n for n in ast.walk(head) if isinstance(n, ast.Call)):
                    if dotted(call.func) in COMMAND_SINKS and call.args:
                        path = origin(call.args[0])
                        if path:
                            shell = any(k.arg == "shell" and getattr(k.value, "value", False) is True
                                        for k in call.keywords)
                            findings.append({"line": call.lineno, "sink": ast.unparse(call), "shell": shell,
                                             "path": path + [(call.lineno, lines[call.lineno - 1].strip())]})

    visit(fn.body)
    return findings


# =============================================================================
# 4. Questions
# =============================================================================

def question(qid, lens, stem, key, key_why, distractors, evidence):
    """key: option text. distractors: [(text, why, origin)], at least 3. Every option must
    be different. Order is shuffled with a fixed seed so this demo's output can be
    reproduced. The real engine shuffles with a per-session secret seed, so the agent
    can't predict which letter is the answer (docs/DESIGN.md, section 7)."""
    options = [(key, None, "key")] + distractors[:3]
    texts = [o[0] for o in options]
    assert len(options) == 4, f"{qid}: need 3 distractors, have {len(distractors)}"
    assert len(set(texts)) == 4, f"{qid}: duplicate options {texts}"
    order = list(range(4))
    random.Random(qid).shuffle(order)
    shown = [options[i] for i in order]
    return {
        "id": qid,
        "lens": lens,
        "stem": stem,
        "options": [{"letter": LETTERS[k], "text": o[0], "why": o[1], "origin": o[2]} for k, o in enumerate(shown)],
        "answer": LETTERS[order.index(0)],
        "explain": key_why,
        "evidence": evidence,
    }


UNUSABLE = {
    None: "output changes from run to run",
    "!timeout": "timed out; a timeout proves nothing about whether it would finish",
    "!unordered": "the result contains a set, whose order isn't fixed",
}


def usable_key(value):
    assert value not in UNUSABLE and not value.startswith("!error"), f"no provable key: {value}"
    return value


def mutant_distractors(source, probe_text, key, fmt):
    """Run every mutant; keep outputs that differ from the key, are provable, and are new."""
    seen, picks, log = {key}, [], []
    for m in mutants(source):
        out = run_stable(m["source"], probe_text)
        if out in UNUSABLE or (out or "").startswith("!error"):
            log.append(f"{m['change']}: not usable ({UNUSABLE.get(out, out)})")
            continue
        if out == key:
            log.append(f"{m['change']}: same result as the real code on this input, not a distractor")
            continue
        log.append(f"{m['change']}: {fmt(out)}")
        if out not in seen:
            seen.add(out)
            picks.append((fmt(out), f"You'd get this if {m['change']}: {m['bug']}.", "executed mutant"))
    return picks, log


def fmt_value(v):
    if v == "!cycle":
        return "Nothing: the loop never ends"
    if v.startswith("!raises "):
        return f"It raises {v[len('!raises '):]}"
    return f"`{v}`"


def fmt_count(v):
    if v.startswith("!"):
        return fmt_value(v)
    return f"{v} time" + ("" if v == "1" else "s")


# ---------------------------------------------------------------- the examples

def example_first_repeat():
    source, probe = read("first_repeat.py"), read("first_repeat_probe.py")
    fn = function(ast.parse(source), "first_repeat")
    questions = []

    # Q1 running time: the rules give the key; timing growing worst-case inputs checks the
    # polynomial degree (timing at these sizes can't separate log factors).
    rt = RunningTime(fn)
    cost = rt.estimate()
    rules = {r for _, r, _ in rt.rules}
    assert cost == (2, 0) and "list-scan" in rules
    confirmed = confirmed_growth(source, probe, degree=2)
    if confirmed is None:
        print("note: first_repeat: timing didn't confirm Θ(n²) in 3 tries (is the computer busy?), "
              "so both first_repeat questions are skipped. Run it again when the computer is idle.",
              file=sys.stderr)
        return source, questions
    measured, times = confirmed
    questions.append(question(
        "first_repeat-time", "algo",
        "`emails` holds n different addresses. How does the running time of `first_repeat` grow as n grows?",
        theta(cost),
        "The loop runs n times, and `e in seen` on line 5 checks the list `seen` item by item. "
        "`seen` grows by one each pass, so the checks add up to 0 + 1 + … + (n − 1) = n(n − 1)/2 "
        "comparisons: Θ(n²). (Θ is a tight bound: it grows in proportion to n², no faster and no slower.)",
        [
            (theta((1, 0)), "One visible loop, but `in` on a list is a hidden loop over `seen`.", "misconception: hidden loop"),
            (theta((1, 1)), "`in` on a list doesn't search by halves; lists aren't kept sorted.", "misconception: lists are searched like sorted arrays"),
            (theta((0, 0)), "It returns early only when it finds a repeat. With n different addresses it never does, so it checks everything.", "misconception: best case taken for worst case"),
        ],
        {"static": [text for _, _, text in rt.rules], "measured_exponent": measured, "timings_s": times},
    ))

    # Q2 design counterfactual: swap seen to a set; check same results, measure the new growth.
    variant = next(m for m in mutants(source) if m["op"] == "SwapContainer" and m["target"] == "seen")
    key_out = usable_key(run_stable(source, probe))
    variant_out = run_stable(variant["source"], probe)
    agreement = agree(source, variant["source"], probe)
    variant_cost = RunningTime(function(ast.parse(variant["source"]), "first_repeat")).estimate()
    assert variant_out == key_out and agreement["differ"] == 0 and variant_cost == (1, 0)
    confirmed = confirmed_growth(variant["source"], probe, degree=1)
    faster_everywhere = confirmed is not None and all(confirmed[1][n] < times[n] for n in times)
    if not faster_everywhere:
        print("note: first_repeat: timing didn't confirm the set version's Θ(n), or that it's faster "
              "at every size, so the design question is skipped.", file=sys.stderr)
        return source, questions
    variant_measured, variant_times = confirmed
    questions.append(question(
        "first_repeat-set", "design",
        "A reviewer suggests changing line 3 to `seen = set()` and line 7 to `seen.add(e)`. "
        "What would change when `emails` is long?",
        "Same results, and the running time becomes Θ(n) on average instead of Θ(n²)",
        "Checking membership in a set is a hash lookup, O(1) on average, so the loop does a constant "
        "amount of work per email. The function still returns the first repeat, because the loop order "
        "is unchanged. (\"On average\": many hash collisions could make lookups slower.)",
        [
            ("Nothing: `in` checks a set item by item, just like a list",
             "Sets are hash tables: `in` jumps to where the item would be instead of scanning.", "misconception: all containers are scanned"),
            ("It gets faster, but it can return a different email, because sets have no order",
             "The order that matters here is the loop's order over `emails`, which doesn't change. `seen` is only asked yes/no questions.", "misconception: set order leaks into the result"),
            ("It gets slower, because hashing an email costs more than comparing two emails",
             "Hashing costs a little per lookup, but it replaces a scan of the whole list. "
             "Measured, the set version was faster at every size tried, from 1,000 to 8,000 emails.", "misconception: constant cost mistaken for growth"),
        ],
        {"same_result_on_probe": variant_out == key_out, "random_inputs": agreement,
         "static_variant": theta(variant_cost), "measured_exponent_variant": variant_measured,
         "timings_s_variant": variant_times, "set_version_faster_at_every_size": faster_everywhere},
    ))
    return source, questions


def example_find_index():
    source, probe = read("find_index.py"), read("find_index_probe.py")
    fn = function(ast.parse(source), "find_index")
    questions = []

    key = usable_key(run_stable(source, probe))
    picks, log = mutant_distractors(source, probe, key, fmt_value)
    questions.append(question(
        "find_index-trace", "algo",
        "What does `find_index([3, 8, 15, 21, 42], 42)` return?",
        fmt_value(key),
        "Trace it: lo=0, hi=4 → mid=2 (15 < 42, so lo=3) → mid=3 (21 < 42, so lo=4) → mid=4, and "
        "nums[4] is 42, so it returns 4. The loop needs `<=` so it still runs when lo and hi meet.",
        picks,
        {"key_from": "running the real code", "mutants": log},
    ))

    rt = RunningTime(fn)
    cost = rt.estimate()
    assert cost == (0, 1)
    questions.append(question(
        "find_index-time", "algo",
        "In the worst case, how does the running time of `find_index` grow with the length n of `nums`?",
        theta(cost),
        "Each pass either returns or moves lo above mid or hi below it, so the range still in play at "
        "least halves. A range of n can halve only about log₂ n times before it's empty.",
        [
            (theta((1, 0)), "The loop doesn't visit every element; each pass throws away half of what's left.", "misconception: every loop is linear"),
            (theta((0, 0)), "It can stop on the first comparison, but that's the best case. The worst case keeps halving.", "misconception: best case taken for worst case"),
            (theta((1, 1)), "Θ(n log n) is the cost of sorting. This function assumes `nums` is already sorted.", "misconception: search confused with sort"),
        ],
        {"static": [text for _, _, text in rt.rules],
         "measured": "not measured: at these sizes timing can't separate log n from a constant, so the rule is the evidence"},
    ))
    return source, questions


def example_profile_cache():
    source, probe = read("profile_cache.py"), read("profile_cache_probe.py")
    fn = function(ast.parse(source), "get_profile")
    questions = []

    locals_ = created_per_call(fn)
    assert locals_ and locals_[0][0] == "cache"
    name, kind, line = locals_[0]
    questions.append(question(
        "profile_cache-scope", "data",
        f"Line {line} creates `{name}` inside `get_profile`. What happens to `{name}` between calls?",
        f"Each call makes a new, empty `{name}`; nothing carries over",
        f"`{name} = {{}}` runs every time `get_profile` runs, and a local variable disappears when the "
        "call returns. To remember results between calls, the dict has to live outside the function.",
        [
            ("It keeps every profile fetched so far, because a dict lasts until the program ends",
             "A dict object can outlive a call only if something outside still refers to it. Here nothing does.", "misconception: local lifetime"),
            ("It keeps only the most recent profile",
             f"Nothing is kept: the next call builds a brand-new empty dict on line {line}.", "misconception: partial persistence"),
            ("Every function in the file can use it, because it's a dict",
             "Being mutable doesn't make a variable global. Scope comes from where it's assigned.", "misconception: mutable means global"),
        ],
        {"static": f"`{name}` is assigned on line {line}, at the top level of `get_profile`, with no `global` or `nonlocal`"},
    ))

    key = usable_key(run_stable(source, probe))
    picks, log = mutant_distractors(source, probe, key, fmt_count)
    picks.append(("6 times",
                  "Reading `cache[user_id]` on line 15 only looks up the dict. It doesn't call `fetch_profile` again.",
                  "misconception: a lookup re-runs the function"))
    questions.append(question(
        "profile_cache-claim", "meta",
        "Wren says: \"I added a cache, so repeated lookups skip the slow fetch.\" "
        "When `get_profile(7)` is called three times in a row, how many times does `fetch_profile` run?",
        fmt_count(key),
        "Each call starts with an empty `cache`, so `user_id not in cache` is always True and every call "
        "fetches. The claim would be true only if the cache lived outside the function.",
        picks,
        {"key_from": "running the real code with a counter on fetch_profile", "mutants": log},
    ))
    return source, questions


def example_fix_ticket():
    source = read("fix_ticket.py")
    spec = importlib.util.spec_from_file_location("fix_ticket_options", os.path.join(EXAMPLES, "fix_ticket_options.py"))
    options_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(options_mod)
    questions = []

    findings = taint(source, "fix_ticket")
    assert len(findings) == 1 and findings[0]["shell"]
    f = findings[0]
    hops = [ln for ln, _ in f["path"]]  # parameter, prompt, model call, reply, sink
    assert len(hops) == 5
    sink = f["line"]
    questions.append(question(
        "fix_ticket-flow", "security",
        "Whoever writes the ticket controls `ticket_text`. Which statement about their text is true?",
        f"It can shape the shell command that line {sink} runs, via the reply",
        f"Follow the value: `ticket_text` goes into `prompt` (line {hops[1]}), the prompt goes to the model "
        f"(line {hops[2]}), the reply becomes `command` (line {hops[3]}), and line {hops[4]} runs it with "
        "`shell=True`. Text that reaches a model's prompt can shape its reply (OWASP LLM01), and running a "
        "reply unchecked is improper output handling (OWASP LLM05).",
        [
            ("It reaches the model, but the model's reply doesn't depend on it",
             "A model's reply depends on everything in its prompt, and the ticket is in the prompt.",
             "misconception: model output is independent of its input"),
            (f"It reaches line {sink} as text that `shell=True` displays but doesn't run",
             "`shell=True` hands the whole string to the system shell, which runs it as a command.",
             "misconception: shell=True"),
            (f"It can't reach line {sink}, because `max_tokens=100` limits the reply",
             "`max_tokens` caps how long the reply is, not what it says. It doesn't cap the prompt either: "
             "a long ticket still costs more.", "misconception: limits mistaken for controls"),
        ],
        {"path": [f"line {ln}: {text}" for ln, text in f["path"]]},
    ))

    analyzed = []
    for text, code, why in options_mod.OPTIONS:
        n = len(taint(code, "fix_ticket"))
        analyzed.append((text, code, why, n))
    safe = [a for a in analyzed if a[3] == 0]
    assert len(safe) == 1, [a[0] for a in safe]
    key = safe[0]
    wrong = [(a[0], a[2], "misconception; the analyzer still finds the path") for a in analyzed if a[3] > 0]
    questions.append(question(
        "fix_ticket-fix", "design",
        "Which change stops the ticket text from writing the command that runs?",
        key[0],
        "Only in the allowlist version does no path turn ticket text into command text: the command comes "
        "from a table of constants, so the ticket can at most pick which preset runs, or none.",
        wrong,
        {"paths_found_per_option": {a[0]: a[3] for a in analyzed}},
    ))
    return source, questions


EXAMPLE_FUNCS = {
    "first_repeat": example_first_repeat,
    "find_index": example_find_index,
    "profile_cache": example_profile_cache,
    "fix_ticket": example_fix_ticket,
}


# =============================================================================
# 5. Report
# =============================================================================

def numbered(source):
    return "\n".join(f"{i:>3}  {line}" for i, line in enumerate(source.splitlines(), 1))


def report(name, source, questions):
    lines = [f"## {name}", "", "```python", numbered(source), "```", ""]
    for q in questions:
        lines.append(f"### {q['id']}  ·  lens: {q['lens']}")
        lines.append(q["stem"])
        lines.append("")
        for o in q["options"]:
            mark = "✓" if o["letter"] == q["answer"] else " "
            lines.append(f"  {mark} {o['letter']}. {o['text']}")
            if o["why"]:
                lines.append(f"       [{o['origin']}] {o['why']}")
        lines.append("")
        lines.append(f"  Why: {q['explain']}")
        lines.append(f"  Evidence: {json.dumps(q['evidence'], ensure_ascii=False, indent=2)}")
        lines.append("")
    return "\n".join(lines)


def main():
    names = sys.argv[1:] or list(EXAMPLE_FUNCS)
    os.makedirs(OUT, exist_ok=True)
    for name in names:
        source, questions = EXAMPLE_FUNCS[name]()
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            json.dump({"example": name, "questions": questions}, f, indent=2, ensure_ascii=False)
        print(report(name, source, questions))


if __name__ == "__main__":
    main()
