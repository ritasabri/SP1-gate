"""File-access questions: what the agent's code does to files, and how safely.

Each key follows from documented semantics of the call itself: what an open mode does to an
existing file, what `with` guarantees, which users a permission mode admits, and where a
check-then-use sequence leaves a gap. None of it needs the code to run.
"""
import ast

from pyfacts import FILES, add_spec, dotted, int_constant, same, short, src

PATH_CALLS = ("open", "os.remove", "os.unlink", "shutil.rmtree", "os.rename", "os.replace", "shutil.move")
CHECKS = ("os.path.exists", "os.path.isfile", "os.path.isdir", "os.access", "os.path.lexists")
FD_MAKERS = ("os.open", "os.dup", "os.dup2", "os.pipe", "os.openpty")


def _shadowed_open(m):
    for n in ast.walk(m.tree):
        if isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == "open" for a in n.names):
            return True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "open":
            return True
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "open" for t in n.targets):
            return True
    return False


def _mode(call):
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for k in call.keywords:
        if k.arg == "mode" and isinstance(k.value, ast.Constant):
            mode = k.value.value
    return mode


def _maybe_descriptor(m, arg):
    """`open(fd, "w")` on a file descriptor doesn't truncate, so those calls are skipped."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
        return True
    if isinstance(arg, ast.Call) and (dotted(arg.func) in FD_MAKERS or dotted(arg.func).endswith(".fileno")):
        return True
    if isinstance(arg, ast.Name):
        for n in ast.walk(m.tree):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == arg.id for t in n.targets):
                v = n.value
                if isinstance(v, ast.Call) and (dotted(v.func) in FD_MAKERS or dotted(v.func).endswith(".fileno")):
                    return True
                if isinstance(v, ast.Constant) and isinstance(v.value, int):
                    return True
            if isinstance(n, ast.arg) and n.arg == arg.id and n.arg in ("fd", "fileno", "descriptor"):
                return True
    return False


def fam_open_mode(m, changed, rel, specs):
    if _shadowed_open(m):
        return
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and dotted(node.func) == "open" and node.lineno in changed):
            continue
        mode = _mode(node)
        if not isinstance(mode, str) or "+" in mode or not node.args or _maybe_descriptor(m, node.args[0]):
            continue
        letter = next((c for c in mode if c in "rwax"), "r")
        if letter == "r":
            continue
        target = short(src(m.lines, node.args[0]), 30)
        texts = {
            "w": "It's erased (truncated to zero length) as soon as the file opens",
            "a": "It's kept, and everything written is added at the end",
            "x": "Nothing is touched: the open fails with FileExistsError",
            "ask": "Python asks first, then replaces it if you agree",
        }
        feedback = {
            "w": "That's mode `\"w\"`, which truncates the file as soon as it opens.",
            "a": "That's mode `\"a\"` (append), which keeps the old contents.",
            "x": "That's mode `\"x\"` (exclusive creation), which refuses to open a file that already exists.",
            "ask": "Python never asks. Mode `\"{}\"` decides on its own.".format(mode),
        }
        others = [o for o in ("w", "a", "x", "ask") if o != letter][:3]
        why = {"w": "Mode `\"w\"` truncates: the old contents are gone as soon as `open` returns, even if nothing is ever "
                    "written and even if the program crashes right after. Writing to a temporary file and then calling "
                    "`os.replace` avoids losing data halfway through.",
               "a": "Mode `\"a\"` appends: the old contents stay, and every write goes at the end.",
               "x": "Mode `\"x\"` creates a new file and refuses to overwrite one that exists, in a single step."}[letter]
        add_spec(specs, "open-mode", 6 if letter == "w" else 4, FILES, node.lineno,
                 "Line {} opens `{}` with mode `\"{}\"`. If that file already exists, what happens to what's in it?".format(
                     node.lineno, target, mode),
                 texts[letter], why, [(texts[o], feedback[o]) for o in others], rel)


def fam_with(m, changed, rel, specs):
    if _shadowed_open(m):
        return
    for node in ast.walk(m.tree):
        if not (isinstance(node, (ast.With, ast.AsyncWith)) and node.lineno in changed):
            continue
        if not any(isinstance(i.context_expr, ast.Call) and dotted(i.context_expr.func) == "open" for i in node.items):
            continue
        add_spec(specs, "with", 3, FILES, node.lineno,
                 "Line {} opens a file with `with open(...)`. What does `with` guarantee that `f = open(...)` followed "
                 "by a later `f.close()` does not?".format(node.lineno),
                 "The file is closed when the block ends, even if an exception is raised",
                 "A file object is a context manager: leaving the `with` block calls its `close()` on every path, normal or "
                 "exceptional. With a plain `close()` at the end, an exception skips it and the file stays open until "
                 "garbage collection (CWE-775, missing release of a file descriptor or handle).",
                 [("The file is locked, so no other program can open it meanwhile",
                   "`with` doesn't lock anything; other programs can still open the file."),
                  ("Every `write` inside the block goes straight to the disk",
                   "Writes are still buffered; they're flushed when the file is closed."),
                  ("The file opens faster, because Python can reuse the handle",
                   "Opening costs the same. The difference is in how reliably it's closed.")],
                 rel)
        return


def _closes_in_handler(m, stmt, name):
    """Is `stmt` inside any try or with (an except, a finally or a context manager might close it)?"""
    p = m.parents.get(stmt)
    while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(p, (ast.Try, ast.With, ast.AsyncWith)):
            return True
        p = m.parents.get(p)
    return False


def fam_unclosed(m, changed, rel, specs):
    """`f = open(...)` ... `f.close()` with a plain statement that can raise in between, and no
    try/except/finally anywhere around or between them."""
    if _shadowed_open(m):
        return
    for block_owner in ast.walk(m.tree):
        for field in ("body", "orelse"):
            block = getattr(block_owner, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Call) and dotted(stmt.value.func) == "open"
                        and stmt.lineno in changed):
                    continue
                name = stmt.targets[0].id
                close_at = None
                for j in range(i + 1, len(block)):
                    s = block[j]
                    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and dotted(s.value.func) == name + ".close":
                        close_at = j
                        break
                if close_at is None or m.in_finally(stmt) or _closes_in_handler(m, stmt, name):
                    continue
                between = block[i + 1:close_at]
                if any(isinstance(n, ast.Try) for s in between for n in ast.walk(s)):
                    continue                 # an except/finally might close it: too subtle to ask
                risky = next((s for s in between
                              if isinstance(s, (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign))
                              and any(isinstance(n, ast.Call) for n in ast.walk(s))), None)
                if risky is None:
                    continue
                close_line = block[close_at].lineno
                add_spec(specs, "unclosed", 6, FILES, stmt.lineno,
                         "If line {} raises an exception, what happens to the file opened on line {}?".format(risky.lineno, stmt.lineno),
                         "Line {} is skipped, so it stays open until garbage collection".format(close_line),
                         "An exception jumps straight out of the block, past `{}.close()` on line {}. Use `with open(...) as {}:` "
                         "(or `try`/`finally`) so the file is closed on every path. Leaked handles are CWE-775, and enough of "
                         "them hit the process's open-file limit.".format(name, close_line, name),
                         [("Python closes every open file when an exception is raised",
                           "Exceptions don't close files. Only `close()`, a `with` block, or garbage collection does."),
                          ("The exception waits until the file has been closed",
                           "The exception propagates immediately; nothing waits for the file."),
                          ("The file is deleted so no half-written copy is left",
                           "Nothing deletes the file. Data already written may sit in the buffer or the file.")],
                         rel)
                return


def _octal(mode):
    return format(mode, "#05o")


WRITERS = {
    "owner": "Only the owner",
    "group": "The owner and members of the file's group",
    "all": "Every user account on this computer",
    "none": "No one, not even the owner",
}


def _writers(mode):
    """Who can write, for modes where the classes nest (others ⊆ group ⊆ owner), else None.
    POSIX classes are exclusive: with 0o602 a group member can't write though "others" can."""
    o, g, e = bool(mode & 0o200), bool(mode & 0o020), bool(mode & 0o002)
    if (e and not g) or (g and not o):
        return None
    if e:
        return "all"
    if g:
        return "group"
    return "owner" if o else "none"


def fam_chmod(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed and dotted(node.func) in ("os.chmod", "os.fchmod")
                and len(node.args) >= 2):
            continue
        mode = int_constant(node.args[1])
        if mode is None or not (0 <= mode <= 0o777) or not (mode & 0o022):
            continue                         # ordinary modes only, and only when the group or others can write
        who = _writers(mode)
        if who not in ("group", "all"):
            continue
        key = WRITERS[who]
        distractors = [(WRITERS[w], "That would take a mode like `{}`. Each octal digit is one class (owner, group, "
                                    "others), and write permission is its 2.".format(
                                        {"owner": "0o755", "group": "0o775", "all": "0o777", "none": "0o555"}[w]))
                       for w in ("owner", "group", "all", "none") if w != who][:3]
        target = short(src(m.lines, node.args[0]), 24)
        simple = dotted(node.func) == "os.chmod" and isinstance(node.args[0], (ast.Name, ast.Attribute, ast.Constant))
        add_spec(specs, "chmod", 8, FILES, node.lineno,
                 "After line {} runs `{}({}, {})`, who can modify {}?".format(
                     node.lineno, dotted(node.func), target, _octal(mode),
                     "`{}`".format(target) if simple else "it"),
                 key,
                 "In `{}`, the three octal digits are owner, group and others, and each adds 4 (read), 2 (write) and 1 "
                 "(execute). Unlike the permission argument of `os.open` or `os.mkdir`, `chmod` isn't reduced by the umask. "
                 "Letting the group or everyone write breaks least privilege (CWE-732, incorrect permission assignment): "
                 "`0o644` or `0o600` is usual for a file, `0o755` or `0o700` for a folder. (On Windows, `chmod` only sets "
                 "the read-only flag.)".format(_octal(mode)),
                 distractors, rel)
        return


def _check_call(test):
    for n in ast.walk(test):
        if isinstance(n, ast.Call) and dotted(n.func) in CHECKS and n.args:
            return n
    return None


def fam_toctou(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not isinstance(node, ast.If):
            continue
        check = _check_call(node.test)
        if not check:
            continue
        target = check.args[0]
        if not isinstance(target, (ast.Name, ast.Attribute, ast.Constant, ast.Call, ast.JoinedStr, ast.BinOp)):
            continue
        use = None
        for n in ast.walk(ast.Module(body=node.body + node.orelse, type_ignores=[])):
            if isinstance(n, ast.Call) and dotted(n.func) in PATH_CALLS and n.args and same(n.args[0], target):
                use = n
                break
        if use is None or not ({node.lineno, use.lineno} & changed):
            continue
        what = dotted(check.func)
        add_spec(specs, "toctou", 7, FILES, use.lineno,
                 "Line {} checks `{}(…)`, and line {} then uses the same path. What can happen between the two lines?".format(
                     node.lineno, what, use.lineno),
                 "Another process can create, delete or swap the file, so the check may no longer be true",
                 "The check and the use are separate system calls, and the file system can change in between: a "
                 "time-of-check to time-of-use race (CWE-367). Attempt the operation directly and handle the exception "
                 "(`try: open(...) except FileNotFoundError:`), or use mode `\"x\"`, which checks and creates in one step.",
                 [("Nothing: Python runs the check and the use as one atomic step",
                   "They are two separate operations; other processes can run in between."),
                  ("The check locks the file until the next line has used it",
                   "`{}` takes no lock. It answers once, for that moment.".format(what)),
                  ("Only a small cost in speed, because the disk is asked twice",
                   "The danger is correctness and security: the answer can change before the next line runs.")],
                 rel)
        return


def fam_mktemp(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed and dotted(node.func) == "tempfile.mktemp"):
            continue
        add_spec(specs, "mktemp", 7, FILES, node.lineno,
                 "Line {} gets a file name from `tempfile.mktemp()`. What is the risk?".format(node.lineno),
                 "Another process can create a file with that name before this program opens it",
                 "`mktemp` only returns a name; the file is created later, and someone else can create it first, possibly as a "
                 "link to a file of their choosing. Python's documentation deprecates it for this reason. `mkstemp()` and "
                 "`NamedTemporaryFile()` create the file securely in one step (CWE-377, insecure temporary file).",
                 [("The file is deleted as soon as `mktemp` returns its name",
                   "`mktemp` creates nothing at all; it only picks a name, which is the problem."),
                  ("The name may be too long for some file systems to accept",
                   "Length isn't the issue; the gap between choosing the name and creating the file is."),
                  ("Nothing: the name is random, so no other process can take it",
                   "It only has to be taken first. Anyone who can predict the name, or learn it from a log or an error "
                   "message, can create the file before you do.")],
                 rel)
        return


FAMILIES = [fam_open_mode, fam_with, fam_unclosed, fam_chmod, fam_toctou, fam_mktemp]
