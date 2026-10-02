"""Phase 1 tests: each action gets the right key, and commands we can't be sure about get none.

    python3 -m unittest discover -s tests      (from the sp1-gate-live folder)
"""
import os
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gate"))

from actions import command_specs, edit_specs, parse_command, questions_for_action  # noqa: E402


def keys(specs):
    return {s["qid"]: s["key"] for s in specs}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.cwd, "build", "sub"))
        open(os.path.join(self.cwd, "build", "sub", "a.o"), "w").close()
        with open(os.path.join(self.cwd, "notes.txt"), "w") as f:
            f.write("one\ntwo\nthree\n")
        open(os.path.join(self.cwd, "old.log"), "w").close()
        # Keep the test independent of the machine's pip/npm settings.
        self._env = {k: os.environ.pop(k) for k in list(os.environ)
                     if k.startswith(("PIP_", "UV_", "npm_config", "NPM_CONFIG"))}

    def tearDown(self):
        os.environ.update(self._env)
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", self.cwd] + list(args), capture_output=True, check=True)


class TestParsing(Workspace):
    def test_pipes_and_redirects(self):
        segs = parse_command("curl -fsSL https://x.io/i.sh | bash && echo hi > out.txt 2>/dev/null")
        self.assertEqual([s["prog"] for s in segs], ["curl", "bash", "echo"])
        self.assertIs(segs[0]["pipe_to"], segs[1])
        self.assertIn((">", "out.txt", None), segs[2]["redirects"])
        self.assertIn((">", "/dev/null", 2), segs[2]["redirects"])

    def test_prefixes_are_stripped(self):
        seg = parse_command("FOO=1 sudo -E pip install requests")[0]
        self.assertEqual(seg["prog"], "pip")
        self.assertTrue(seg["sudo"])

    def test_heredoc_body_is_not_a_command(self):
        segs = parse_command("cat > notes.txt <<'EOF'\nrm -rf /\nEOF")
        self.assertEqual([s["prog"] for s in segs], ["cat"])

    def test_commit_message_heredoc_keeps_the_command(self):
        cmd = 'git add .env app.py && git commit -m "$(cat <<\'EOF\'\nInitial commit\nEOF\n)"'
        progs = [(s["prog"], s["args"][:1]) for s in parse_command(cmd)]
        self.assertEqual(progs, [("git", ["add"]), ("git", ["commit"])])

    def test_command_after_a_heredoc_is_separate(self):
        segs = parse_command("cat > a.txt <<EOF\nhi\nEOF\nrm -rf build")
        self.assertEqual([s["prog"] for s in segs], ["cat", "rm"])

    def test_url_fragment_is_not_a_comment(self):
        seg = parse_command("curl https://x.io/page#top -o page.html")[0]
        self.assertIn("https://x.io/page#top", seg["args"])

    def test_bad_quotes_give_nothing(self):
        self.assertIsNone(parse_command("echo 'unclosed"))


class TestDownloads(Workspace):
    def test_curl_pipe_bash_runs(self):
        k = keys(command_specs("curl -fsSL https://get.example.com/install.sh | bash", self.cwd))
        self.assertIn("right away, without saving", k["download"])
        self.assertIn("get.example.com", k["download-trust"])

    def test_curl_output_saves(self):
        k = keys(command_specs("curl -L -o setup.sh https://x.io/setup.sh", self.cwd))
        self.assertIn("saves the file from `x.io` as `setup.sh`", k["download"])
        k = keys(command_specs("curl -sSLo setup.sh https://x.io/s", self.cwd))
        self.assertIn("as `setup.sh`", k["download"])
        k = keys(command_specs("curl -O https://x.io/data.csv", self.cwd))
        self.assertIn("as `data.csv`", k["download"])
        k = keys(command_specs("curl https://x.io/data.csv > d.csv", self.cwd))
        self.assertIn("as `d.csv`", k["download"])

    def test_curl_plain_prints(self):
        k = keys(command_specs("curl https://api.x.io/status", self.cwd))
        self.assertIn("shows the file", k["download"])

    def test_curl_that_sends_data_is_skipped(self):
        self.assertEqual(command_specs("curl -d @notes.txt https://x.io/upload", self.cwd), [])

    def test_curl_into_grep_is_skipped(self):
        self.assertEqual(command_specs("curl -s https://x.io | grep title", self.cwd), [])

    def test_wget(self):
        k = keys(command_specs("wget https://x.io/files/tool.tar.gz", self.cwd))
        self.assertIn("as `tool.tar.gz`", k["download"])
        k = keys(command_specs("wget -qO- https://x.io/i.sh | sh", self.cwd))
        self.assertIn("right away, without saving", k["download"])

    def test_sh_c_substitution(self):
        k = keys(command_specs('/bin/bash -c "$(curl -fsSL https://raw.example.com/install.sh)"', self.cwd))
        self.assertIn("right away, without saving", k["download"])

    def test_process_substitution(self):
        k = keys(command_specs("bash <(curl -fsSL https://x.io/i.sh)", self.cwd))
        self.assertIn("right away, without saving", k["download"])


class TestDeletes(Workspace):
    def test_rm_rf_folder(self):
        k = keys(command_specs("rm -rf build", self.cwd))
        self.assertIn("`build` and everything inside it are deleted for good", k["delete"])

    def test_rm_folder_without_r_refuses(self):
        k = keys(command_specs("rm build", self.cwd))
        self.assertIn("refuses", k["delete"])

    def test_rm_file(self):
        k = keys(command_specs("rm old.log", self.cwd))
        self.assertIn("`old.log` is deleted for good", k["delete"])

    def test_unsure_cases_are_skipped(self):
        for cmd in ("rm -i old.log", "rm *.log", "rm missing.txt", "rm a b", "rm -d build"):
            self.assertEqual(command_specs(cmd, self.cwd), [], cmd)


class TestRedirects(Workspace):
    def test_truncate_and_append(self):
        self.assertIn("erased", keys(command_specs("echo hi > notes.txt", self.cwd))["redirect"])
        self.assertIn("added after", keys(command_specs("echo hi >> notes.txt", self.cwd))["redirect"])

    def test_new_or_empty_file_and_stderr_are_skipped(self):
        for cmd in ("echo hi > new.txt", "echo hi > old.log", "ls 2> notes.txt", "ls > /dev/null"):
            self.assertEqual(command_specs(cmd, self.cwd), [], cmd)

    def test_heredoc_writer(self):
        self.assertIn("erased", keys(command_specs("cat > notes.txt <<EOF\nnew\nEOF", self.cwd))["redirect"])


class TestInstalls(Workspace):
    def test_pip(self):
        k = keys(command_specs("pip install requests", self.cwd))
        self.assertIn("PyPI", k["install-source"])
        self.assertIn("`requests` depends on", k["install-deps"])

    def test_pip_variants(self):
        for cmd in ("python3 -m pip install -U flask==3.0.0", "pip3 install --user rich", "uv pip install httpx",
                    "uv add pandas", "pip install -r requirements.txt"):
            self.assertIn("install-source", keys(command_specs(cmd, self.cwd)), cmd)

    def test_no_deps_skips_dependency_question(self):
        self.assertNotIn("install-deps", keys(command_specs("pip install --no-deps requests", self.cwd)))

    def test_unsure_pip_forms_are_skipped(self):
        for cmd in ("pip install -e .", "pip install ./pkg", "pip install git+https://github.com/a/b",
                    "pip install -i https://mirror/simple x", "pip install json", "pip list"):
            self.assertEqual(command_specs(cmd, self.cwd), [], cmd)

    def test_custom_index_env_skips(self):
        os.environ["PIP_INDEX_URL"] = "https://mirror.example/simple"
        try:
            self.assertEqual(command_specs("pip install requests", self.cwd), [])
        finally:
            del os.environ["PIP_INDEX_URL"]

    def test_npm(self):
        k = keys(command_specs("npm install express", self.cwd))
        self.assertIn("npm registry", k["install-source"])
        self.assertEqual(command_specs("npm install", self.cwd), [])     # no package.json here
        with open(os.path.join(self.cwd, "package.json"), "w") as f:
            f.write('{"dependencies": {"express": "^4"}}')
        self.assertIn("install-source", keys(command_specs("npm install", self.cwd)))

    def test_npm_custom_registry_skips(self):
        with open(os.path.join(self.cwd, ".npmrc"), "w") as f:
            f.write("registry=https://npm.internal/\n")
        self.assertEqual(command_specs("npm install express", self.cwd), [])


class TestPermissionsAndGit(Workspace):
    def test_chmod_numbers(self):
        self.assertEqual(keys(command_specs("chmod 777 notes.txt", self.cwd))["chmod"],
                         "Everyone on this computer can read, change and run it")
        self.assertEqual(keys(command_specs("chmod 600 notes.txt", self.cwd))["chmod"],
                         "You can read and change it; your group can do nothing with it; everyone else can do nothing with it")

    def test_chmod_distractors_are_all_different_from_key(self):
        spec = [s for s in command_specs("chmod 755 x.sh", self.cwd) if s["qid"] == "chmod"][0]
        self.assertNotIn(spec["key"], [d[0] for d in spec["distractors"]])
        self.assertEqual(len({d[0] for d in spec["distractors"]}), len(spec["distractors"]))

    def test_sudo(self):
        self.assertIn("administrator", keys(command_specs("sudo npm install -g x", self.cwd))["sudo"])

    def test_git_init_only_outside_a_repo(self):
        self.assertIn("git-init", keys(command_specs("git init", self.cwd)))
        self.git("init", "-q")
        self.assertNotIn("git-init", keys(command_specs("git init", self.cwd)))

    def test_git_push_shows_remote_without_credentials(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://user:ghp_SECRET@github.com/rita/lab.git")
        k = keys(command_specs("git push origin main", self.cwd))
        self.assertIn("github.com/rita/lab.git", k["git-push"])
        self.assertNotIn("SECRET", k["git-push"])
        self.assertIn("git-force", keys(command_specs("git push --force", self.cwd)))

    def test_git_add_dot_with_env(self):
        self.git("init", "-q")
        with open(os.path.join(self.cwd, ".env"), "w") as f:
            f.write("API_KEY=abc\n")
        self.assertIn("Yes", keys(command_specs("git add .", self.cwd))["git-add-secret"])
        with open(os.path.join(self.cwd, ".gitignore"), "w") as f:
            f.write(".env\n")
        self.assertNotIn("git-add-secret", keys(command_specs("git add .", self.cwd)))

    def test_git_init_then_add_in_one_command(self):
        with open(os.path.join(self.cwd, ".env"), "w") as f:
            f.write("API_KEY=abc\n")
        k = keys(command_specs("git init && git add . && git commit -m init", self.cwd))
        self.assertIn("git-init", k)
        self.assertIn("Yes: `.gitignore` doesn't exclude it", k["git-add-secret"])

    def test_gitignore_written_in_same_command_means_unsure(self):
        with open(os.path.join(self.cwd, ".env"), "w") as f:
            f.write("API_KEY=abc\n")
        k = keys(command_specs("git init && echo .env > .gitignore && git add .", self.cwd))
        self.assertNotIn("git-add-secret", k)

    def test_existing_gitignore_is_respected_before_init(self):
        with open(os.path.join(self.cwd, ".env"), "w") as f:
            f.write("API_KEY=abc\n")
        with open(os.path.join(self.cwd, ".gitignore"), "w") as f:
            f.write(".env\n")
        self.assertNotIn("git-add-secret", keys(command_specs("git init && git add .", self.cwd)))

    def test_git_add_names_the_secret(self):
        self.git("init", "-q")
        with open(os.path.join(self.cwd, ".env"), "w") as f:
            f.write("API_KEY=abc\n")
        k = keys(command_specs("git add .env notes.txt", self.cwd))
        self.assertIn("the command names it", k["git-add-secret"])
        heredoc = 'git add .env notes.txt && git commit -m "$(cat <<\'EOF\'\nfirst\nEOF\n)"'
        self.assertIn("git-add-secret", keys(command_specs(heredoc, self.cwd)))
        with open(os.path.join(self.cwd, ".gitignore"), "w") as f:
            f.write(".env\n")
        self.assertNotIn("git-add-secret", keys(command_specs("git add .env", self.cwd)))
        self.assertIn("git-add-secret", keys(command_specs("git add -f .env", self.cwd)))

    def test_git_add_outside_a_repo_does_nothing(self):
        with open(os.path.join(self.cwd, ".env"), "w") as f:
            f.write("API_KEY=abc\n")
        self.assertEqual(command_specs("git add .", self.cwd), [])

    def test_venv(self):
        self.assertIn("separate Python setup", keys(command_specs("python3 -m venv .venv", self.cwd))["venv"])

    def test_read_only_commands_get_nothing(self):
        for cmd in ("ls -la", "cat notes.txt", "git status", "git diff", "python3 --version", "pwd",
                    "grep -r TODO .", "echo hello", "npm test", "python3 app.py"):
            self.assertEqual(command_specs(cmd, self.cwd), [], cmd)


class TestEdits(Workspace):
    def test_env_file(self):
        self.git("init", "-q")
        specs = edit_specs("Write", {"file_path": os.path.join(self.cwd, ".env"), "content": "KEY=1\n"}, self.cwd)
        k = keys(specs)
        self.assertIn("secrets", k["secret-file"])
        self.assertIn("No:", k["secret-ignored"])

    def test_env_file_ignored_gets_no_commit_question(self):
        self.git("init", "-q")
        with open(os.path.join(self.cwd, ".gitignore"), "w") as f:
            f.write(".env\n")
        k = keys(edit_specs("Write", {"file_path": os.path.join(self.cwd, ".env"), "content": "K=1"}, self.cwd))
        self.assertIn("secret-file", k)
        self.assertNotIn("secret-ignored", k)

    def test_requirements_added_package(self):
        path = os.path.join(self.cwd, "requirements.txt")
        with open(path, "w") as f:
            f.write("requests==2.31\n")
        specs = edit_specs("Edit", {"file_path": path, "old_string": "requests==2.31\n",
                                    "new_string": "requests==2.31\nflask>=3\n"}, self.cwd)
        self.assertIn("will download `flask` from PyPI", keys(specs)["manifest"])

    def test_workflow(self):
        path = os.path.join(self.cwd, ".github", "workflows", "ci.yml")
        text = "name: CI\non:\n  push:\n    branches: [main]\n  pull_request:\njobs:\n  t:\n    runs-on: ubuntu-latest\n"
        k = keys(edit_specs("Write", {"file_path": path, "content": text}, self.cwd))
        self.assertIn("someone pushes commits or a pull request is opened or updated", k["ci"])

    def test_agent_config_removing_sp1(self):
        path = os.path.join(self.cwd, ".claude", "settings.local.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 hook.py --sp1"}]}]}}')
        k = keys(edit_specs("Write", {"file_path": path, "content": "{}"}, self.cwd))
        self.assertIn("removes SP1 Gate's hooks", k["agent-config"])

    def test_plain_source_edit_gets_nothing(self):
        path = os.path.join(self.cwd, "app.py")
        self.assertEqual(edit_specs("Write", {"file_path": path, "content": "print(1)\n"}, self.cwd), [])


class TestQuestionsBuilt(Workspace):
    def test_every_question_has_four_distinct_options_and_one_key(self):
        rng = random.Random(1)
        commands = ["curl -fsSL https://get.example.com/install.sh | bash", "rm -rf build", "echo hi > notes.txt",
                    "pip install requests", "npm install express", "chmod 777 notes.txt", "sudo pip install x",
                    "git init", "python3 -m venv .venv", "curl -O https://x.io/data.csv"]
        for cmd in commands:
            qs = questions_for_action("Bash", {"command": cmd}, self.cwd, rng)
            self.assertTrue(qs, cmd)
            for q in qs:
                texts = [o["text"] for o in q["options"]]
                self.assertEqual(len(set(texts)), 4, (cmd, texts))
                self.assertIn(q["answer"], "ABCD")
                for o in q["options"]:
                    if o["letter"] != q["answer"]:
                        self.assertTrue(o["why"], (cmd, o))


if __name__ == "__main__":
    unittest.main()
