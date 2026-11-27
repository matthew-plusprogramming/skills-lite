"""Tests for bin/agentloop. Run: python3 -m unittest discover -s tests"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


def slurp(path):
    with open(path) as f:
        return f.read()

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "agentloop")
sys.path.insert(0, os.path.join(ROOT, "agentloop"))
import agentloop as al
import install as ins


def tree_digest(root):
    h = hashlib.sha256()
    for d, dirs, files in sorted(os.walk(root)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(p.encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            else:
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "CLAUDE_CODE_ENTRYPOINT": "cli"}
        # No real process tree in unit tests: interactive, no agent ancestors.
        self._orig = al.nearest_agent
        al.nearest_agent = lambda env, start=None, want_args=False: None

    def tearDown(self):
        al.nearest_agent = self._orig
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv, session=("claude", "s1")):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = al.main(list(argv), env=self.env, session=session)
        return code, out.getvalue(), err.getvalue()

    def stop(self, turn, agent="claude", sid="s1", **extra):
        payload = {"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": False}
        payload["prompt_id" if agent == "claude" else "turn_id"] = turn
        payload.update(extra)
        out = al.cmd_hook(self.env, agent, json.dumps(payload))
        return json.loads(out)["reason"] if out else None


class UsageTest(Base):
    def test_bare_h_help_identical_and_no_writes(self):
        before = tree_digest(self.home)
        outs = [self.run_cli(*a)[1] for a in ([], ["-h"], ["--help"])]
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[1], outs[2])
        for cmd in ("plan <link>", "reviewed", "approved", "verified", "wait", "exempt", "status"):
            self.assertIn(cmd, outs[0])
        self.assertNotIn("trivial", outs[0])
        self.assertEqual(before, tree_digest(self.home))

    def test_usage_via_subprocess(self):
        r = [subprocess.run([sys.executable, SCRIPT] + a, capture_output=True, text=True,
                            env=dict(self.env)).stdout for a in ([], ["-h"], ["--help"])]
        self.assertTrue(r[0].startswith("usage: agentloop"))
        self.assertEqual(len(set(r)), 1)

    def test_required_args(self):
        self.assertEqual(self.run_cli("plan")[0], 2)
        self.assertEqual(self.run_cli("verified")[0], 2)


class SessionLookupTest(unittest.TestCase):
    def setUp(self):
        self._orig = al.ancestors

    def tearDown(self):
        al.ancestors = self._orig

    def with_tree(self, chain):
        al.ancestors = lambda start, want_args=False: iter(chain)

    def test_claude(self):
        self.with_tree([(10, "zsh", ["zsh"]), (5, "/opt/x/claude", ["claude"])])
        env = {"CLAUDE_PID": "5", "CLAUDE_CODE_SESSION_ID": "c1"}
        self.assertEqual(al.current_session(env), ("claude", "c1"))

    def test_codex_full_path_comm(self):
        self.with_tree([(10, "/bin/zsh", ["zsh"]), (7, "/opt/vendor/bin/codex", ["codex"])])
        self.assertEqual(al.current_session({"CODEX_THREAD_ID": "x1"}), ("codex", "x1"))

    def test_codex_nested_in_claude_wins(self):
        self.with_tree([(10, "zsh", []), (7, "codex", ["codex", "exec"]), (6, "zsh", []),
                        (5, "claude", ["claude"])])
        env = {"CLAUDE_PID": "5", "CLAUDE_CODE_SESSION_ID": "c1", "CODEX_THREAD_ID": "x1"}
        self.assertEqual(al.current_session(env), ("codex", "x1"))

    def test_claude_nested_in_codex_wins(self):
        self.with_tree([(10, "zsh", []), (9, "claude", ["claude"]), (7, "codex", ["codex"])])
        env = {"CLAUDE_PID": "9", "CLAUDE_CODE_SESSION_ID": "c2", "CODEX_THREAD_ID": "x1"}
        self.assertEqual(al.current_session(env), ("claude", "c2"))

    def test_real_tree_finds_this_process(self):
        al.ancestors = self._orig
        chain = list(al.ancestors(os.getpid()))
        self.assertEqual(chain[0][0], os.getpid())
        self.assertTrue(all(isinstance(p, int) and n for p, n, _ in chain))

    def test_no_agent(self):
        self.with_tree([(10, "sh", [])])
        with self.assertRaises(al.UserError):
            al.current_session({"CLAUDE_CODE_SESSION_ID": "c1"})

    def test_headless_detection(self):
        self.with_tree([(7, "codex", ["codex", "--yolo", "exec", "hi"])])
        self.assertTrue(al.is_headless("codex", {}))
        self.with_tree([(7, "codex", ["codex", "--yolo"])])
        self.assertFalse(al.is_headless("codex", {}))
        self.with_tree([(7, "codex", ["codex", "review this exec plan"])])
        self.assertFalse(al.is_headless("codex", {}))
        self.with_tree([(7, "codex", ["codex", "-c", "model=x", "e", "hi"])])
        self.assertTrue(al.is_headless("codex", {}))
        self.with_tree([(7, "codex", ["codex", "-m", "exec"])])
        self.assertFalse(al.is_headless("codex", {}))
        # Claude: the entrypoint decides; `claude -p` sets sdk-cli, so argv is not consulted.
        self.with_tree([(5, "claude", ["claude", "-p", "x"])])
        self.assertFalse(al.is_headless("claude", {"CLAUDE_PID": "5", "CLAUDE_CODE_ENTRYPOINT": "cli"}))
        self.with_tree([(5, "claude", ["claude"])])
        self.assertTrue(al.is_headless("claude", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        self.assertFalse(al.is_headless("claude", {"CLAUDE_PID": "5", "CLAUDE_CODE_ENTRYPOINT": "cli"}))
        self.assertFalse(al.is_headless("claude", {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}))


class HookTest(Base):
    def test_missing_state_reminds_once_per_turn(self):
        self.assertIn("no lifecycle step", self.stop("t1"))
        self.assertIsNone(self.stop("t1"))                       # replay
        self.assertIsNone(self.stop("t1", stop_hook_active=True))  # retry
        self.assertIn("no lifecycle step", self.stop("t2"))      # next distinct turn

    def test_review_and_work_remind(self):
        self.run_cli("plan", "https://example.com/plan")
        self.assertIn("agentloop reviewed", self.stop("t1"))
        self.assertIsNone(self.stop("t1", stop_hook_active=True))
        self.run_cli("approved")
        self.assertIn("agentloop verified", self.stop("t2"))

    def test_approval_always_yields_and_keeps_state(self):
        self.run_cli("plan", "x")
        self.run_cli("reviewed")
        for t in ("a1", "a2", "a3"):
            self.assertIsNone(self.stop(t))
            self.assertIsNone(self.stop(t))
        self.assertIn("phase:   approval", self.run_cli("status")[1])

    def test_wait_pass_and_replay(self):
        self.run_cli("plan", "x")
        self.run_cli("approved")
        self.run_cli("wait", "question for the human")
        self.assertIsNone(self.stop("w1"))
        self.assertIsNone(self.stop("w1"))  # replay after wait must not re-remind
        self.assertIn("phase:   work", self.run_cli("status")[1])
        self.assertIn("agentloop verified", self.stop("w2"))

    def test_background_tasks_allow(self):
        self.run_cli("plan", "x")
        self.assertIsNone(self.stop("b1", background_tasks=[{"id": "1"}]))

    def test_sequence_a_verified_resets(self):
        for c in (["plan", "x"], ["reviewed"], ["approved"], ["verified", "tests pass"]):
            self.run_cli(*c)
        self.assertIsNone(self.stop("t1"))
        self.assertIsNone(self.stop("t1"))
        self.assertIn("no lifecycle step", self.stop("t2"))
        self.assertIsNone(self.stop("t2", stop_hook_active=True))

    def test_sequence_a_prime_reminded_then_verified(self):
        self.run_cli("plan", "x")
        self.run_cli("approved")
        self.assertIn("agentloop verified", self.stop("t1"))
        self.run_cli("verified", "evidence")
        self.assertIsNone(self.stop("t1", stop_hook_active=True))
        self.assertIsNone(self.stop("t1"))
        self.assertIn("no lifecycle step", self.stop("t2"))

    def test_sequence_b_exempt_resets(self):
        self.assertIn("no lifecycle step", self.stop("t1"))
        self.run_cli("exempt", "one-off")
        self.assertIsNone(self.stop("t1", stop_hook_active=True))
        self.assertIn("no lifecycle step", self.stop("t2"))

    def test_exempt_before_first_stop(self):
        self.run_cli("exempt")
        self.assertIsNone(self.stop("t1"))
        self.assertIn("no lifecycle step", self.stop("t2"))

    def test_replay_every_state_at_most_one_reminder(self):
        setups = {
            "none": [], "review": [["plan", "x"]], "approval": [["plan", "x"], ["reviewed"]],
            "work": [["plan", "x"], ["approved"]], "wait": [["plan", "x"], ["approved"], ["wait"]],
            "done": [["verified", "e"]], "exempt": [["exempt"]],
        }
        for i, (name, cmds) in enumerate(setups.items()):
            sid = "r%d" % i
            for c in cmds:
                self.run_cli(*c, session=("claude", sid))
            results = [self.stop("same", sid=sid) for _ in range(3)]
            self.assertLessEqual(sum(1 for r in results if r), 1, name)
            self.assertTrue(all(r is None for r in results[1:]), name)

    def test_codex_turn_id_and_sessions_isolated(self):
        self.assertIn("no lifecycle step", self.stop("u1", agent="codex", sid="x1"))
        self.assertIn("no lifecycle step", self.stop("u1", agent="codex", sid="x2"))
        self.run_cli("plan", "x", session=("codex", "x3"))
        self.assertIn("agentloop reviewed", self.stop("u1", agent="codex", sid="x3"))
        self.assertIn("no lifecycle step", self.stop("u1", agent="claude", sid="x3"))

    def test_fail_open(self):
        for bad in ("", "not json", "[]", json.dumps({"hook_event_name": "Stop"})):
            self.assertEqual(al.cmd_hook(self.env, "claude", bad), "")
        log = slurp(os.path.join(al.state_dir(self.env), "log"))
        self.assertIn("error", log)

    def test_missing_turn_id_allows_and_logs(self):
        self.assertEqual(al.cmd_hook(self.env, "claude", json.dumps({"session_id": "s9"})), "")
        self.assertIn("no prompt_id/turn_id", slurp(os.path.join(al.state_dir(self.env), "log")))

    def test_off_and_headless(self):
        self.env["AGENTLOOP"] = "off"
        self.assertIsNone(self.stop("t1"))
        del self.env["AGENTLOOP"]
        self.env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
        self.run_cli("plan", "x")
        self.assertIsNone(self.stop("t1"))

    def test_block_output_shape(self):
        out = al.cmd_hook(self.env, "codex", json.dumps(
            {"session_id": "z", "turn_id": "t", "hook_event_name": "Stop"}))
        data = json.loads(out)
        self.assertEqual(data["decision"], "block")
        self.assertTrue(data["reason"])


SETTINGS = {
    "permissions": {"defaultMode": "default"},
    "hooks": {
        "PermissionRequest": [{"matcher": "", "hooks": [{"type": "command", "command": "echo allow"}]}],
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo start"}]}],
    },
}
CODEX_HOOKS = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
CONFIG_TOML = 'model = "m"\n\n[features]\nhooks = true\n\n[hooks.state]\n\n' \
              '[hooks.state."HOOKS:session_start:0:0"]\ntrusted_hash = "sha256:00"\n'


class InstallTest(Base):
    def write(self, rel, text):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def read(self, rel):
        with open(os.path.join(self.home, rel)) as f:
            return f.read()

    def seed(self):
        hooks_path = os.path.join(self.home, ".codex", "hooks.json")
        self.files = {
            ".claude/settings.json": json.dumps(SETTINGS, indent=2) + "\n",
            ".claude/CLAUDE.md": "@../.codex/AGENTS.md\n",
            ".codex/hooks.json": json.dumps(CODEX_HOOKS, indent=2),
            ".codex/config.toml": CONFIG_TOML.replace("HOOKS", hooks_path),
            ".codex/AGENTS.md": "# Notes\n\nSome rules.\n",
            ".agents/skills/review-plan/SKILL.md": slurp(
                os.path.join(ROOT, "skills", "review-plan", "SKILL.md")),
        }
        for rel, text in self.files.items():
            self.write(rel, text)
        os.makedirs(os.path.join(self.home, ".claude", "skills", "real-folder"))
        os.symlink(os.path.join(self.home, ".agents", "skills", "review-plan"),
                   os.path.join(self.home, ".claude", "skills", "review-plan"))

    def install(self):
        return self.run_cli("install", "--python", "/usr/bin/env-python", "--source", ROOT)

    def snapshot(self):
        return {rel: self.read(rel) for rel in self.files}

    def test_install_twice_then_uninstall_restores_bytes(self):
        self.seed()
        before = self.snapshot()
        binp = os.path.join(self.home, ".local", "bin", "agentloop")
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        after1 = self.snapshot()
        s = json.loads(after1[".claude/settings.json"])
        self.assertEqual(s["hooks"]["PermissionRequest"], SETTINGS["hooks"]["PermissionRequest"])
        self.assertEqual(s["hooks"]["SessionStart"], SETTINGS["hooks"]["SessionStart"])
        self.assertEqual(s["hooks"]["Stop"], [{"hooks": [
            {"type": "command", "command": '"%s" hook claude' % binp, "timeout": 10}]}])
        c = json.loads(after1[".codex/hooks.json"])
        self.assertEqual(c["hooks"]["SessionStart"], CODEX_HOOKS["hooks"]["SessionStart"])
        self.assertEqual(c["hooks"]["Stop"], [{"hooks": [
            {"type": "command", "command": '"%s" hook codex' % binp, "timeout": 10}]}])
        self.assertEqual(after1[".codex/config.toml"], before[".codex/config.toml"])  # sandbox untouched
        self.assertIn("Use `agentloop` to record lifecycle steps", after1[".codex/AGENTS.md"])
        self.assertTrue(after1[".codex/AGENTS.md"].startswith(before[".codex/AGENTS.md"]))
        self.assertEqual(after1[".claude/CLAUDE.md"], before[".claude/CLAUDE.md"])  # imports AGENTS.md
        self.assertEqual(after1[".agents/skills/review-plan/SKILL.md"],
                         before[".agents/skills/review-plan/SKILL.md"])
        self.assertTrue(slurp(binp).startswith("#!/usr/bin/env-python -sS\n"))
        self.assertTrue(os.access(binp, os.X_OK))
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', out)

        _, out2, _ = self.install()
        self.assertEqual(len(json.loads(self.read(".claude/settings.json"))["hooks"]["Stop"]), 1)
        self.assertEqual(after1, self.snapshot())
        self.assertIn("nothing changed", out2)

        # Simulate Codex writing its trust record for the new hook (Codex's key format).
        toml_rel = ".codex/config.toml"
        self.assertIn("hook not trusted yet", " ".join(al.health(self.env)["codex"]))
        self.write(toml_rel, self.read(toml_rel) + '\n[hooks.state."%s:stop:0:0"]\n'
                   % os.path.join(self.home, ".codex", "hooks.json") + 'trusted_hash = "sha256:11"\n')
        self.assertEqual(al.health(self.env)["codex"], [])

        code, _, err = self.run_cli("uninstall")
        self.assertEqual(code, 0, err)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(os.path.exists(binp))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude", "skills", "real-folder")))
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "skills", "review-plan")))

    def test_fresh_home_creates_and_uninstall_removes(self):
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".codex", "config.toml")))  # hooks default on
        self.assertIn(ins.MARK_BEGIN, self.read(".claude/CLAUDE.md"))
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "skills", "review-plan")))
        self.run_cli("uninstall")
        for rel in (".claude/settings.json", ".codex/hooks.json", ".codex/AGENTS.md",
                    ".claude/CLAUDE.md", ".agents/skills/review-plan/SKILL.md"):
            self.assertFalse(os.path.exists(os.path.join(self.home, rel)), rel)
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".claude", "skills", "review-plan")))

    def test_upgrade_old_guidance_preserves_notes_and_imports_full_loop(self):
        old = ins.MARK_BEGIN + "\n## Lifecycle\nOld reminder\n" + ins.MARK_END + "\n"
        for path in (".codex/AGENTS.md", ".claude/CLAUDE.md"):
            self.write(path, "Personal notes\n\n" + old + "More notes\n")
        for _ in range(2):
            code, _, err = self.install()
            self.assertEqual(code, 0, err)
            agents = self.read(".codex/AGENTS.md")
            claude = self.read(".claude/CLAUDE.md")
            for content in (agents, claude):
                self.assertTrue(content.startswith("Personal notes\n\n"))
                self.assertTrue(content.endswith("More notes\n"))
                self.assertEqual(content.count(ins.MARK_BEGIN), 1)
                self.assertNotIn("Old reminder", content)
            self.assertIn("wait for my explicit approval", agents)
            self.assertIn("Have a separate verifier", agents)
            self.assertIn("@../.codex/AGENTS.md\n", claude)

    def test_invalid_json_aborts_without_writes(self):
        self.write(".claude/settings.json", "{not json")
        before = tree_digest(self.home)
        code, _, err = self.install()
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        self.assertEqual(before, tree_digest(self.home))

    def test_differing_same_name_skill_is_left_alone(self):
        self.write(".agents/skills/review-plan/SKILL.md", "local edit\n")
        _, out, _ = self.install()
        d = os.path.join(self.home, ".agents", "skills", "review-plan")
        self.assertEqual(slurp(os.path.join(d, "SKILL.md")), "local edit\n")
        self.assertEqual(os.listdir(d), ["SKILL.md"])  # no backup needed: nothing was replaced
        self.assertIn("left %s alone" % d, out)

    def test_existing_claude_skill_entry_untouched(self):
        os.makedirs(os.path.join(self.home, ".claude", "skills", "review-plan"))
        _, out, _ = self.install()
        self.assertFalse(os.path.islink(os.path.join(self.home, ".claude", "skills", "review-plan")))
        self.assertIn("left existing", out)

    def test_unwritable_state_gives_clear_error(self):
        os.makedirs(os.path.join(self.home, ".local"))
        with open(os.path.join(self.home, ".local", "state"), "w") as f:
            f.write("not a directory")
        code, _, err = self.run_cli("exempt")
        self.assertEqual(code, 2)
        self.assertIn("cannot write lifecycle state", err)

    def test_real_argv_keeps_spaces(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", "two words"])
        try:
            info = al.proc_info(p.pid, want_args=True)
            self.assertEqual(info[2][-1], "two words")
        finally:
            p.kill()
            p.wait()

    def test_edited_created_skill_kept_on_uninstall(self):
        self.install()
        f = os.path.join(self.home, ".agents", "skills", "review-plan", "SKILL.md")
        with open(f, "a") as fh:
            fh.write("my edit\n")
        _, out, _ = self.run_cli("uninstall")
        self.assertTrue(os.path.exists(f))
        self.assertIn("kept edited", out)

    def test_invalid_toml_aborts_when_parser_available(self):
        try:
            import tomllib  # noqa: F401
        except ImportError:
            self.skipTest("tomllib needs Python 3.11+")
        self.write(".codex/config.toml", "[features\nhooks = true\n")
        before = tree_digest(self.home)
        code, _, err = self.install()
        self.assertEqual(code, 2)
        self.assertIn("not valid TOML", err)
        self.assertEqual(before, tree_digest(self.home))

    def test_legacy_features_flag_restored(self):
        # What earlier installers wrote, and the "before" they recorded in the manifest.
        cases = (("", "[features]\nhooks = true\n", "no-table"),
                 ('model = "m"\n', 'model = "m"\n\n[features]\nhooks = true\n', "no-table"),
                 ("[features]\nother = 1\n", "[features]\nhooks = true\nother = 1\n", "no-key"),
                 ("[features]\nhooks = false\n", "[features]\nhooks = true\n", "hooks = false"))
        for before, written, recorded in cases:
            restored = ins.toml_restore_features(written, recorded)
            self.assertEqual(restored, before)

    # Each spelling with the state Codex 0.171 reports for it (`codex features list`).
    SPELLINGS = (("", None), ('model = "m"\n', None), ("[features]\nhooks = true\n", True),
                 ("[features]\nhooks = false\n", False), ("[features]\nhooks = false # off\n", False),
                 ("[features]\n# hooks = false\n", None), ("features.hooks = false\n", False),
                 ('"features".hooks = false\n', False), ("features = { hooks = false }\n", False),
                 ("[features]\ncodex_hooks = false\n", False), ("features.codex_hooks = false\n", False),
                 ("features = { codex_hooks = false }\n", False),
                 ("[features]\nhooks = true\ncodex_hooks = false\n", True),
                 ("[features]\ncodex_hooks = true\nhooks = false\n", False),
                 ("[profiles.p.features]\nhooks = false\n", None),
                 ("[features]\nhooks\t= false\n", False), ("features\t.\thooks = false\n", False),
                 ('note = """\n[features]\nhooks = false\n"""\n', None),
                 ("note = '''\nfeatures.hooks = false\n'''\n[features]\nhooks = true\n", True),
                 # quote runs that are not multi-line strings must not hide what follows
                 ('# example: x = """\n[features]\nhooks = false\n', False),
                 ('model = "m" # """\nfeatures.hooks = false\n', False),
                 ("x = 'has \"\"\" inside'\nfeatures.hooks = false\n", False),
                 ("x = '''a \"\"\" b'''\nfeatures.hooks = false\n", False),
                 ('x = """a""" # """\nfeatures.hooks = false\n', False),
                 ('x = """a \\""" b"""\nfeatures.hooks = false\n', False))

    def test_features_hooks_spellings(self):
        for text, want in self.SPELLINGS:
            self.assertIs(al.toml_features_hooks(text), want, text)
            got = al._toml_features_lines(text)  # the parser-free path used before Python 3.11
            self.assertIs(got.get("hooks", got.get("codex_hooks")), want, text)

    def test_health_warns_only_when_hooks_explicitly_off(self):
        self.install()
        self.assertNotIn("config.toml", " ".join(al.health(self.env)["codex"]))
        for text in ("[features]\nhooks = false\n", "features.codex_hooks = false\n"):
            self.write(".codex/config.toml", text)
            self.assertIn("turned off in config.toml", " ".join(al.health(self.env)["codex"]), text)

    def test_install_sh_runs(self):
        env = dict(self.env, PATH=os.environ.get("PATH", "/usr/bin:/bin"))
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([os.path.join(self.home, ".local", "bin", "agentloop"), "--help"],
                           capture_output=True, text=True, env=env)
        self.assertTrue(r.stdout.startswith("usage: agentloop"))
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--uninstall"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
