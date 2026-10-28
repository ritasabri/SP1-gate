"""Facts about Python code that every question family shares.

Nothing here runs the agent's code. It reads the syntax tree and answers narrow questions
such as "which container does this name hold?" or "is this node inside a loop?". The
families in code_questions.py, complexity.py and security.py build questions on top.
"""
import ast
import difflib

AGENT_CAP = "The AI agent"
CONTAINER_CALLS = {"list": "list", "dict": "dict", "set": "set", "tuple": "tuple", "frozenset": "frozenset"}
COLLECTIONS = {"deque": "deque", "defaultdict": "defaultdict", "Counter": "Counter", "OrderedDict": "dict"}
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
LOOPS = (ast.For, ast.AsyncFor, ast.While)

# The lenses shown on each question's chip. They follow the Stop Pressing 1 taxonomy.
SECURITY = "security"
FILES = "file access"
DATA = "data structures"
ALGORITHMS = "algorithms"
RUNNING_TIME = "running time"
DESIGN = "design choice"
CORRECTNESS = "correctness"
LOGIC = "logic"
DEPENDENCIES = "dependencies"
LLM = "llm integration"
ACTION = "action"


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


def same(a, b):
    """True if two expressions are written the same way (ignoring line numbers)."""
    return ast.dump(a) == ast.dump(b)


def add_spec(specs, family, priority, lens, line, stem, key, why, distractors, file_rel):
    specs.append({"family": family, "priority": priority, "lens": lens, "line": line, "file": file_rel,
                  "stem": stem, "key": key, "why": why, "distractors": distractors})


def rebound_names(target):
    """Names that an assignment target rebinds. `x[i] = v` and `x.a = v` change the
    object x refers to, but x itself still names the same object."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for e in target.elts:
            out |= rebound_names(e)
        return out
    if isinstance(target, ast.Starred):
        return rebound_names(target.value)
    return set()


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

    # -- containers ---------------------------------------------------------

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
        while p is not None and not isinstance(p, FUNCS + (ast.Lambda,)):
            p = self.parents.get(p)
        return p if p is not None else self.tree

    def own_nodes(self, scope):
        """Every node in scope, without descending into nested functions or classes."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            n = stack.pop()
            yield n
            if isinstance(n, FUNCS + (ast.Lambda, ast.ClassDef)):
                continue
            stack.extend(ast.iter_child_nodes(n))

    def functions(self):
        return [n for n in ast.walk(self.tree) if isinstance(n, FUNCS)]

    def stable_kinds(self, scope):
        """name -> (kind, first line) for names that only ever hold one container kind in
        this scope: every binding is a plain assignment of that kind, and the name isn't a
        parameter, a loop variable, global, nonlocal, or deleted."""
        kinds, first, unstable = {}, {}, set()
        if isinstance(scope, FUNCS + (ast.Lambda,)):
            a = scope.args
            unstable |= {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            if a.vararg:
                unstable.add(a.vararg.arg)
            if a.kwarg:
                unstable.add(a.kwarg.arg)
        for n in ast.walk(scope):
            if n is not scope and isinstance(n, FUNCS + (ast.Lambda, ast.ClassDef)):
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
                        unstable |= rebound_names(t)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
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

    def uses(self, name, scope):
        """(node, parent) for every load of `name` in scope (nested functions excluded)."""
        out = []
        for n in self.own_nodes(scope):
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
                out.append((n, self.parents.get(n)))
        return out

    def method_calls(self, name, scope):
        """Calls like `name.append(x)` in scope: list of (method name, call node)."""
        out = []
        for n in self.own_nodes(scope):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == name:
                out.append((n.func.attr, n))
        return out

    # -- control flow -------------------------------------------------------

    def inside_loop(self, node, stop=None):
        """The nearest loop whose body (not its `else:` clause) contains node."""
        child, p = node, self.parents.get(node)
        while p is not None and p is not stop and not isinstance(p, FUNCS):
            if isinstance(p, (ast.For, ast.AsyncFor, ast.While)) and child in p.body:
                return p
            if isinstance(p, (ast.comprehension, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                return p
            child, p = p, self.parents.get(p)
        return None

    def enclosing(self, node, kinds, stop=None):
        """The nearest ancestor of one of `kinds`, stopping at `stop` or a function."""
        p = self.parents.get(node)
        while p is not None and p is not stop:
            if isinstance(p, kinds):
                return p
            if isinstance(p, FUNCS):
                return None
            p = self.parents.get(p)
        return None

    def in_finally(self, node, stop=None):
        p = self.parents.get(node)
        child = node
        while p is not None and p is not stop:
            if isinstance(p, ast.Try) and child in p.finalbody:
                return True
            child, p = p, self.parents.get(p)
        return False


def parameters(fn):
    a = fn.args
    return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]


def int_constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def str_constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
