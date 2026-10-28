"""Tests for the academic question bank: running time, design choices, security (CWE) and
file access. Each test checks the key that the code proves, and that unsure cases ask nothing.

    python3 -m unittest discover -s tests      (from the sp1-gate-live folder)
"""
import os
import random
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gate"))

from code_questions import questions_for_turn, specs_for_file  # noqa: E402
from common import question  # noqa: E402


def specs(code, before=None, name="app.py"):
    code = textwrap.dedent(code).lstrip("\n")
    root = tempfile.gettempdir()
    return specs_for_file(os.path.join(root, name), name, before, code, root)


def fam(code, **kw):
    out = {}
    for s in specs(code, **kw):
        out.setdefault(s["family"], s)
    return out


class TestRunningTime(unittest.TestCase):
    def test_list_seen_loop_is_quadratic(self):
        f = fam('''
            def first_repeat(emails):
                seen = []
                for e in emails:
                    if e in seen:
                        return e
                    seen.append(e)
                return None
        ''')
        self.assertEqual(f["rt-membership"]["key"], "Θ(n²)")
        self.assertIn("n distinct items", f["rt-membership"]["stem"])
        self.assertIn("Same results", f["design-membership"]["key"])

    def test_set_seen_loop_is_linear_on_average(self):
        f = fam('''
            def dedupe(emails):
                seen = set()
                unique = []
                for email in emails:
                    if email not in seen:
                        seen.add(email)
                        unique.append(email)
                return unique
        ''')
        self.assertEqual(f["rt-membership"]["key"], "Θ(n)")
        self.assertIn("On average", f["rt-membership"]["stem"])
        self.assertIn("hash lookup", f["design-membership"]["key"])

    def test_unknown_work_in_the_loop_means_no_running_time_question(self):
        self.assertNotIn("rt-membership", fam('''
            def f(items):
                seen = []
                for x in items:
                    if x not in seen:
                        seen.append(x)
                        process(x)
        '''))
        self.assertNotIn("rt-membership", fam('''
            def f(items):
                seen = set()
                for x in items:
                    if x not in seen:
                        seen.add(x)
                        print(seen)
        '''))

    def test_list_returned_in_order_gets_no_switch_to_set_question(self):
        f = fam('''
            def unique(items):
                seen = []
                for x in items:
                    if x not in seen:
                        seen.append(x)
                return seen
        ''')
        self.assertEqual(f["rt-membership"]["key"], "Θ(n²)")
        self.assertNotIn("design-membership", f)

    def test_nested_loops(self):
        f = fam('''
            def pairs(nums, target):
                count = 0
                for i in range(len(nums)):
                    for j in range(i + 1, len(nums)):
                        if nums[i] + nums[j] == target:
                            count += 1
                return count
        ''')
        self.assertEqual(f["rt-nested"]["key"], "Θ(n²)")
        self.assertIn("n = `len(nums)`", f["rt-nested"]["stem"])
        f = fam("xs = [3, 1, 2]\nfor a in xs:\n    for b in xs:\n        print(a, b)\n")
        self.assertEqual(f["rt-nested"]["key"], "Θ(n²)")
        self.assertNotIn("rt-nested", fam("xs = [1]\nys = [2]\nfor a in xs:\n    for b in ys:\n        print(a, b)\n"))
        # a file or generator is used up by the first pass, so items of an unknown name are skipped
        self.assertNotIn("rt-nested", fam("rows = open('a.txt')\nfor a in rows:\n    for b in rows:\n        print(a, b)\n"))
        # three nested loops: the two-loop question would be wrong
        self.assertNotIn("rt-nested", fam("n = 9\nfor i in range(n):\n    for j in range(n):\n        for k in range(n):\n            print(i)\n"))

    def test_binary_search(self):
        f = fam('''
            def find_index(nums, target):
                lo, hi = 0, len(nums) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if nums[mid] == target:
                        return mid
                    elif nums[mid] < target:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                return -1
        ''')
        self.assertEqual(f["rt-halving"]["key"], "Θ(log n)")
        self.assertIn("sorted in ascending order", f["precondition"]["key"])

    def test_descending_binary_search(self):
        f = fam('''
            def find(nums, target):
                lo, hi = 0, len(nums) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if nums[mid] == target:
                        return mid
                    if nums[mid] > target:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                return -1
        ''')
        self.assertIn("descending", f["precondition"]["key"])

    def test_halving_needs_a_known_start(self):
        self.assertNotIn("rt-halving", fam('''
            def f(nums, target, lo, hi):
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if nums[mid] < target:
                        lo = mid + 1
                    else:
                        hi = mid - 1
        '''))

    def test_double_recursion(self):
        f = fam("def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n")
        self.assertEqual(f["rt-recursion"]["key"], "Θ(φⁿ), exponential")
        self.assertIn("Θ(n)", f["design-memo"]["key"])
        f = fam("def moves(n):\n    if n == 0:\n        return 0\n    return moves(n - 1) + moves(n - 1) + 1\n")
        self.assertEqual(f["rt-recursion"]["key"], "Θ(2ⁿ), exponential")

    def test_cached_recursion_asks_why_it_is_fast(self):
        f = fam('''
            import functools

            @functools.cache
            def fib(n):
                if n < 2:
                    return n
                return fib(n - 1) + fib(n - 2)
        ''')
        self.assertNotIn("rt-recursion", f)
        self.assertIn("Why does that make", f["design-memo"]["stem"])

    def test_sort(self):
        self.assertEqual(fam("names = list(load())\nnames.sort()\n")["rt-sort"]["key"], "Θ(n log n)")
        self.assertEqual(fam("ranked = sorted(scores)\n")["rt-sort"]["key"], "Θ(n log n)")
        self.assertNotIn("rt-sort", fam("import numpy as np\na = np.array(load())\na.sort()\n"))


class TestDesignChoices(unittest.TestCase):
    def test_list_as_queue(self):
        f = fam('''
            def bfs(graph, start):
                queue = [start]
                while queue:
                    node = queue.pop(0)
                    queue.extend(graph[node])
        ''')
        self.assertIn("Θ(1) instead of Θ(k)", f["design-deque"]["key"])
        self.assertNotIn("design-deque", fam("def f(xs):\n    return xs.pop(0)\n"))     # not known to be a list

    def test_counting_dict(self):
        f = fam('''
            def counts(words):
                c = {}
                for w in words:
                    c[w] = c.get(w, 0) + 1
                return c
        ''')
        self.assertIn("Θ(1) hash lookup", f["design-counting"]["key"])
        # `+= 1` on a plain dict raises KeyError, and adding prices isn't counting
        self.assertNotIn("design-counting", fam("def f(ws):\n    c = {}\n    for w in ws:\n        c[w] += 1\n"))
        self.assertNotIn("design-counting", fam("def f(items):\n    t = {}\n    for k, p in items:\n        t[k] = t.get(k, 0) + p\n"))


class TestInjectionGuards(unittest.TestCase):
    def assertFinds(self, code, kind_word):
        f = fam(code)
        self.assertIn("injection", f, code)
        self.assertIn(kind_word, f["injection"]["key"])

    def test_quit_word_does_not_hide_the_injection(self):
        self.assertFinds('''
            import os
            def collect():
                while True:
                    name = input("Item: ").strip()
                    if name.lower() == "done":
                        break
                    os.system("say " + name)
        ''', "CWE-78")

    def test_empty_check_length_check_and_duplicate_check_do_not_hide_it(self):
        self.assertFinds('''
            import os
            seen = []
            while True:
                name = input("Item: ")
                if not name:
                    break
                if len(name) > 40:
                    continue
                if name in seen:
                    continue
                seen.append(name)
                os.system("say " + name)
        ''', "CWE-78")

    def test_while_not_quit_loop(self):
        self.assertFinds('''
            import os
            cmd = input("> ")
            while cmd != "quit":
                os.system(cmd)
                cmd = input("> ")
        ''', "CWE-78")

    def test_real_checks_still_hide_it(self):
        for code in ('import os\ncmd = input()\nif cmd != "yes":\n    raise SystemExit\nos.system(cmd)\n',
                     'import os\ncmd = input()\nif cmd.isalnum():\n    os.system("ls " + cmd)\n',
                     'import os\nALLOWED = []\nALLOWED.append("ls")\ncmd = input()\nif cmd in ALLOWED:\n    os.system(cmd)\n'):
            self.assertNotIn("injection", fam(code), code)

    def test_path_traversal_needs_direct_user_input(self):
        f = fam('import sys\nname = sys.argv[1]\nwith open("notes/" + name) as fh:\n    print(fh.read())\n')
        self.assertIn("CWE-22", f["injection"]["key"])
        self.assertIn("read files", f["injection"]["key"])
        self.assertIn("command-line argument", f["injection"]["stem"])
        self.assertIn("Resolve the full path", f["mitigation"]["key"])
        f = fam('name = input()\nopen("out/" + name, "w").write("x")\n')
        self.assertIn("overwrite", f["injection"]["key"])
        # a tool that opens the file you name has no folder to escape from
        self.assertNotIn("injection", fam('import sys\nprint(len(open(sys.argv[1]).read().split()))\n'))
        self.assertNotIn("injection", fam("def load(path):\n    return open(path).read()\n"))
        self.assertNotIn("injection", fam("import os\nname = input()\nopen(os.path.join('notes', os.path.basename(name)))\n"))
        # basename keeps ".." and rmtree("notes/..") deletes the parent folder, so deletes still count
        f = fam("import os, shutil\nname = input()\nshutil.rmtree(os.path.join('uploads', os.path.basename(name)))\n")
        self.assertIn("delete folders", f["injection"]["key"])

    def test_basename_does_not_clean_a_shell_command(self):
        self.assertFinds('import os\nname = input()\nos.system("cat " + os.path.basename(name))\n', "CWE-78")


class TestAuditRegressions(unittest.TestCase):
    """Cases an independent audit found where an earlier version asked a question with a wrong key."""

    def none(self, code, family="injection"):
        self.assertNotIn(family, fam(code), code)

    def test_guards_on_derived_values_and_real_allowlists(self):
        self.none('import os\nALLOWED = {"ls", "pwd"}\nraw = input()\naction = raw.strip().lower()\n'
                  'if action not in ALLOWED:\n    raise SystemExit\nos.system(action)\n')
        self.none('import os, re\nhost = input()\nok = re.fullmatch(r"[a-z.]+", host)\nif not ok:\n    raise SystemExit\n'
                  'os.system("ping -c 1 " + host)\n')
        self.none('import os\ndef f():\n    cmd = input()\n    if cmd in ("ls", "pwd"):\n        return os.system(cmd)\n')
        self.none('import os\ncmd = input()\nif not cmd in ("ls", "pwd"):\n    raise SystemExit\nos.system(cmd)\n')
        self.none('import os\nwhile True:\n    a = input()\n    if a in ("ls", "pwd"):\n        break\nos.system(a)\n')
        self.none('import os\ncmd = input()\nwhile cmd not in ("ls", "pwd"):\n    cmd = input()\nos.system(cmd)\n')
        self.none('import os\nALLOWED = []\nALLOWED.extend(["status", "restart"])\ncmd = input()\n'
                  'if cmd in ALLOWED:\n    os.system("service app " + cmd)\n')
        self.none('import os\nfrom validators import check\ncmd = input()\ncheck(cmd)\nos.system(cmd)\n')
        self.none('import os\ndef check(c):\n    if not c.isalnum():\n        raise ValueError(c)\ncmd = input()\ncheck(cmd)\n'
                  'os.system(cmd)\n')
        # a function defined right here that can't raise or exit doesn't check anything
        self.assertIn("injection", fam('import os\ndef check(c):\n    pass\ncmd = input()\ncheck(cmd)\nos.system(cmd)\n'))
        self.none('import os\nCOMMANDS = {"a": "ls", "b": "pwd"}\naction = input()\n'
                  'os.system(COMMANDS.get(action, "echo unknown"))\n')

    def test_sinks_that_are_not_injection(self):
        self.none("def run(session, name):\n    return session.execute(select(User).filter_by(name=name))\n")
        self.none("def go(scheduler, job):\n    scheduler.execute(job)\n")
        self.none('import subprocess\ndef f(name):\n    subprocess.run(["ls", "-l", name], shell=True)\n')
        self.none('import subprocess\ndef f(name):\n    cmd = ["ls", "-l", name]\n    subprocess.run(cmd, shell=True)\n')
        self.none('import subprocess, shlex\ndef f(p):\n    subprocess.run(shlex.join(["grep", "-n", p, "f"]), shell=True)\n')
        self.none('def f(con, ids):\n    marks = ",".join("?" for _ in ids)\n'
                  '    return con.execute(f"SELECT * FROM t WHERE id IN ({marks})", ids)\n')
        self.none('import os\nname = input()\nos.remove(os.path.join("uploads", os.path.basename(name)))\n')
        self.none('from pathlib import Path\nname = input()\nsend_file(Path("files") / Path(name).name)\n')
        self.none('import os\n@app.route("/job/<int:pid>")\ndef kill(pid):\n    os.system("kill " + str(pid))\n')
        self.none('import os\ndef kill(pid: int):\n    os.system("kill %d" % pid)\n')

    def test_sql_positions(self):
        like = fam('def search(con, term):\n    return con.execute(f"SELECT * FROM t WHERE n LIKE \'%{term}%\'")\n')
        self.assertIn("parameterized", like["mitigation"]["key"])
        second = fam('def f(con, name):\n    return con.execute("SELECT * FROM {} WHERE n = \'{}\'".format("users", name))\n')
        self.assertIn("parameterized", second["mitigation"]["key"])
        order = fam('def f(con, column):\n    return con.execute(f"SELECT * FROM t ORDER BY {column}")\n')
        self.assertIn("allowlist", order["mitigation"]["key"])

    def test_file_and_scope_edge_cases(self):
        self.none("import json\ndef load():\n    try:\n        fh = open('a.json')\n        data = json.load(fh)\n"
                  "        fh.close()\n    except ValueError:\n        return None\n", "unclosed")
        self.none("def gen():\n    batch = []\n    while True:\n        batch.append(1)\n        yield batch\n", "lifetime")
        self.none("def f(xs):\n    for i in range(len(xs)):\n        if i % 2:\n            continue\n"
                  "        for j in range(len(xs)):\n            print(i, j)\n", "rt-nested")
        self.none("import random\nsalt = random.randint(1000, 9999)\n", "weak-random")


class TestSecurityPatterns(unittest.TestCase):
    def test_hardcoded_secret(self):
        self.assertIn("git history", fam('API_KEY = "sk-live-4f9a8b7c6d5e"\n')["hardcoded-secret"]["key"])
        self.assertIn("password", fam('db = connect(user="admin", password="hunter22")\n')["hardcoded-secret"]["stem"])
        for code in ('API_KEY = "your-api-key-here"\n', 'TOKEN_URL = "https://x.io/token"\n',
                     'API_KEY = os.environ["API_KEY"]\n', 'password = ""\n'):
            self.assertNotIn("hardcoded-secret", fam(code), code)

    def test_pickle(self):
        f = fam("import pickle\nwith open('s.pkl', 'rb') as fh:\n    state = pickle.load(fh)\n")
        self.assertIn("Run any code", f["pickle"]["key"])
        self.assertIn("CWE-502", f["pickle"]["why"])

    def test_password_hash(self):
        f = fam("import hashlib\ndef h(password):\n    return hashlib.sha256(password.encode()).hexdigest()\n")
        self.assertIn("billions of times per second", f["password-hash"]["key"])
        # a random reset token isn't a password, and a Have I Been Pwned prefix isn't storage
        self.assertNotIn("password-hash", fam("import hashlib\nt = hashlib.sha256(password_reset_token.encode()).hexdigest()\n"))
        self.assertNotIn("password-hash", fam("import hashlib\np = hashlib.sha1(password.encode()).hexdigest().upper()[:5]\n"))
        self.assertNotIn("password-hash", fam("import hashlib\ndigest = hashlib.md5(data).hexdigest()\n"))
        self.assertNotIn("password-hash", fam("import hashlib\nk = hashlib.pbkdf2_hmac('sha256', password, salt, 600000)\n"))

    def test_weak_random(self):
        f = fam("import random, string\nreset_token = ''.join(random.choice(string.ascii_letters) for _ in range(32))\n")
        self.assertIn("predictable", f["weak-random"]["key"])
        self.assertIn("weak-random", fam("import random\notp = random.randint(100000, 999999)\n"))
        for code in ("import random\ncolor = random.choice(['red', 'blue'])\n",
                     "import random\ntoken = random.choice(tokens)\n",
                     "import random\nnext_token = random.choices(vocab, weights=w)[0]\n",
                     "import random\nsalt_grams = random.randint(1, 5)\n",
                     "import random\ndef hotpot_order():\n    return random.choice(string.ascii_letters)\n"):
            self.assertNotIn("weak-random", fam(code), code)

    def test_tls_verify(self):
        f = fam("import requests\nr = requests.get('https://x.io', verify=False)\n")
        self.assertIn("certificate isn't checked", f["tls-verify"]["key"])
        self.assertNotIn("tls-verify", fam("import requests\nr = requests.get('https://x.io')\n"))

    def test_bind_all(self):
        f = fam("from http.server import HTTPServer\ns = HTTPServer(('0.0.0.0', 8000), H)\n")
        self.assertIn("Any device", f["bind-all"]["key"])
        self.assertIn("bind-all", fam("app.run(host='0.0.0.0', port=5000)\n"))
        self.assertNotIn("bind-all", fam("app.run(host='127.0.0.1')\n"))


class TestFileAccess(unittest.TestCase):
    def test_chmod_world_writable(self):
        f = fam("import os\nos.chmod('run.sh', 0o777)\n")
        self.assertEqual(f["chmod"]["key"], "Every user account on this computer")
        self.assertIn("os.fchmod(fd", fam("import os\nos.fchmod(fd, 0o666)\n")["chmod"]["stem"])
        # POSIX classes are exclusive: with 0o602 a group member can't write, so no simple answer exists
        self.assertNotIn("chmod", fam("import os\nos.chmod('f', 0o602)\n"))
        self.assertIn("CWE-732", f["chmod"]["why"])
        self.assertNotIn("chmod", fam("import os\nos.chmod('run.sh', 0o755)\n"))

    def test_toctou(self):
        f = fam("import os\nif os.path.exists(p):\n    with open(p) as fh:\n        data = fh.read()\n")
        self.assertIn("create, delete or swap", f["toctou"]["key"])
        self.assertIn("CWE-367", f["toctou"]["why"])

    def test_mktemp(self):
        self.assertIn("before this program opens it", fam("import tempfile\nname = tempfile.mktemp()\n")["mktemp"]["key"])

    def test_with_and_unclosed(self):
        self.assertIn("even if an exception", fam("with open('a.txt') as f:\n    print(f.read())\n")["with"]["key"])
        f = fam("import json\nf = open('a.json')\ndata = json.load(f)\nf.close()\n")
        self.assertIn("is skipped", f["unclosed"]["key"])
        self.assertIn("line 3 raises", f["unclosed"]["stem"])

    def test_open_mode_wording(self):
        f = fam('with open("notes.txt", "w") as fh:\n    fh.write("x")\n')
        self.assertIn("truncated to zero length", f["open-mode"]["key"])
        self.assertEqual(f["open-mode"]["lens"], "file access")


class TestAgentStyleCode(unittest.TestCase):
    """Code shaped the way coding agents usually write it: a main() function, a helper per job,
    pathlib, argparse, an existence check before the real work."""

    def injection(self, code):
        f = fam(code)
        self.assertIn("injection", f, code)
        return f["injection"]

    def none(self, code, family="injection"):
        self.assertNotIn(family, fam(code), code)

    def test_existence_check_does_not_hide_path_traversal(self):
        q = self.injection('''
            import os
            import sys

            NOTES_DIR = "notes"

            def main():
                name = sys.argv[1]
                path = os.path.join(NOTES_DIR, name)
                if not os.path.isfile(path):
                    print("No such note")
                    sys.exit(1)
                with open(path) as f:
                    print(f.read())
        ''')
        self.assertIn("read files outside", q["key"])
        # an existence check still counts as a check in front of a shell command
        self.none('import os\ncmd = input()\nif not os.path.exists(cmd):\n    raise SystemExit\nos.system(cmd)\n')
        # a real containment check hides it
        self.none('import os, sys\nname = sys.argv[1]\npath = os.path.join("notes", name)\n'
                  'if not os.path.isfile(path) or ".." in name:\n    sys.exit(1)\nopen(path)\n')

    def test_pathlib_paths(self):
        q = self.injection('''
            import sys
            from pathlib import Path

            NOTES = Path(__file__).parent / "notes"

            def main():
                note = NOTES / sys.argv[1]
                if not note.is_file():
                    sys.exit("not found")
                print(note.read_text(encoding="utf-8"))
        ''')
        self.assertIn("read files outside", q["key"])
        self.assertIn("overwrite", self.injection('from pathlib import Path\nname = input()\n'
                                                   '(Path("out") / name).write_text("x")\n')["key"])
        self.assertIn("overwrite", self.injection('from pathlib import Path\nname = input()\n'
                                                   'with Path("out").joinpath(name).open("w") as f:\n    f.write("x")\n')["key"])
        self.assertIn("read files", self.injection('from pathlib import Path\nname = input()\n'
                                                    'print(Path("notes", name).read_bytes())\n')["key"])
        # no folder to escape from, or a resolved path checked against the folder
        self.none('import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text())\n')
        self.none('''
            import sys
            from pathlib import Path
            NOTES = Path("notes").resolve()
            path = (NOTES / sys.argv[1]).resolve()
            if not path.is_relative_to(NOTES):
                raise SystemExit("escapes the folder")
            print(path.read_text())
        ''')

    def test_helper_called_from_main(self):
        f = fam('''
            import sys
            from pathlib import Path

            NOTES = Path("notes")

            def delete_note(name):
                target = NOTES.joinpath(name)
                if target.exists():
                    target.unlink()

            if __name__ == "__main__":
                delete_note(sys.argv[1])
        ''')
        self.assertIn("delete files outside", f["injection"]["key"])
        self.assertIn("command-line argument read on line 12 reaches line 9", f["injection"]["stem"])
        self.assertIn("line 12 → line 7 → line 9", f["injection"]["why"])
        self.assertIn("Resolve the full path", f["mitigation"]["key"])
        helper = 'import os, sys\nBASE = "notes"\ndef read_note(name):\n    return open(os.path.join(BASE, name)).read()\n'
        self.assertIn("injection", fam(helper + 'print(read_note(name=sys.argv[1]))\n'))
        self.assertIn("injection", fam(helper + 'read_note(sys.argv[1])\n'))
        for call in ('print(read_note("todo.txt"))\n',                                      # a constant
                     'print(read_note(os.path.basename(sys.argv[1])))\n',                   # only the file name
                     'def show(n):\n    print(read_note(n))\n',                             # two levels: not followed
                     'name = sys.argv[1]\nif name not in ("a.txt", "b.txt"):\n    sys.exit(1)\nprint(read_note(name))\n'):
            self.none(helper + call)
        self.none('import os, sys\ndef read_note(name):\n    if "/" in name or ".." in name:\n        raise ValueError(name)\n'
                  '    return open(os.path.join("notes", name)).read()\nprint(read_note(sys.argv[1]))\n')

    def test_argparse(self):
        f = fam('''
            import argparse
            import os

            def read_note(name):
                with open(os.path.join("notes", name)) as f:
                    return f.read()

            def main():
                parser = argparse.ArgumentParser()
                parser.add_argument("name")
                parser.add_argument("--lines", type=int, default=0)
                args = parser.parse_args()
                text = read_note(args.name)
                if args.lines:
                    text = "\\n".join(text.splitlines()[: args.lines])
                print(text)
        ''')
        self.assertIn("read files outside", f["injection"]["key"])
        self.assertIn("command-line argument", f["injection"]["stem"])
        head = 'import argparse, os, sys\np = argparse.ArgumentParser()\np.add_argument("host")\n'
        q = self.injection(head + 'args = p.parse_args()\nif args.verbose:\n    print("pinging")\n'
                                  'os.system("ping -c 1 " + args.host)\n')
        self.assertIn("CWE-78", q["key"])
        self.assertIn("CWE-78", self.injection(head + 'def get_args():\n    return p.parse_args()\n'
                                                      'args = get_args()\nos.system("ping " + args.host)\n')["key"])
        # options that can't carry text, and tests of the same option, hide it
        self.none('import argparse, os\np = argparse.ArgumentParser()\np.add_argument("--seconds", type=int)\n'
                  'p.add_argument("--mode", choices=["a", "b"])\np.add_argument("-q", "--quiet", action="store_true")\n'
                  'args = p.parse_args()\nos.system(f"sleep {args.seconds}; echo {args.mode} {args.quiet}")\n')
        self.none(head + 'args = p.parse_args()\nif args.host not in ("a.io", "b.io"):\n    sys.exit(1)\n'
                         'os.system("ping " + args.host)\n')
        self.none(head + 'args = p.parse_args()\nhost = args.host\nif args.host not in ("a.io", "b.io"):\n'
                         '    sys.exit(1)\nos.system("ping " + host)\n')
        self.none(head + 'def validate_host(h):\n    if not h.isalnum():\n        raise SystemExit(h)\n'
                         'args = p.parse_args()\nvalidate_host(args.host)\nos.system("ping " + args.host)\n')

    def test_weak_token_built_from_an_alphabet_variable(self):
        f = fam('''
            import random
            import string

            def make_reset_token(length=32):
                alphabet = string.ascii_letters + string.digits
                return "".join(random.choice(alphabet) for _ in range(length))
        ''')
        self.assertIn("predictable", f["weak-random"]["key"])
        self.assertIn("`make_reset_token` returns", f["weak-random"]["stem"])
        f = fam('import string\nfrom random import choices\nALPHABET = string.ascii_letters\n'
                'def generate_session_token():\n    token = "".join(choices(ALPHABET, k=32))\n    return token\n')
        self.assertIn("random.choices", f["weak-random"]["stem"])
        self.assertEqual(f["weak-random"]["line"], 5)
        self.none('import secrets, string\ndef make_reset_token():\n    alphabet = string.ascii_letters\n'
                  '    return "".join(secrets.choice(alphabet) for _ in range(32))\n', "weak-random")
        self.none('import random\ndef make_token_list(words):\n    return random.sample(words, 3)\n', "weak-random")

    def test_wording_for_agent_style_code(self):
        f = fam("def count_pairs(nums, target):\n    count = 0\n    n = len(nums)\n    for i in range(n):\n"
                "        for j in range(i + 1, n):\n            if nums[i] + nums[j] == target:\n                count += 1\n"
                "    return count\n")
        self.assertIn("grow with n = `len(nums)`?", f["rt-nested"]["stem"])
        f = fam("def grid(n):\n    for i in range(n):\n        for j in range(n):\n            print(i, j)\n")
        self.assertIn("grow with n?", f["rt-nested"]["stem"])
        f = fam("import os\nfor root, dirs, files in os.walk('d'):\n    for name in files:\n"
                "        os.chmod(os.path.join(root, name), 0o777)\n")
        self.assertIn("who can modify it?", f["chmod"]["stem"])

    def test_sort_cost_beats_basic_control_flow(self):
        code = textwrap.dedent('''
            def count_words(text):
                counts = {}
                for word in text.lower().split():
                    word = word.strip(".,!?")
                    if not word:
                        continue
                    counts[word] = counts.get(word, 0) + 1
                return sorted(counts, key=counts.get)
        ''').lstrip("\n")
        with tempfile.TemporaryDirectory() as root:
            qs = questions_for_turn([{"path": os.path.join(root, "words.py"), "before": None, "after": code}],
                                    root, random.Random(1), limit=3)
        self.assertIn("rt-sort", {q["evidence"]["family"] for q in qs})


class TestAuditRoundThree(unittest.TestCase):
    """An independent audit of the pathlib, argparse and helper-following rules: 166 snippets, and
    these are the false alarms it found. Each must stay quiet (or keep the right key)."""

    def none(self, code, family="injection"):
        self.assertNotIn(family, fam(code), code)

    def key(self, code, family="injection"):
        f = fam(code)
        self.assertIn(family, f, code)
        return f[family]["key"]

    def test_real_containment_checks_on_path_attributes(self):
        head = 'import sys\nfrom pathlib import Path\nNOTES = Path("notes").resolve()\n'
        self.none(head + 'note = (NOTES / sys.argv[1]).resolve()\nif note.parent != NOTES:\n    sys.exit(1)\n'
                         'print(note.read_text())\n')
        self.none(head + 'target = (NOTES / input()).resolve()\nif NOTES not in target.parents:\n    sys.exit(1)\n'
                         'target.unlink()\n')
        self.none(head + 'rel = Path(sys.argv[1])\nif rel.anchor or ".." in rel.parts:\n    sys.exit(1)\n'
                         'print(open(NOTES / rel).read())\n')
        self.none(head + 'p = (NOTES / sys.argv[1]).resolve()\ntry:\n    p.relative_to(NOTES)\nexcept ValueError:\n'
                         '    sys.exit("outside")\nprint(p.read_text())\n')

    def test_argparse_namespace_reaching_a_helper(self):
        helper = ('import argparse, os, sys\ndef show(args):\n    with open(os.path.join("notes", args.name)) as f:\n'
                  '        print(f.read())\np = argparse.ArgumentParser()\np.add_argument("name")\n')
        self.none(helper + 'args = p.parse_args()\nif args.name not in ("a.txt", "b.txt"):\n    sys.exit(1)\nshow(args)\n')
        self.assertIn("read files", self.key(helper + 'show(p.parse_args())\n'))
        self.none('import argparse, os\ndef show_log(args):\n    print(open(os.path.join("logs", f"day{args.day}.log")).read())\n'
                  'p = argparse.ArgumentParser()\np.add_argument("day", type=int)\nshow_log(p.parse_args())\n')
        self.none('import argparse, os\ndef run(args):\n    os.system(f"sleep {args.seconds}")\n'
                  'p = argparse.ArgumentParser()\np.add_argument("--seconds", type=int)\nrun(p.parse_args())\n')

    def test_what_the_write_can_do(self):
        head = 'import os, sys\nfrom pathlib import Path\npath = os.path.join("notes", input())\n'
        # with an existence check in front, a write may only create files, or not: "write" is true either way
        self.assertIn("write files", self.key(head + 'if os.path.exists(path):\n    sys.exit("exists")\n'
                                                     'open(path, "w").write("")\n'))
        self.assertIn("write files", self.key(head + 'p = Path(path)\nif not p.exists():\n    p.write_text("")\n'))
        self.assertIn("create new files", self.key(head + 'open(path, "x").write("")\n'))
        self.assertIn("add text", self.key(head + 'open(path, "a").write("")\n'))
        self.assertIn("change files", self.key(head + 'open(path, "r+").write("")\n'))
        self.assertIn("overwrite", self.key(head + 'mode = "w"\nopen(path, mode).write("")\n'))
        self.assertIn("open files", self.key(head + 'import random\nopen(path, random.choice("wr")).write("")\n'))
        self.assertIn("read files", self.key(head + 'raw = "-b" in sys.argv\nprint(open(path, "rb" if raw else "r").read())\n'))
        # a check for folders or links doesn't make the program refuse existing files
        self.assertIn("delete folders", self.key(head + 'import shutil\nif not os.path.isdir(path) or os.path.islink(path):\n'
                                                        '    sys.exit(1)\nshutil.rmtree(path)\n'))
        # nothing to read where nothing exists
        self.none(head + 'if os.path.exists(path):\n    sys.exit(1)\nprint(open(path).read())\n')

    def test_argparse_values_that_cannot_carry_text(self):
        head = 'import argparse, os\np = argparse.ArgumentParser()\n'
        self.none(head + 'def positive_int(s):\n    n = int(s)\n    if n <= 0:\n        raise ValueError(s)\n    return n\n'
                         'p.add_argument("--n", type=positive_int)\nargs = p.parse_args()\nos.system(f"seq {args.n}")\n')
        self.none(head + 'p.add_argument("--n", type=lambda s: int(s, 0))\nargs = p.parse_args()\nos.system(f"seq {args.n}")\n')
        self.none(head + 'p.add_argument("--color", action=argparse.BooleanOptionalAction)\nargs = p.parse_args()\n'
                         'os.system(f"ls --color={args.color}")\n')
        self.none(head + 'p.add_argument("--port")\nargs = p.parse_args()\nargs.port = int(args.port)\n'
                         'os.system(f"nc -z localhost {args.port}")\n')
        self.none(head + 'p.add_argument("--count", type=int)\nopts = vars(p.parse_args())\nos.system(f"seq {opts[\'count\']}")\n')
        self.none(head + 'p.add_argument("host")\nargs = p.parse_args(["example.com"])\nos.system("ping " + args.host)\n')
        self.none(head + 'p.add_argument("--log")\nargs = p.parse_args()\nmode = "verbose" if args.log else "quiet"\n'
                         'os.system("./run --" + mode)\n')
        self.none(head + 'p.add_argument("host")\nargs = p.parse_args()\nopts = args\n'
                         'if opts.host not in ("a.io", "b.io"):\n    raise SystemExit\nos.system("ping " + args.host)\n')
        self.none('import os\nport = input()\nport = int(port)\nos.system(f"nc -z localhost {port}")\n')

    def test_a_check_of_any_input_is_enough_to_stay_quiet(self):
        # A question about a value built from two inputs, one of them checked, would point at the
        # checked one. Staying quiet is the safe side: a missed question, never a wrong one.
        self.none('import argparse, os, sys\np = argparse.ArgumentParser()\np.add_argument("host")\np.add_argument("--port")\n'
                  'args = p.parse_args()\nif not args.port.isdigit():\n    sys.exit(1)\nos.system(f"nc -zv {args.host} {args.port}")\n')
        self.none('import re, sys, os\nhost, port = sys.argv[1], sys.argv[2]\ntarget = f"{host}:{port}"\n'
                  'if not re.fullmatch(r"[a-z.]+:[0-9]+", target):\n    sys.exit(1)\nos.system("nc " + host + " " + port)\n')
        # helpers that only print aren't checks, and a call to another file helper isn't one either
        self.assertIn("CWE-78", self.key('import os\ndef log(m):\n    print("log:", m)\nhost = input()\nlog(host)\n'
                                         'os.system("ping " + host)\n'))
        self.assertIn("CWE-78", self.key('import os, logging\nlogger = logging.getLogger()\ndef report(m):\n'
                                         '    logger.error("bad: %s", m)\nhost = input()\nreport(host)\nos.system("ping " + host)\n'))
        self.assertIn("CWE-78", self.key('import subprocess\nhost = input()\nsubprocess.check_call(f"ping {host}", shell=True)\n'))
        f = fam('import os, sys\ndef show(n):\n    print(open(os.path.join("notes", n)).read())\n'
                'def drop(n):\n    os.remove(os.path.join("notes", n))\nname = sys.argv[2]\n'
                'if sys.argv[1] == "show":\n    show(name)\nelse:\n    drop(name)\n')
        self.assertIn("injection", f)

    def test_objects_that_are_not_file_paths(self):
        self.assertIn("read files", self.key('import sys, zipfile\nzf = zipfile.ZipFile("archives/" + sys.argv[1])\n'
                                             'print(zf.open("index.json").read())\n'))
        self.assertEqual(fam('import sys, zipfile\nzf = zipfile.ZipFile("archives/" + sys.argv[1])\n'
                             'print(zf.open("index.json").read())\n')["injection"]["line"], 2)
        self.none('import sys, zipfile\nroot = zipfile.Path("bundle.zip")\nprint((root / sys.argv[1]).read_text())\n')

    def test_names_that_cannot_hold_a_slash(self):
        head = 'import os, re, sys\nfrom pathlib import Path\nNOTES = Path("notes")\nt = sys.argv[1]\n'
        for expr in ('re.sub(r"[^a-z0-9]+", "-", t.lower())', '"".join(c for c in t if c.isalnum() or c in "-_")',
                     'Path(t).stem + ".md"', 'Path(t).name', 'os.path.split(t)[1]', 't.split("/")[-1]',
                     't.replace("/", "_")', 'secure_filename(t)', 'slugify(t)'):
            self.none(head + 'print((NOTES / ({})).read_text())\n'.format(expr))
        self.none(head + 'from datetime import datetime\nd = datetime.strptime(t, "%Y-%m-%d")\n'
                         'print(open(f"logs/{d:%Y%m%d}.log").read())\n')
        self.none(head + 'import uuid\nprint(open(os.path.join("uploads", str(uuid.UUID(t)))).read())\n')
        self.none(head + 'p = Path(t)\nprint(open(os.path.join("notes", p.name)).read())\n')
        self.none('import os, sys, shutil\nfrom werkzeug.utils import secure_filename\n'
                  'shutil.rmtree(os.path.join("uploads", secure_filename(sys.argv[1])))\n')
        # a dot is allowed, so ".." survives: deleting a folder tree still counts
        self.assertIn("delete folders", self.key('import os, re, sys, shutil\nn = re.sub(r"[^a-z0-9.]", "", sys.argv[1])\n'
                                                 'shutil.rmtree(os.path.join("uploads", n))\n'))
        # replacing only the first match leaves the rest
        self.assertIn("read files", self.key(head + 'print((NOTES / re.sub(r"[/]", "_", t, count=1)).read_text())\n'))

    def test_weak_random_needs_a_real_secret(self):
        for code in ('import random, string\nsecret = "".join(random.sample(string.digits, 4))\n',
                     'import random\nsecret = random.randint(1000, 9999)\n',
                     'import random, string\ndef make_secret():\n    return "".join(random.choice(string.digits) for _ in range(4))\n',
                     'import random, string\ndef make_fake_password():\n    return "".join(random.choices(string.ascii_letters, k=10))\n',
                     'import random, string\ndef test_login():\n    password = "".join(random.choices(string.ascii_letters, k=12))\n',
                     'import string\nfrom random import SystemRandom\nrandom = SystemRandom()\n'
                     'def generate_password():\n    return "".join(random.choice(string.ascii_letters) for _ in range(16))\n'):
            self.none(code, "weak-random")
        for code in ('import random as rnd, string\ndef generate_api_key():\n'
                     '    return "".join(rnd.choice(string.ascii_letters) for _ in range(40))\n',
                     'import random, string\ndef generate_password(n=12, chars=string.ascii_letters + string.digits):\n'
                     '    return "".join(random.choice(chars) for _ in range(n))\n',
                     'import random, string\ndef make_reset_token():\n'
                     '    return "".join(random.choices(population=string.ascii_letters, k=32))\n',
                     'import random\ndef generate_session_token():\n    return random.randbytes(16).hex()\n',
                     'import random, string\napp.secret_key = "".join(random.choices(string.ascii_letters, k=32))\n'):
            self.assertIn("predictable", self.key(code, "weak-random"), code)


class TestAuditRoundFour(unittest.TestCase):
    """The second audit of the same rules: 97 more snippets. These were its false alarms and wrong keys."""

    def none(self, code, family="injection"):
        self.assertNotIn(family, fam(code), code)

    def key(self, code, family="injection"):
        f = fam(code)
        self.assertIn(family, f, code)
        return f[family]["key"]

    def test_existence_checks_must_be_about_the_written_path(self):
        head = 'import os, sys\nname = sys.argv[1]\ntarget = os.path.join("out", name)\n'
        # the lock file is checked, the target isn't: "create" would be wrong, so the key stays neutral
        self.assertIn("write files", self.key(head + 'if os.path.exists(target + ".lock"):\n    sys.exit(1)\n'
                                                     'open(target, "w").write("x")\n'))
        self.assertIn("write files", self.key(head + 'if os.path.exists(target):\n    sys.exit(1)\n'
                                                     'target = target + ".bak"\nopen(target, "w").write("x")\n'))
        # a check that comes after the write says nothing about it
        self.assertIn("overwrite", self.key(head + 'open(target, "w").write("x")\nif os.path.exists(target):\n'
                                                   '    print("ok")\n'))
        # a guard that refuses existing files: "overwrite ... like .bashrc" would be false, so the key says "write"
        self.assertIn("write files", self.key('from flask import request\nimport os\n@app.post("/n")\ndef new():\n'
                                              '    p = os.path.join("notes", request.form["name"])\n'
                                              '    if os.path.exists(p):\n        return {"error": "exists"}, 409\n'
                                              '    open(p, "w").write("")\n    return {}\n'))
        self.assertIn("write files", self.key(head + 'if not args_overwrite:\n    if os.path.exists(target):\n'
                                                     '        sys.exit(1)\nopen(target, "w").write("x")\n'))
        # only the mode itself guarantees a new file
        self.assertIn("create new", self.key('import os, sys\n'
                                             'fd = os.open(os.path.join("out", sys.argv[1]), os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n'))
        self.assertIn("open files", self.key('import os, sys\nforce = "-f" in sys.argv\nfd = os.open(os.path.join("out", '
                                             'sys.argv[1]), os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL))\n'))

    def test_save_needs_a_file_object(self):
        self.none('import os\nclass Store:\n    def __init__(self):\n        self.d = {}\n    def save(self, k):\n'
                  '        self.d[k] = 1\nstore = Store()\nstore.save(os.path.join("x", input()))\n')
        self.assertIn("overwrite", self.key('import os\nfrom openpyxl import Workbook\nwb = Workbook()\n'
                                            'wb.save(os.path.join("reports", input()))\n'))
        self.assertIn("overwrite", self.key('import os\nfrom flask import request\nf = request.files["file"]\n'
                                            'if f.filename == "":\n    abort(400)\nf.save(os.path.join("uploads", f.filename))\n'))

    def test_more_ways_to_clean_a_name(self):
        head = 'import os, re, sys, string\nt = sys.argv[1]\n'
        sink = 'open(os.path.join("out", {}), "w").write("x")\n'
        for expr in (r're.sub(r"\W+", "_", t)', r're.sub(r"[\W_]+", "", t)', r're.sub(r"\D", "", t)',
                     '"".join(c if c.isalnum() else "_" for c in t)', '"".join(filter(str.isalnum, t))',
                     '"".join(c for c in t if c.isalnum() or c == "_")', '"".join(ch.lower() for ch in t if ch.isalnum())',
                     're.subn(r"[^a-z0-9]", "", t)[0]'):
            self.none(head + sink.format(expr))
        self.none(head + 'valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)\n'
                         'name = "".join(c for c in t if c in valid_chars)\n' + sink.format("name"))
        self.none(head + 'PATTERN = r"[^a-z0-9-]"\nname = re.sub(PATTERN, "", t)\n' + sink.format("name"))
        self.none(head + 'try:\n    port = int(t)\nexcept ValueError:\n    sys.exit(1)\nos.system(f"nc -z host {t}")\n')
        self.none(head + 'try:\n    t = int(t)\nexcept ValueError:\n    sys.exit(1)\nos.system(f"nc -z host {t}")\n')
        self.none('import os, shlex\ndef run(name):\n    name = shlex.quote(name)\n    os.system("ls " + name)\n')
        self.none(head + 'def safe_name(s):\n    return re.sub(r"[^A-Za-z0-9_-]", "_", s)\n' + sink.format("safe_name(t)"))
        self.none(head + 'def checked(s):\n    if not re.fullmatch(r"[a-z]+", s):\n        raise ValueError(s)\n    return s\n'
               + sink.format("checked(t)"))
        # not clean: a homemade slugify that keeps "/", split with a limit, shlex.quote for a path
        self.assertIn("overwrite", self.key(head + 'def slugify(s):\n    return s.replace(" ", "-")\n' + sink.format("slugify(t)")))
        self.assertIn("read files", self.key(head + 'print(open(os.path.join("notes", t.split("/", 1)[1])).read())\n'))
        self.assertIn("read files", self.key(head + 'import shlex\nprint(open(os.path.join("notes", shlex.quote(t))).read())\n'))
        self.assertIn("CWE-78", self.key('import os, ipaddress\nip = ipaddress.ip_address(input())\nos.system(f"ping {ip}")\n'))

    def test_argparse_dicts_and_lists(self):
        head = 'import argparse, os\np = argparse.ArgumentParser()\np.add_argument("--count", type=int)\np.add_argument("name")\n'
        self.none(head + 'opts = vars(p.parse_args())\nos.system(f"seq {opts.get(\'count\')}")\n')
        self.none(head + 'args = p.parse_args(["--count", input()])\nos.system(f"seq {args.count}")\n')
        self.none(head + 'def show(o):\n    print(open(os.path.join("notes", o["name"])).read())\nargs = p.parse_args()\n'
                         'if args.name not in ("a", "b"):\n    raise SystemExit\nshow(vars(args))\n')
        self.none(head + 'args = p.parse_args()\nif args.count:\n    args.count = int(args.count)\nos.system(f"seq {args.count}")\n')

    def test_weak_random_context(self):
        for code in ('import hashlib, random\ndef mine(d):\n    while True:\n        nonce = random.randint(0, 4294967295)\n'
                     '        if hashlib.sha256(f"{d}{nonce}".encode()).hexdigest().startswith("00"):\n            return nonce\n',
                     'import random, string\nclass SimulatedClient:\n    def __init__(self):\n'
                     '        self.session_id = "".join(random.choices(string.ascii_letters, k=16))\n',
                     'import random, string\nclass FakeUserFactory:\n    def make_password(self):\n'
                     '        return "".join(random.choices(string.ascii_letters, k=12))\n'):
            self.none(code, "weak-random")
        self.assertIn("predictable", self.key('import random, string\napp.config["SECRET_KEY"] = '
                                              '"".join(random.choices(string.ascii_letters, k=32))\n', "weak-random"))
        self.assertIn("predictable", self.key('import string\nfrom numpy import random\ndef generate_api_key():\n'
                                              '    return "".join(random.choice(list(string.ascii_letters), 32))\n',
                                              "weak-random"))


class TestAuditRoundFive(unittest.TestCase):
    """The third audit: 50 snippets aimed at the newest rules."""

    def none(self, code, family="injection"):
        self.assertNotIn(family, fam(code), code)

    def key(self, code, family="injection"):
        f = fam(code)
        self.assertIn(family, f, code)
        return f[family]["key"]

    def test_existence_guards_never_promise_create(self):
        k = self.key('import argparse, os, sys\np = argparse.ArgumentParser()\np.add_argument("name")\n'
                     'p.add_argument("--overwrite", action="store_true")\nargs = p.parse_args()\n'
                     'out = os.path.join("exports", args.name)\nif not args.overwrite:\n    if os.path.exists(out):\n'
                     '        sys.exit("exists")\nwith open(out, "w") as f:\n    f.write("x")\n')
        self.assertIn("write files", k)
        self.assertIn("read files", self.key('import os, sys\npath = os.path.join("docs", sys.argv[1])\n'
                                             'raw = "--raw" in sys.argv\nif not os.path.isfile(path):\n    sys.exit(1)\n'
                                             'print(open(path, "rb" if raw else "r").read())\n'))

    def test_validators_that_exit_through_helpers(self):
        self.none('import os, re, sys\ndef die(m):\n    print(m)\n    sys.exit(1)\ndef validate_name(n):\n'
                  '    if not re.fullmatch(r"[\\w-]+", n):\n        die("bad")\nname = sys.argv[1]\nvalidate_name(name)\n'
                  'print(open(os.path.join("notes", name)).read())\n')
        self.none('import argparse, os, re\nap = argparse.ArgumentParser()\nap.add_argument("name")\n'
                  'def check_name(a):\n    if not re.fullmatch(r"[\\w-]+", a.name):\n        ap.error("bad")\n'
                  'args = ap.parse_args()\ncheck_name(args)\nprint(open(os.path.join("notes", args.name)).read())\n')

    def test_typed_and_cleaned_values(self):
        self.none('import argparse, os, re\ndef note_name(v):\n    if not re.fullmatch(r"[\\w-]+\\.md", v):\n'
                  '        raise argparse.ArgumentTypeError(v)\n    return v\np = argparse.ArgumentParser()\n'
                  'p.add_argument("name", type=note_name)\nargs = p.parse_args()\n'
                  'print(open(os.path.join("notes", args.name)).read())\n')
        # a type= function that returns the text unchanged on one branch is not a check
        self.assertIn("CWE-78", self.key('import argparse, os\ndef port_or_service(v):\n    if v.isdigit():\n'
                                         '        return int(v)\n    return v\np = argparse.ArgumentParser()\n'
                                         'p.add_argument("port", type=port_or_service)\nargs = p.parse_args()\n'
                                         'os.system(f"lsof -i :{args.port}")\n'))
        self.none('import os\nport = input()\ntry:\n    port = int(port)\nexcept ValueError:\n    port = 8080\n'
                  'os.system(f"lsof -i :{port}")\n')
        self.none('import os\nport = input()\nif port:\n    port = int(port)\nelse:\n    port = 8080\n'
                  'os.system(f"lsof -i :{port}")\n')
        self.assertIn("CWE-78", self.key('import os\nport = input()\ntry:\n    port = int(port)\nexcept ValueError:\n'
                                         '    print("not a number")\nos.system(f"lsof -i :{port}")\n'))
        self.none('import os\ndef wait(seconds):\n    seconds = int(seconds)\n    os.system(f"sleep {seconds}")\n')
        self.none('import argparse, os\ndef run(opts):\n    os.system(f"seq {opts[\'count\']}")\np = argparse.ArgumentParser()\n'
                  'p.add_argument("--count", type=int)\nrun(vars(p.parse_args()))\n')

    def test_more_cleaning_idioms(self):
        head = 'import os, re, string, sys\nt = sys.argv[1]\n'
        sink = 'open(os.path.join("out", {}), "w").write("x")\n'
        for expr in ('"".join("_" if ch in \'<>:"/\\\\|?*.\' else ch for ch in t)',
                     '"".join(c.lower() if c.isalnum() else "-" for c in t)',
                     '"".join(filter(lambda c: c.isalnum() or c in "-_", t))'):
            self.none(head + sink.format(expr))
        self.none(head + 'FORBIDDEN = \'<>:"/\\\\|?*\'\nsafe = "".join(c if c not in FORBIDDEN else "_" for c in t)\n'
                  + sink.format('safe + ".txt"'))
        self.none(head + 'valid = f"-_.() {string.ascii_letters}{string.digits}"\n'
                         'name = "".join(c for c in t if c in valid)\n' + sink.format("name"))
        self.none('import os, re\nclass Exporter:\n    SAFE = re.compile(r"[^A-Za-z0-9_-]")\n    def run(self):\n'
                  '        name = self.SAFE.sub("_", input())\n        open(os.path.join("exports", name), "w").write("")\n')
        self.none('import os, re\ndef sanitize(text, pattern):\n    return pattern.sub("_", text)\n'
                  'STRICT = re.compile(r"[^A-Za-z0-9_-]")\nname = sanitize(input(), STRICT)\n'
                  'open(os.path.join("exports", name + ".csv"), "w").write("")\n')
        # a path-building helper is seen through, so the caller's open() is a finding
        self.assertIn("read files", self.key('import os, sys\ndef note_path(n):\n    return os.path.join("notes", n)\n'
                                             'print(open(note_path(sys.argv[1])).read())\n'))
        # keep-lists that allow "/" stay findings
        self.assertIn("overwrite", self.key(head + 'name = "".join(c for c in t if c in string.printable)\n'
                                            + sink.format("name")))

    def test_save_on_classes_defined_here(self):
        self.none('class ReportDocument:\n    def __init__(self):\n        self.store = {}\n    def save(self, key):\n'
                  '        self.store[key] = 1\ndoc = ReportDocument()\ndoc.save("reports/" + input())\n')
        self.assertIn("overwrite", self.key('import os\nfrom docx import Document\nd = Document()\n'
                                            'd.save(os.path.join("out", input()))\n'))

    def test_weak_random_in_load_tests_and_real_tokens(self):
        self.none('import random, string\nfrom locust import HttpUser, task\nclass SignupUser(HttpUser):\n    @task\n'
                  '    def signup(self):\n        payload = {}\n'
                  '        payload["password"] = "".join(random.choices(string.ascii_letters, k=12))\n', "weak-random")
        self.none('import numpy as np\ndf = {}\ndf["session_id"] = np.random.randint(0, 10**9, size=1000)\n', "weak-random")
        self.assertIn("predictable", self.key('import random, string\ndef refresh(store):\n'
                                              '    latest_token = "".join(random.choices(string.ascii_letters, k=32))\n'
                                              '    return latest_token\ndef generate_reset_token():\n'
                                              '    return "".join(random.choices(string.ascii_letters, k=32))\n',
                                              "weak-random"))


class TestEveryQuestionIsWellFormed(unittest.TestCase):
    def test_all_new_families_build_valid_questions(self):
        programs = [
            "def first_repeat(emails):\n    seen = []\n    for e in emails:\n        if e in seen:\n            return e\n        seen.append(e)\n",
            "import os\nimport random\nimport hashlib\nimport pickle\nimport tempfile\nimport requests\n"
            "API_KEY = 'sk-live-4f9a8b7c6d5e'\n"
            "token = ''.join(random.choice('ab') for _ in range(8))\n"
            "def h(password):\n    return hashlib.sha256(password.encode()).hexdigest()\n"
            "state = pickle.load(open('s.pkl', 'rb'))\n"
            "os.chmod('x', 0o777)\nname = tempfile.mktemp()\n"
            "r = requests.get('https://x.io', verify=False)\n"
            "if os.path.exists('a'):\n    open('a').read()\n",
            "def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n",
            "def find(nums, t):\n    lo, hi = 0, len(nums) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n"
            "        if nums[mid] == t:\n            return mid\n        elif nums[mid] < t:\n            lo = mid + 1\n"
            "        else:\n            hi = mid - 1\n    return -1\n",
            "import sqlite3\nconn = sqlite3.connect('a.db')\nname = input()\nconn.execute(f\"SELECT * FROM u WHERE n = '{name}'\")\n",
        ]
        rng = random.Random(0)
        for code in programs:
            for s in specs(code):
                self.assertNotIn(s["key"], [d[0] for d in s["distractors"]], s["family"])
                q = question("t-x-0", s["lens"], s["stem"], s["key"], s["why"], s["distractors"], rng)
                self.assertIsNotNone(q, (s["family"], s["key"], s["distractors"]))
                for o in q["options"]:
                    if o["letter"] != q["answer"]:
                        self.assertTrue(o["why"], (s["family"], o["text"]))


class TestNoFamilyCrashes(unittest.TestCase):
    """specs_for_file skips a family that raises, so a crash would only show up as a missing
    question. Here every family has to run cleanly."""

    def test_families_run_without_errors(self):
        import code_questions
        programs = [
            "import os\ndef f(names):\n    keep = [n for n in names if n.isalnum()]\n    os.system('ls ' + keep[0])\n",
            "import os, sys\nx = {k: v for k, v in os.environ.items() if k.startswith('A')}\nprint(sys.argv[1:])\n",
            "import argparse, os\np = argparse.ArgumentParser()\np.add_argument(*('--n',), **{'type': int})\n"
            "a = p.parse_args()\nos.system(f'seq {a.n}')\n",
            "import sys\nfrom pathlib import Path\nARGS = ('w',)\np = Path('out') / sys.argv[1]\nwith p.open(*ARGS) as f:\n"
            "    f.write('x')\n",
            "import os, sys\ndef h(n):\n    return open(os.path.join('d', n)).read()\ndef h(n):\n    return n\nprint(h(sys.argv[1]))\n",
            "import os\nasync def go(name):\n    async with lock:\n        os.system('x ' + name)\n",
            "try:\n    pass\nexcept* ValueError:\n    pass\n",
        ]
        failures = []

        def strict(fn):
            def run(*a, **k):
                try:
                    return fn(*a, **k)
                except Exception as e:      # pragma: no cover - reported below
                    failures.append("{}: {!r}".format(fn.__name__, e))
                    raise
            return run

        saved = list(code_questions.PY_FAMILIES)
        code_questions.PY_FAMILIES[:] = [strict(f) for f in saved]
        try:
            for code in programs:
                specs(code)
        finally:
            code_questions.PY_FAMILIES[:] = saved
        self.assertEqual(failures, [])


class TestSelection(unittest.TestCase):
    def test_a_turn_spreads_across_lenses(self):
        code = textwrap.dedent('''
            import hashlib
            import random
            import string

            API_KEY = "sk-live-4f9a8b7c6d5e"

            def hash_password(password):
                return hashlib.sha256(password.encode()).hexdigest()

            def make_token():
                return "".join(random.choice(string.ascii_letters) for _ in range(32))
        ''').lstrip("\n")
        with tempfile.TemporaryDirectory() as root:
            qs = questions_for_turn([{"path": os.path.join(root, "auth.py"), "before": None, "after": code}],
                                    root, random.Random(1), limit=3)
        families = {q["evidence"]["family"] for q in qs}
        self.assertEqual(families, {"hardcoded-secret", "password-hash", "weak-random"})
        self.assertNotIn("import", families)          # a strong question in a used lens beats a weak one


if __name__ == "__main__":
    unittest.main()
