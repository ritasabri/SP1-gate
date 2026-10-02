"""Phase 2 tests: each code pattern gets the right key, and code we can't be sure about gets none."""
import os
import random
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gate"))

from code_questions import changed_lines, questions_for_turn, specs_for_file  # noqa: E402


def specs(code, before=None, name="app.py", root=None):
    code = textwrap.dedent(code).lstrip("\n")
    root = root or tempfile.gettempdir()
    return specs_for_file(os.path.join(root, name), name, before, code, root)


def by_family(code, **kw):
    out = {}
    for s in specs(code, **kw):
        out.setdefault(s["family"], s)
    return out


class TestChangedLines(unittest.TestCase):
    def test_new_file_is_all_lines(self):
        self.assertEqual(changed_lines(None, "a\nb\n"), {1, 2})

    def test_edit_marks_only_new_lines(self):
        self.assertEqual(changed_lines("a\nb\nc\n", "a\nB\nc\nd\n"), {2, 4})


class TestDataStructures(unittest.TestCase):
    FIRST_REPEAT = '''
        def first_repeat(emails):
            seen = []
            for e in emails:
                if e in seen:
                    return e
                seen.append(e)
            return None
    '''

    def test_first_repeat(self):
        f = by_family(self.FIRST_REPEAT)
        self.assertIn("up to k comparisons", f["membership"]["key"])
        self.assertEqual(f["membership"]["priority"], 6)            # inside a loop
        self.assertIn("A list", f["container"]["key"])
        self.assertIn("new, empty `seen`", f["lifetime"]["key"])
        self.assertIn("ends `first_repeat` right away", f["flow"]["key"])

    def test_set_membership_is_hashed(self):
        f = by_family("seen = set()\nif 3 in seen:\n    print(1)\n")
        self.assertIn("hash lookup", f["membership"]["key"])

    def test_empty_braces_are_a_dict(self):
        f = by_family("d = {}\n")
        self.assertIn("A dict", f["container"]["key"])
        set_feedback = [w for t, w in f["container"]["distractors"] if t.startswith("A set")][0]
        self.assertIn("`set()`", set_feedback)

    def test_name_with_two_kinds_is_skipped(self):
        f = by_family("x = []\nx = {}\nif 1 in x:\n    pass\n")
        self.assertNotIn("container", f)
        self.assertNotIn("membership", f)

    def test_parameters_are_not_guessed(self):
        f = by_family("def f(items):\n    return 3 in items\n")
        self.assertNotIn("membership", f)

    def test_collections_need_the_import(self):
        self.assertIn("A deque", by_family("from collections import deque\nq = deque()\n")["container"]["key"])
        self.assertNotIn("container", by_family("q = deque()\n"))

    def test_mutable_default(self):
        f = by_family('''
            def add_item(item, items=[]):
                items.append(item)
                return items
        ''')
        self.assertIn("The same list, made once", f["mutable-default"]["key"])
        self.assertIn("additions are still there", f["mutable-default"]["key"])

    def test_mutable_default_reassigned_is_skipped(self):
        f = by_family('''
            def add_item(item, items=[]):
                items = list(items)
                items.append(item)
                return items
        ''')
        self.assertNotIn("mutable-default", f)

    def test_cache_inside_function(self):
        f = by_family('''
            def get_profile(user_id):
                cache = {}
                if user_id not in cache:
                    cache[user_id] = fetch(user_id)
                return cache[user_id]
        ''')
        self.assertEqual(f["lifetime"]["priority"], 7)
        self.assertIn("hash lookup", f["membership"]["key"])

    def test_closure_keeps_it_alive_so_skip(self):
        f = by_family('''
            def make_counter():
                counts = {}
                def bump(k):
                    counts[k] = counts.get(k, 0) + 1
                return bump
        ''')
        self.assertNotIn("lifetime", f)


class TestLogic(unittest.TestCase):
    def test_dict_loop_and_items(self):
        f = by_family("ages = {'a': 1}\nfor name in ages:\n    print(name)\n")
        self.assertIn("Each key of `ages`", f["iteration"]["key"])
        f = by_family("for k, v in ages.items():\n    print(k, v)\n")
        self.assertIn("`k` is a key and `v` is that key's value", f["iteration"]["key"])

    def test_enumerate_start(self):
        f = by_family("for i, x in enumerate(names, start=1):\n    print(i, x)\n")
        self.assertIn("counts 1, 2, 3", f["iteration"]["key"])
        wrong = [t for t, _ in f["iteration"]["distractors"]]
        self.assertTrue(any("counts 0, 1, 2" in t for t in wrong))

    def test_range_len(self):
        f = by_family("for i in range(len(names)):\n    print(names[i])\n")
        self.assertIn("Each position in `names`", f["iteration"]["key"])

    def test_range_count(self):
        self.assertEqual(by_family("for i in range(2, 10, 3):\n    print(i)\n")["loop-count"]["key"], "3 times")
        self.assertEqual(by_family("for i in range(5):\n    print(i)\n")["loop-count"]["key"], "5 times")
        self.assertNotIn("loop-count", by_family("for i in range(5):\n    if i == 2:\n        break\n"))

    def test_zip(self):
        f = by_family("for a, b in zip(xs, ys):\n    print(a, b)\n")
        self.assertIn("stopping at the shorter one", f["iteration"]["key"])

    def test_break_continue_while(self):
        f = by_family('''
            def f(xs):
                for x in xs:
                    if x:
                        continue
                    print(x)
        ''')
        self.assertIn("next item", f["flow"]["key"])
        f = by_family('''
            def f(n):
                while n > 0:
                    n -= 1
                    if n == 3:
                        continue
        ''')
        self.assertIn("condition is checked again", f["flow"]["key"])

    def test_return_in_try_finally_is_skipped(self):
        f = by_family('''
            def f(xs):
                for x in xs:
                    try:
                        return x
                    finally:
                        print("done")
        ''')
        self.assertNotIn("flow", f)

    def test_swallowed_exception(self):
        f = by_family('''
            def load(path):
                try:
                    return open(path).read()
                except Exception:
                    pass
        ''')
        self.assertIn("silently ignored", f["swallowed"]["key"])
        self.assertNotIn("swallowed", by_family("try:\n    x()\nexcept ValueError:\n    pass\n"))

    def test_discarded_string_method(self):
        f = by_family('name = input("Name? ")\nname.strip()\nprint(name)\n')
        self.assertIn("thrown away", f["immutable-str"]["key"])
        self.assertNotIn("immutable-str", by_family("def f(name):\n    name.strip()\n"))


class TestFilesAndImports(unittest.TestCase):
    def test_open_modes(self):
        self.assertIn("erased", by_family('with open("notes.txt", "w") as f:\n    f.write("x")\n')["open-mode"]["key"])
        self.assertIn("kept", by_family('f = open("log.txt", mode="a")\n')["open-mode"]["key"])
        self.assertIn("FileExistsError", by_family('f = open("new.txt", "x")\n')["open-mode"]["key"])
        self.assertNotIn("open-mode", by_family('f = open("in.txt")\n'))
        self.assertNotIn("open-mode", by_family('from gzip import open\nf = open("a.gz", "w")\n'))

    def test_imports(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "helpers.py"), "w") as f:
                f.write("x = 1\n")
            got = {s["stem"].split("`")[1]: s["key"] for s in
                   specs("import os\nimport requests\nimport helpers\n", root=root) if s["family"] == "import"}
            self.assertIn("standard library", got["os"])
            self.assertIn("third-party", got["requests"])
            self.assertIn("A file in this project", got["helpers"])

    def test_import_in_another_project_folder_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "src", "mylib"))
            got = [s for s in specs("import mylib\n", root=root) if s["family"] == "import"]
            self.assertEqual(got, [])

    def test_new_file_kind(self):
        f = by_family("<h1>Hi</h1>\n", name="index.html")
        self.assertIn("web page", f["file-kind"]["key"])


class TestSecurity(unittest.TestCase):
    def test_shell_injection(self):
        f = by_family('''
            import os
            def list_dir(folder):
                os.system("ls " + folder)
        ''')
        self.assertIn("Run extra shell commands", f["taint"]["key"])
        self.assertIn("Whoever calls `list_dir` controls `folder`", f["taint"]["stem"])

    def test_guarded_or_sanitized_input_is_skipped(self):
        self.assertNotIn("taint", by_family('''
            import os
            ALLOWED = {"a", "b"}
            def run(cmd):
                if cmd in ALLOWED:
                    os.system(cmd)
        '''))
        self.assertNotIn("taint", by_family('''
            import os
            def nap(n):
                os.system("sleep " + str(int(n)))
        '''))
        self.assertNotIn("taint", by_family('''
            import subprocess
            def ls(folder):
                subprocess.run(["ls", folder])
        '''))

    def test_input_at_module_level(self):
        f = by_family('import os\ncmd = input("> ")\nos.system(cmd)\n')
        self.assertIn("Whoever runs this program controls the input on line 2", f["taint"]["stem"])
        self.assertNotIn("taint", by_family('import os\ncmd = input("> ")\nif cmd == "ls":\n    os.system(cmd)\n'))

    def test_sql(self):
        f = by_family('''
            def find(cur, name):
                cur.execute(f"SELECT * FROM users WHERE name = '{name}'")
        ''')
        self.assertIn("Change the SQL query itself", f["taint"]["key"])
        self.assertNotIn("taint", by_family('''
            def find(cur, name):
                cur.execute("SELECT * FROM users WHERE name = ?", (name,))
        '''))

    def test_eval(self):
        f = by_family("expr = input()\nprint(eval(expr))\n")
        self.assertIn("Run any Python code", f["taint"]["key"])

    def test_model_reply_to_shell(self):
        f = by_family('''
            import subprocess
            def fix_ticket(ticket_text):
                prompt = f"Reply with one shell command:\\n{ticket_text}"
                reply = client.messages.create(model="m", max_tokens=100, messages=[{"role": "user", "content": prompt}])
                command = reply.content[0].text
                subprocess.run(command, shell=True)
        ''')
        self.assertIn("via the model's reply", f["taint"]["key"])
        self.assertIn("`max_tokens=100`", f["llm-limit"]["key"])


class TestTurnSelection(unittest.TestCase):
    def test_only_changed_lines_are_asked(self):
        before = "def f(xs):\n    seen = []\n    return 3 in seen\n"
        after = before + "\ndef g():\n    d = {}\n    return d\n"
        fams = {s["family"]: s["line"] for s in specs(after, before=before)}
        self.assertTrue(all(line >= 5 for line in fams.values()), fams)

    def test_turn_picks_highest_priority_one_per_family(self):
        rng = random.Random(3)
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "app.py")
            code = textwrap.dedent(TestDataStructures.FIRST_REPEAT).lstrip("\n")
            qs = questions_for_turn([{"path": path, "before": None, "after": code}], root, rng, limit=3)
        self.assertEqual(len(qs), 3)
        self.assertEqual(len({q["id"].split("-")[1] for q in qs}), 3)
        for q in qs:
            self.assertEqual(len({o["text"] for o in q["options"]}), 4)
            self.assertEqual(q["evidence"]["file"], "app.py")

    def test_broken_code_gives_no_python_questions(self):
        self.assertEqual([s for s in specs("def f(:\n") if s["family"] != "file-kind"], [])


if __name__ == "__main__":
    unittest.main()
