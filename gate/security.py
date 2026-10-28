"""Security questions about the code the agent wrote, each tied to a weakness class.

Every key names the weakness and its entry in MITRE's Common Weakness Enumeration (CWE),
and says what an attacker could actually do. Two kinds of evidence back the keys:
- data flow, from taint.py: untrusted input that reaches a shell, SQL text, `eval` or a
  file path under a fixed folder (injection and path traversal);
- a construct whose risk the library itself documents: `pickle` can run code while loading,
  `random` is predictable, `verify=False` skips the certificate check, and so on.

Names alone are never enough. A credential needs a credential-like name and a literal value
that looks like a secret; a weak token needs a secret-like name and a random call that builds
a password-style string; a weak password hash needs a hash call whose input is named as a
password and whose result isn't just a lookup prefix.
"""
import ast
import re

from pyfacts import DESIGN, SECURITY, add_spec, dotted, short, src, str_constant
from taint import taint_findings

# ----------------------------------------------------------------- injection (data flow)

CLASSES = {
    "shell": "OS command injection (CWE-78)",
    "sql": "SQL injection (CWE-89)",
    "code": "Eval injection (CWE-95)",
    "path": "Path traversal (CWE-22)",
    "xss": "Cross-site scripting (CWE-79)",
}


PATH_EFFECTS = {
    "read": "read files outside the intended folder, like `../../.ssh/id_rsa`",
    "read_image": "read image files outside the intended folder",
    "overwrite": "overwrite files outside the intended folder, like `../../.bashrc`",
    "append": "add text to files outside the intended folder, like `../../.bashrc`",
    "modify": "change files outside the intended folder, like `../../.bashrc`",
    "create": "create new files outside the intended folder, in any folder your account can write to",
    "write": "write files outside the intended folder, in any folder your account can write to",
    "access": "open files outside the intended folder, like `../../.ssh/id_rsa`",
    "delete": "delete files outside the intended folder, like `../../.bashrc`",
    "delete_tree": "delete folders outside the intended folder, like `../../Documents`",
}


def _stem(f):
    first_line, first_text = f["path"][0]
    sink = f["line"]
    if f["function"] and f["param"]:
        return ("Whoever calls `{}` controls `{}`. What weakness does that create on line {}, and what can they "
                "do with it?").format(f["function"], f["param"], sink)
    if "web request" in first_text:
        return ("The web request value read on line {} reaches line {}. What weakness is that, and what could "
                "whoever sends the request do?").format(first_line, sink)
    if "command-line" in first_text:
        return ("The command-line argument read on line {} reaches line {}. What weakness is that, and what could "
                "someone who controls that argument do?").format(first_line, sink)
    return ("The text typed at the prompt on line {} reaches line {}. What weakness is that, and what could "
            "someone who controls that text do?").format(first_line, sink)


def _flow(f):
    lines = []
    for line, _ in f["path"]:
        if not lines or lines[-1] != line:
            lines.append(line)
    return " → ".join("line {}".format(n) for n in lines)


def _first_finding(m, changed):
    try:
        findings = taint_findings(m.source)
    except (SyntaxError, RecursionError):
        return None
    for f in findings:
        if any(line in changed for line, _ in f["path"]):
            return f
    return None


def fam_injection(m, changed, rel, specs):
    f = _first_finding(m, changed)
    if not f:
        return
    sink, call, flow = f["line"], f["call"], _flow(f)
    c = CLASSES
    if f["kind"] == "shell" and f["via_model"]:
        key = "Prompt injection turned command execution: via the model's reply, they shape what line {} runs".format(sink)
        why = ("Data flow: {}. Their text goes into the prompt, the model's reply becomes the command, and line {} runs it "
               "through a shell. Text in a prompt can steer the reply (OWASP LLM01, prompt injection), and running a reply "
               "unchecked is improper output handling (OWASP LLM05), which ends in OS command injection (CWE-78).").format(flow, sink)
        distractors = [("None: the model's reply doesn't depend on the text they typed into the prompt",
                        "A model's reply depends on everything in its prompt, including their text."),
                       ("None: `max_tokens` keeps their text from ever reaching line {}".format(sink),
                        "`max_tokens` caps how long the reply is, not what it says."),
                       (c["xss"] + ": they run JavaScript in a visitor's web browser",
                        "The reply goes to a shell, not to a web page.")]
    elif f["kind"] == "shell":
        key = c["shell"] + ": they can append commands of their own, like `; rm -rf ~`"
        why = ("Data flow: {}. The value becomes part of a command string that line {} hands to the shell, and the shell "
               "treats `;`, `|`, `&&` and `$( )` as the start of new commands. That is CWE-78, OS command injection.").format(flow, sink)
        distractors = [(c["sql"] + ": they can rewrite the database query this line sends",
                        "SQL injection needs SQL text. Line {} builds a shell command, so the shell is what reads their characters.".format(sink)),
                       (c["xss"] + ": they can run JavaScript in a visitor's browser",
                        "XSS needs their text inside a web page. Here it goes to the operating system's shell."),
                       ("None: `{}` passes the whole string along as one harmless argument".format(call),
                        "`{}` hands the string to a shell, which splits it at `;`, `|` and `&&` and runs each part.".format(call))]
    elif f["kind"] == "code":
        if len(f["node"].args) >= 2:
            key = c["code"] + ": they can run Python code; a restricted namespace is known to be escapable"
        else:
            key = c["code"] + ": they can run any Python code, with this program's permissions"
        why = ("Data flow: {}. `{}` evaluates its argument as Python, and Python can import modules, read files and start "
               "processes, e.g. `__import__('os').system('…')`. That is CWE-95, eval injection.").format(flow, call)
        distractors = [("None: `{}` only evaluates arithmetic, so nothing else can run".format(call),
                        "`{}` evaluates any Python expression, not just arithmetic.".format(call)),
                       (c["sql"] + ": they can rewrite a database query this program sends",
                        "No database is involved: `{}` runs Python.".format(call)),
                       (c["xss"] + ": they can run JavaScript in a visitor's browser",
                        "The text runs as Python on this computer, not as JavaScript in a browser.")]
    elif f["kind"] == "sql":
        key = c["sql"] + ": they can rewrite the query, e.g. to return rows they shouldn't see"
        why = ("Data flow: {}. The value is pasted into the SQL text on line {}, so quotes and keywords in it become part of "
               "the query (CWE-89): `' OR '1'='1` turns a lookup into one that matches every row.").format(flow, sink)
        distractors = [("None: `execute` escapes any values that are placed inside f-strings",
                        "By the time `execute` sees it, the f-string is one finished string. Nothing is escaped."),
                       (c["shell"] + ": they can run shell commands on this computer",
                        "The text goes into SQL, not into a shell. The database's parser is what they control."),
                       (c["xss"] + ": they can run JavaScript in a visitor's browser",
                        "No web page is involved here; the text changes the SQL query.")]
    elif f["kind"] == "path":
        verb = PATH_EFFECTS.get(f["effect"])
        if verb is None:
            return
        key = c["path"] + ": they can " + verb
        why = ("Data flow: {}. The value is joined to a folder name and used as a path on line {}. Each `../` climbs one "
               "folder up, so the program reaches whatever file they name, with your permissions (CWE-22, path "
               "traversal). With `os.path.join` or a pathlib `/`, an absolute path such as `/etc/passwd` even discards "
               "the folder.").format(flow, sink)
        if f["effect"] == "create":
            why += (" The open mode refuses a file that already exists, so it can't replace one, but it can create "
                    "new files anywhere.")
        elif f["effect"] == "write":
            why += (" (The code checks whether something is already there, and may only create new files; either way "
                    "the path can leave the folder.)")
        distractors = [("None: `{}` stays inside the folder that the code puts in front".format(call),
                        "Nothing stops `../` from climbing out of that folder."),
                       (c["shell"] + ": they can run shell commands on this computer",
                        "`{}` never starts a shell. The danger is which file it reaches.".format(call)),
                       (c["sql"] + ": they can rewrite a database query this program sends",
                        "No database is involved; the input becomes a file path.")]
    else:
        return
    add_spec(specs, "injection", 10, SECURITY, sink, _stem(f), key, why, distractors, rel)


def fam_mitigation(m, changed, rel, specs):
    f = _first_finding(m, changed)
    if not f or f["via_model"] or f["kind"] not in ("shell", "sql", "path"):
        return
    sink = f["line"]
    if f["kind"] == "shell":
        stem = "Which change to line {} removes the command injection and still runs the program?".format(sink)
        key = "Pass the program and its arguments to `subprocess.run` as a list, with no shell"
        why = ("Without a shell, each list item reaches the program as one argument, so `;` and `|` are just characters. "
               "(`shlex.quote` is the fallback when a shell is truly needed.) An argument that starts with `-` can still "
               "be read as an option, so put `--` before user input when the program supports it (CWE-88).")
        distractors = [("Remove every `;` from the input before building the command string",
                        "A blocklist misses `|`, `&&`, `$( )`, backticks and newlines. The shell still parses the string."),
                       ("Check that the input isn't empty before the command is run",
                        "An emptiness check doesn't change which characters reach the shell."),
                       ("Print the full command to the terminal before running it",
                        "Printing records the command; the next line still runs it.")]
    elif f["kind"] == "sql":
        if f["sql_position"] == "value":
            stem = "Which change to line {} removes the SQL injection?".format(sink)
            key = "Use a parameterized query: a placeholder in the SQL, the value passed separately"
            why = ("With a parameter, the driver keeps the value as data: the database receives it separately (sqlite3, "
                   "psycopg 3) or the driver escapes it correctly (psycopg2, PyMySQL), so it can't become SQL. Placeholder "
                   "syntax depends on the driver: `?` for `sqlite3`, `%s` for psycopg and the MySQL drivers.")
            distractors = [("Cut the input down to its first 20 characters",
                            "Short payloads fit: `' OR '1'='1` is 11 characters."),
                           ("Check that the input isn't empty before running it",
                            "An emptiness check says nothing about quotes or SQL keywords in the input."),
                           ("Wrap the `execute` call in a `try`/`except` block",
                            "Injected SQL that is valid runs without raising anything.")]
        elif f["sql_position"] == "identifier":
            stem = "Line {} puts the input where a table or column name goes. Which change removes the SQL injection?".format(sink)
            key = "Accept the name only if it's in a fixed allowlist of table or column names"
            why = ("Placeholders stand for values, not for names: `SELECT * FROM ?` is a syntax error. So the name has to be "
                   "checked against the names the program actually uses before it's put into the SQL text.")
            distractors = [("Use a `?` placeholder for the name and pass it separately",
                            "Placeholders can't stand for table or column names: the query fails, or the value is silently ignored."),
                           ("Cut the input down to its first 20 characters",
                            "A short name can still carry SQL, such as `users--`."),
                           ("Wrap the `execute` call in a `try`/`except` block",
                            "Injected SQL that is valid runs without raising anything.")]
        else:
            return
    else:
        stem = "Which change keeps the path on line {} inside the folder it's meant for?".format(sink)
        key = "Resolve the full path and reject it unless it's still inside the allowed folder"
        why = ("`os.path.realpath` (or `Path.resolve`) turns `..` and links into a real location, and comparing it with "
               "the allowed folder (`os.path.commonpath`) catches escapes. Keeping only the file name with "
               "`os.path.basename` also works when no subfolders are needed, as long as empty names, `.` and `..` are "
               "rejected too.")
        distractors = [("Join the input to the folder name with `os.path.join` first",
                        "`os.path.join(\"uploads\", \"../../x\")` still climbs out, and an absolute path replaces the folder entirely."),
                       ("Check that the file exists before the program uses it",
                        "Existence says nothing about where the file is."),
                       ("Remove spaces at both ends of the input with `.strip()`",
                        "`.strip()` leaves `../` and `/` untouched.")]
    add_spec(specs, "mitigation", 8, DESIGN, sink, stem, key, why, distractors, rel)


# ----------------------------------------------------------------- credentials in source

SECRET_NAME = re.compile(r"(?i)(^|_)(password|passwd|pwd|secret|api_?key|apikey|token|access_?key|private_?key|"
                         r"client_?secret|auth_?key|secret_?key)($|_)")
NOT_SECRET = re.compile(r"(?i)(url|uri|path|file|dir|name|endpoint|header|type|prefix|suffix|format|label|field|env|"
                        r"var|placeholder|regex|pattern|message|prompt|hint|length|len|count|limit|example|kind|mode|"
                        r"policy|algo|scheme|style|location|backend|strength|audience|cookie|encod|issuer|lifetime|"
                        r"expir|ttl|timeout|version|provider|param|scope|method|grant|size|min|max|rule|class|key_id)")
PLACEHOLDER = re.compile(r"(?i)(your|here|<|>|xxx|\.\.\.|changeme|todo|example|dummy|fake|placeholder)")


def _looks_secret(text):
    """A literal that could be a real secret: not a plain word or identifier."""
    if text is None or len(text) < 6 or text.strip() != text or " " in text:
        return False
    if text.startswith(("http://", "https://", "/")) or PLACEHOLDER.search(text) or len(set(text)) <= 2:
        return False
    if re.fullmatch(r"[a-z]+(_[a-z]+)*", text) or re.fullmatch(r"[A-Z][a-z]+", text) or re.fullmatch(r"[A-Z_]+", text):
        return False                     # "refresh_token", "Bearer", "HS_ALGO": words, not secrets
    classes = sum(bool(re.search(p, text)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 2


def _credential(name, value):
    return bool(SECRET_NAME.search(name)) and not NOT_SECRET.search(name) and _looks_secret(str_constant(value))


def fam_hardcoded_secret(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        found = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and node.lineno in changed:
            t = node.targets[0]
            name = t.id if isinstance(t, ast.Name) else (t.attr if isinstance(t, ast.Attribute) else None)
            if name and _credential(name, node.value):
                found = (node.lineno, "`{}`".format(name))
        elif isinstance(node, ast.Call) and node.lineno in changed:
            for k in node.keywords:
                if k.arg and _credential(k.arg, k.value):
                    found = (node.lineno, "the `{}` argument".format(k.arg))
                    break
        if not found:
            continue
        line, what = found
        add_spec(specs, "hardcoded-secret", 9, SECURITY, line,
                 "Line {} writes a literal value into {}. Who can learn that credential?".format(line, what),
                 "Anyone who can read the source or its git history, in every copy",
                 "A credential written into code travels with every copy, push and backup of that code, and git keeps it in "
                 "history even after the line is deleted. That is CWE-798, use of hard-coded credentials. Read it from the "
                 "environment instead (`os.environ[\"API_KEY\"]`), and rotate any key that was ever committed.",
                 [("Only this program: Python hides string constants at run time",
                   "String constants sit in the `.py` file as plain text, and stay readable in compiled `.pyc` files."),
                  ("Nobody, unless they actually run the program",
                   "Reading the file is enough; nothing has to run."),
                  ("Only accounts with administrator rights here",
                   "File permissions don't follow the code to GitHub, backups or someone else's laptop.")],
                 rel)
        return


# ----------------------------------------------------------------- unsafe loading

UNPICKLERS = {"pickle.load", "pickle.loads", "cPickle.load", "cPickle.loads", "joblib.load",
              "pd.read_pickle", "pandas.read_pickle", "dill.load", "dill.loads"}


def fam_pickle(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed and dotted(node.func) in UNPICKLERS):
            continue
        fn = dotted(node.func)
        add_spec(specs, "pickle", 9, SECURITY, node.lineno,
                 "Line {} loads data with `{}`. What can a crafted file do when this line runs?".format(node.lineno, fn),
                 "Run any code it likes, with this program's permissions",
                 "Python's documentation warns that `pickle` is not secure: a pickle stream can name any importable callable, "
                 "such as `os.system`, and the unpickler calls it while loading (that's what `__reduce__` sets up). That is "
                 "CWE-502, deserialization of untrusted data. For data that comes from outside, use a format that only "
                 "describes data, such as JSON.",
                 [("Only produce wrong values; loading can't run code",
                   "Unpickling can call any importable function, such as `os.system`."),
                  ("Nothing: `{}` checks a signature first".format(fn),
                   "Pickle files carry no signature; loading trusts whatever bytes it gets."),
                  ("Crash the program, but nothing worse",
                   "A crafted pickle can do anything the program can do, not just crash it.")],
                 rel)
        return


# ----------------------------------------------------------------- choices with a known weakness

FAST_HASHES = {"md5": "MD5", "sha1": "SHA-1", "sha224": "SHA-224", "sha256": "SHA-256", "sha384": "SHA-384",
               "sha512": "SHA-512", "sha3_256": "SHA3-256", "sha3_512": "SHA3-512", "blake2b": "BLAKE2b"}
PASSWORD_NAME = re.compile(r"(?i)(^|_)(pass(word|wd)?|pwd|pw|passcode)$")


def _names_in(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr


def _sliced_result(m, call):
    """`sha1(...).hexdigest().upper()[:5]`: a lookup prefix (e.g. Have I Been Pwned), not storage."""
    node = call
    while True:
        p = m.parents.get(node)
        if isinstance(p, ast.Attribute) and isinstance(m.parents.get(p), ast.Call):
            node = m.parents[p]
            continue
        return isinstance(p, ast.Subscript) and isinstance(p.slice, ast.Slice)


def fam_password_hash(m, changed, rel, specs):
    if "pwnedpasswords" in m.source:
        return
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed):
            continue
        d = dotted(node.func)
        algo = None
        if d.startswith("hashlib.") and d.split(".", 1)[1] in FAST_HASHES:
            algo = d.split(".", 1)[1]
        elif d == "hashlib.new" and node.args and str_constant(node.args[0]) in FAST_HASHES:
            algo = str_constant(node.args[0])
        if not algo or _sliced_result(m, node):
            continue
        if not any(PASSWORD_NAME.search(n) for a in node.args + [k.value for k in node.keywords] for n in _names_in(a)):
            continue
        shown = FAST_HASHES[algo]
        add_spec(specs, "password-hash", 8, DESIGN, node.lineno,
                 "Line {} hashes a password with `hashlib.{}`. Why is {} a weak choice for storing passwords?".format(
                     node.lineno, algo, shown),
                 "It's built to be fast, so stolen hashes can be guessed billions of times per second",
                 "General-purpose hashes are designed for speed: one high-end GPU computes billions of {} hashes per second. "
                 "Password storage needs a deliberately slow, salted function, so each guess is expensive: Argon2id or scrypt "
                 "(OWASP's first choices), or bcrypt or PBKDF2 (`hashlib.pbkdf2_hmac`) where those are required (CWE-916; "
                 "OWASP Password Storage Cheat Sheet).".format(shown),
                 [("{} can be run backwards to recover each password".format(shown),
                   "There's no inverse function. Attackers recover passwords by guessing quickly, or by looking hashes up in "
                   "precomputed tables when there's no salt."),
                  ("Its output is too long to fit in a database column",
                   "Length isn't the problem; a hex digest fits in any text column."),
                  ("Nothing: any cryptographic hash is fine for passwords",
                   "Hashes built for speed make guessing cheap. Password hashing needs a slow function.")],
                 rel)
        return


SECRET_TARGET = re.compile(r"(?i)(^|_)(password|passwd|otp|nonce|csrf_?token|reset_?token|session_?(id|token|key)|"
                           r"auth_?token|access_?token|api_?key|secret_?key|client_?secret|verification_?code|"
                           r"one_?time_?(code|password))$")
SECRET_MAKER = re.compile(r"(?i)^(make|generate|gen|create|new|issue|random)_?([a-z0-9]+_)?(password|token|otp|nonce|"
                          r"api_?key|session_?id|reset_?code|verification_?code)s?$")
NOT_REAL = {"fake", "dummy", "test", "tests", "mock", "sample", "example", "demo", "placeholder", "seed",
            "simulated", "simulation", "stub", "fixture", "fixtures", "synthetic"}


def _words(name):
    """`make_fake_password` -> {"make", "fake", "password"}; `FakeUserFactory` -> {"fake", "user", "factory"}."""
    return {w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)}


def _not_real(name):
    return bool(_words(name) & NOT_REAL)
CHAR_POOLS = ("string.ascii_letters", "string.ascii_lowercase", "string.ascii_uppercase", "string.digits",
              "string.hexdigits", "string.printable", "string.punctuation", "string.octdigits")


def _random_imports(tree):
    """Local names for the random module's functions: {"choice": "random.choice", "rnd": "random"}.
    None when the name `random` itself is rebound (`random = SystemRandom()`): then `random.choice`
    may well be the operating system's secure generator."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if n.module == "random":
                    out[a.asname or a.name] = "random." + a.name
                elif n.module == "numpy" and a.name == "random":
                    out[a.asname or a.name] = "random"
                elif (a.asname or a.name) == "random":
                    return None
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "random" and a.asname:
                    out[a.asname] = "random"
                elif a.asname == "random":
                    return None
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == "random" for t in targets):
                return None
    return out


def _full_name(func, imports):
    d = dotted(func)
    if d in imports:
        return imports[d]
    head, _, rest = d.partition(".")
    if rest and imports.get(head) == "random":
        return "random." + rest
    for prefix in ("np.random.", "numpy.random."):
        if d.startswith(prefix):
            return "random." + d[len(prefix):]   # numpy's generator isn't cryptographic either
    return d


def _assigned_values(m, scope, name):
    """The values `name` was given in scope, or else at module level: how `alphabet` was built.
    A parameter's default value counts too."""
    for s in (scope, m.tree):
        vals = []
        for n in m.own_nodes(s):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                vals.append(n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name \
                    and n.value is not None:
                vals.append(n.value)
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = s.args
            positional = a.posonlyargs + a.args
            for arg, default in zip(positional[len(positional) - len(a.defaults):], a.defaults):
                if arg.arg == name:
                    vals.append(default)
            for arg, default in zip(a.kwonlyargs, a.kw_defaults):
                if arg.arg == name and default is not None:
                    vals.append(default)
        if vals:
            return vals
    return []


def _password_style_random(node, imports=None, lookup=None):
    """A random call that builds a password-like value: characters drawn from a pool, a number
    with at least 4 digits, or at least 32 random bits. Returns the call's name, or None.
    `lookup(name)` gives the values a variable was assigned, so a pool kept in `alphabet` counts."""
    imports = imports or {}
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        full = _full_name(n.func, imports)
        if not full.startswith("random."):
            continue
        fn = full.split(".", 1)[1]
        if fn in ("choice", "choices", "sample"):
            pool = n.args[0] if n.args else next((k.value for k in n.keywords if k.arg in ("population", "seq")), None)
            if pool is None:
                continue
            pools = [pool]
            if isinstance(pool, ast.Name) and lookup:
                pools += lookup(pool.id)
            for p in pools:
                pool_names = {dotted(x) for x in ast.walk(p) if isinstance(x, (ast.Name, ast.Attribute))}
                if pool_names & set(CHAR_POOLS) or len(str_constant(p) or "") >= 10:
                    return full
        elif fn in ("randint", "randrange") and len(n.args) == 2 and all(isinstance(a, ast.Constant) for a in n.args):
            lo, hi = n.args[0].value, n.args[1].value
            if isinstance(lo, int) and isinstance(hi, int) and hi - lo >= 1000:
                return full
        elif fn in ("getrandbits", "randbytes") and n.args and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, int) and n.args[0].value >= (32 if fn == "getrandbits" else 4):
            return full
    return None


def fam_weak_random(m, changed, rel, specs):
    imports = _random_imports(m.tree)
    base = rel.replace("\\", "/").split("/")[-1]
    if imports is None or base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py":
        return                                  # `random` is something else here, or this is test data
    for n in ast.walk(m.tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [n.module or ""] if isinstance(n, ast.ImportFrom) else [a.name for a in n.names]
            if any(x.split(".")[0] in ("locust", "faker", "factory", "hypothesis") for x in mods):
                return                          # load tests and fake-data generators make throwaway values
    hits = []

    def not_real(node):
        """Inside a test, or a class or function named as fake data (`FakeUserFactory`, `make_fake_password`)."""
        p = m.parents.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                words = _words(p.name)
                if p.name.startswith("test_") or p.name == "test" or (words & NOT_REAL):
                    return True
            p = m.parents.get(p)
        return False

    def hashed(name, scope):
        """A nonce fed to a hash function, as in proof of work: it's published, not kept secret."""
        return any(isinstance(n, ast.Call) and dotted(n.func).startswith("hashlib.")
                   and any(isinstance(x, ast.Name) and x.id == name for a in n.args for x in ast.walk(a))
                   for n in m.own_nodes(scope))

    def counter(name, scope):
        """`nonce += 1`: a counter (as in proof of work), not a secret."""
        return any(isinstance(n, ast.AugAssign) and dotted(n.target) == name for n in m.own_nodes(scope))

    for node in ast.walk(m.tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and node.lineno in changed:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                name = t.id
            elif isinstance(t, ast.Attribute):
                name = t.attr
            elif isinstance(t, ast.Subscript) and str_constant(t.slice):
                name = str_constant(t.slice)    # `app.config["SECRET_KEY"] = ...`
            else:
                continue
            if not SECRET_TARGET.search(name) or _not_real(name) or not_real(node):
                continue
            scope = m.scope_of(node)
            if counter(dotted(t), scope) or (name.lower() == "nonce" and hashed(name, scope)):
                continue
            if any(isinstance(c, ast.Call) and any(k.arg == "size" for k in c.keywords) for c in ast.walk(node.value)):
                continue                        # `np.random.randint(..., size=n)`: a column of synthetic data
            fn = _password_style_random(node.value, imports, lambda n, scope=scope: _assigned_values(m, scope, n))
            if fn:
                hits.append((node.lineno, "`{}`".format(src(m.lines, t)), fn))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and SECRET_MAKER.search(node.name) \
                and not _not_real(node.name) and not not_real(node):
            def lookup(name, node=node):
                return _assigned_values(m, node, name)
            for r in (n for n in m.own_nodes(node) if isinstance(n, ast.Return) and n.value is not None):
                fn = _password_style_random(r.value, imports, lookup)
                line = r.lineno
                if not fn and isinstance(r.value, ast.Name):
                    for v in lookup(r.value.id):        # `token = "".join(random.choice(...))`, then `return token`
                        fn = _password_style_random(v, imports, lookup)
                        if fn:
                            line = v.lineno
                            break
                if fn and line in changed:
                    hits.append((line, "the value `{}` returns".format(node.name), fn))
    if not hits:
        return
    line, what, fn = sorted(hits)[0]
    add_spec(specs, "weak-random", 8, DESIGN, line,
             "Line {} makes {} with `{}`. Why is the `random` module the wrong tool for a secret?".format(line, what, fn),
             "Its generator is predictable: enough outputs reveal the next ones",
             "Python's documentation says `random` should not be used for security and points to `secrets`. Its Mersenne "
             "Twister generator is not cryptographic: from enough of its outputs (624 consecutive 32-bit values; outputs "
             "that reveal fewer bits just take more) its whole internal state can be rebuilt, and every future value "
             "follows. Use `secrets.token_urlsafe()` or `secrets.choice()` (CWE-338).",
             [("It produces the same values every time the program runs",
               "Without a seed, `random` starts from a different state each run. The problem is that its outputs reveal its state."),
              ("Its values aren't uniform, so some characters show up more",
               "Its output is uniform enough for simulations; uniformity isn't the issue."),
              ("Nothing: the OS seeds it, which makes it secure enough",
               "A good seed doesn't make the generator unpredictable: its outputs still reveal its internal state.")],
             rel)


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "Client", "AsyncClient"}
LOOPBACK = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)")


def fam_tls_verify(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed):
            continue
        d = dotted(node.func)
        off = any(k.arg == "verify" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords)
        if off:
            if d.split(".")[-1] not in HTTP_METHODS or "." not in d:
                continue                    # requests.get(...), httpx.Client(...), session.post(...)
            url = str_constant(node.args[0]) if node.args else None
            if url and (url.startswith("http://") or LOOPBACK.match(url)):
                continue                    # no TLS at all, or this computer's own test server
            how = "with `verify=False`"
        elif d == "ssl._create_unverified_context":
            how = "to make an SSL context"
        else:
            continue
        shown = short(src(m.lines, node.func), 30)
        add_spec(specs, "tls-verify", 9, SECURITY, node.lineno,
                 "Line {} calls `{}` {}. On a network you don't control, what does that allow?".format(
                     node.lineno, shown, how),
                 "Someone in the middle can pose as the server, because its certificate isn't checked",
                 "TLS proves who you are talking to only when the client checks the server's certificate. Turning the check off "
                 "leaves the encryption in place but lets anyone in the path present their own certificate and read or change "
                 "the traffic: a man-in-the-middle attack (CWE-295, improper certificate validation).",
                 [("Nothing: the connection is still encrypted, so it's private",
                   "Encryption to an unverified server can be encryption to the attacker."),
                  ("Only faster requests; the security stays exactly the same",
                   "Skipping the check removes the server's authentication."),
                  ("The server can no longer see your computer's IP address",
                   "Your address is visible either way; the setting is about the server's identity.")],
                 rel)
        return


def _bind_host(node):
    """The host a server call listens on, if it's a constant. Framework `run(host=...)` calls
    count only "0.0.0.0" or "::": Flask, for one, treats an empty host as 127.0.0.1. Socket-level
    calls (`HTTPServer(("", port), ...)`, `sock.bind(...)`, `make_server("", ...)`) also count ""."""
    d = dotted(node.func)
    for k in node.keywords:
        if k.arg == "host" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str) \
                and d.split(".")[-1] in ("run", "serve", "run_simple"):
            return k.value.value if k.value.value in ("0.0.0.0", "::") else None
    if d.split(".")[-1] in ("HTTPServer", "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer", "bind", "make_server") \
            and node.args:
        first = node.args[0]
        if isinstance(first, ast.Tuple) and len(first.elts) == 2 and isinstance(first.elts[0], ast.Constant) \
                and isinstance(first.elts[0].value, str):
            return first.elts[0].value
        if d.endswith("make_server") and isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def fam_bind_all(m, changed, rel, specs):
    for node in ast.walk(m.tree):
        if not (isinstance(node, ast.Call) and node.lineno in changed):
            continue
        host = _bind_host(node)
        if host not in ("0.0.0.0", "", "::"):
            continue
        shown = {"": "`\"\"` (empty)", "::": "`::`"}.get(host, "`0.0.0.0`")
        add_spec(specs, "bind-all", 7, SECURITY, node.lineno,
                 "Line {} starts listening on host {}. Who can connect to it?".format(node.lineno, shown),
                 "Any device that can reach this computer over the network",
                 "For a listening socket, `0.0.0.0` (or `::` for IPv6, or the empty host `\"\"` at the socket level) means "
                 "every network interface. Anyone on the same network can connect, and anyone on the internet if a router "
                 "forwards the port (CWE-1327, binding to an unrestricted IP address). Use `127.0.0.1` unless other devices "
                 "really need access.",
                 [("Only programs that run on this same computer",
                   "That's `127.0.0.1` (localhost). This address listens on every network interface."),
                  ("Nobody: it isn't a real address, so nothing starts",
                   "For a server, it's the standard way to say \"every interface\"."),
                  ("Only devices that first send it the right password",
                   "Listening adds no password; any login has to be built into the program.")],
                 rel)
        return


FAMILIES = [fam_injection, fam_mitigation, fam_hardcoded_secret, fam_pickle, fam_password_hash,
            fam_weak_random, fam_tls_verify, fam_bind_all]
