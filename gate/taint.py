"""Where can outside input end up? A small, conservative taint analysis for Python.

Sources: a function's parameters (the caller controls them), `input()`, `sys.argv`, and
web request data (`request.args`, `request.form`, ...). Sinks: shell commands
(`shell=True`, `os.system`, `os.popen`), code execution (`eval`, `exec`) and SQL built
from strings (`cursor.execute(f"...")`).

Conservative on purpose: a finding becomes the answer key of a question, so false alarms
are worse than misses. A value that goes through int()/float()/len()/shlex.quote() is
treated as clean, and if the tainted variable is ever tested in an `if`, `while` or
`assert` (it might be checked against an allowlist), its findings are dropped.
"""
import ast

MODEL_CALLS = ("messages.create", "chat.completions.create", "responses.create", "generate_content")
SHELL_ALWAYS = ("os.system", "os.popen")
SHELL_IF_FLAG = ("subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_output",
                 "subprocess.check_call")
CODE_SINKS = ("eval", "exec")
SQL_METHODS = ("execute", "executemany", "executescript")
SANITIZERS = ("int", "float", "bool", "len", "abs", "round", "hash", "id", "ord",
              "shlex.quote", "pipes.quote")
REQUEST_ATTRS = ("args", "form", "values", "json", "files", "cookies", "headers", "data",
                 "GET", "POST", "get_json", "query_params")


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = dotted(node.value)
        return inner + "." + node.attr if inner else ""
    return ""


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


def _source_call(node):
    """input(), sys.argv[...], request.args[...] / request.form.get(...) and friends."""
    d = dotted(node.func) if isinstance(node, ast.Call) else dotted(node)
    if d == "input":
        return "`input()` reads whatever the user types"
    if d.startswith("sys.argv"):
        return "`sys.argv` holds the command-line arguments"
    parts = d.split(".")
    if len(parts) >= 2 and parts[0] == "request" and parts[1] in REQUEST_ATTRS:
        return "`request.{}` comes from the web request".format(parts[1])
    return None


def _tested_names(fn):
    """Names that appear in any if/while/assert test or comprehension filter."""
    names = set()
    for n in ast.walk(fn):
        tests = []
        if isinstance(n, (ast.If, ast.While, ast.IfExp)):
            tests = [n.test]
        elif isinstance(n, ast.Assert):
            tests = [n.test]
        elif isinstance(n, ast.comprehension):
            tests = n.ifs
        for t in tests:
            names |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
    return names


def _scan(scope_node, body, params, tables, lines, fn_name):
    tainted = {}
    root_of = {}
    for p in params:
        tainted[p] = [(scope_node.lineno, "`{}` is a parameter the caller controls".format(p))]
        root_of[p] = p
    findings = []

    def origin(expr):
        """(path, root name) if expr carries tainted data, else None."""
        if expr is None:
            return None
        if isinstance(expr, (ast.Compare, ast.BoolOp)) or isinstance(expr, ast.Constant):
            return None
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) and expr.value.id in tables:
            return None
        if isinstance(expr, ast.Name):
            if expr.id in tainted:
                return tainted[expr.id], root_of.get(expr.id)
            return None
        src = _source_call(expr) if isinstance(expr, (ast.Call, ast.Attribute, ast.Subscript)) else None
        if isinstance(expr, ast.Subscript) and src is None:
            src = _source_call(expr.value) if isinstance(expr.value, (ast.Attribute, ast.Name)) else None
        if src:
            return [(expr.lineno, src)], "__source__"
        if isinstance(expr, ast.Call):
            d = dotted(expr.func)
            if d in SANITIZERS:
                return None
            if d.endswith(MODEL_CALLS):
                for arg in list(expr.args) + [k.value for k in expr.keywords]:
                    o = origin(arg)
                    if o:
                        return o
                return None
        for child in ast.iter_child_nodes(expr):
            o = origin(child)
            if o:
                return o
        return None

    def check_sinks(node):
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            d = dotted(call.func)
            kind = None
            if d in SHELL_ALWAYS:
                kind = "shell"
            elif d in SHELL_IF_FLAG and any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                                            and k.value.value is True for k in call.keywords):
                kind = "shell"
            elif d in CODE_SINKS:
                kind = "code"
            elif isinstance(call.func, ast.Attribute) and call.func.attr in SQL_METHODS and call.args:
                first = call.args[0]
                if isinstance(first, (ast.JoinedStr, ast.BinOp, ast.Name)) or (
                        isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute)
                        and first.func.attr == "format"):
                    kind = "sql"
            if not kind or not call.args:
                continue
            o = origin(call.args[0])
            if not o:
                continue
            path, root = o
            findings.append({
                "kind": kind, "line": call.lineno, "sink": ast.unparse(call) if hasattr(ast, "unparse") else d,
                "call": d, "root": root, "function": fn_name, "param": root if root in params else None,
                "via_model": any(m + "(" in text for _, text in path for m in MODEL_CALLS),
                "path": path + [(call.lineno, lines[call.lineno - 1].strip())],
            })

    def visit(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                value = s.value
                o = origin(value) if value is not None else None
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                if o:
                    path, root = o
                    for t in targets:
                        for name in (n for n in ast.walk(t) if isinstance(n, ast.Name)):
                            tainted[name.id] = path + [(s.lineno, lines[s.lineno - 1].strip())]
                            root_of[name.id] = name.id if root == "__source__" else root
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                o = origin(s.iter)
                if o:
                    path, root = o
                    for name in (n for n in ast.walk(s.target) if isinstance(n, ast.Name)):
                        tainted[name.id] = path + [(s.lineno, lines[s.lineno - 1].strip())]
                        root_of[name.id] = name.id if root == "__source__" else root
                visit(s.body)
                visit(s.orelse)
            elif isinstance(s, (ast.If, ast.While)):
                visit(s.body)
                visit(s.orelse)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                visit(s.body)
            elif isinstance(s, ast.Try):
                visit(s.body)
                for h in s.handlers:
                    visit(h.body)
                visit(s.orelse)
                visit(s.finalbody)
            if isinstance(s, (ast.If, ast.While)):
                heads = [s.test]
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                heads = [s.iter]
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                heads = [item.context_expr for item in s.items]
            elif isinstance(s, ast.Try):
                heads = []
            else:
                heads = [s]
            for h in heads:
                check_sinks(h)

    visit(body)
    return findings


def taint_findings(source):
    """All findings in a module, one list for every function plus the module's own code."""
    tree = ast.parse(source)
    lines = source.splitlines()
    tables = _constant_tables(tree)
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                      if a.arg not in ("self", "cls")]
            tested = _tested_names(node)
            for f in _scan(node, node.body, params, tables, lines, node.name):
                if f["root"] in tested:
                    continue          # it's checked somewhere (maybe against an allowlist)
                findings.append(f)
    module_body = [s for s in tree.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    module = ast.Module(body=module_body, type_ignores=[])
    tested = _tested_names(module)
    fake = ast.parse("0").body[0]
    fake.lineno = 1
    for f in _scan(fake, module_body, [], tables, lines, None):
        if f["root"] in tested:
            continue
        findings.append(f)
    return findings
