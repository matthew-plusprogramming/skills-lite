"""Black-box tests: run agentloop as a subprocess in a fake HOME and check only its
observable behavior (exit codes, stdout/stderr, files). They pin behavior so the code can
be restructured or reimplemented safely.

Run: python3 -m unittest discover -s tests
Point the suite at another build with AGENTLOOP_UNDER_TEST='["/path/to/agentloop"]'.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CMD = [sys.executable, os.path.join(ROOT, "bin", "agentloop")]
CMD = json.loads(os.environ["AGENTLOOP_UNDER_TEST"]) if os.environ.get("AGENTLOOP_UNDER_TEST") \
    else DEFAULT_CMD

CLAUDE_TREE = [[100, "zsh", ["zsh"]], [50, "claude", ["claude"]]]
CODEX_TREE = [[100, "zsh", ["zsh"]], [70, "codex", ["codex"]]]


# The released single-file agentloop, read from git history for the compatibility tests rather
# than vendored. Shallow clones (CI checkouts) fetch just this commit; without it the tests fail.
V1_COMMIT = "dfffe92a9a4544549037252efa28d833ec505b58"
V1_SHA256 = "dcb340f90a75f3deae8a36c372ffdd80f26bc0cbbab9a9b923b6a2c481b87e65"
_v1 = {}


def v1_script():
    if "path" not in _v1:
        def show():
            return subprocess.run(["git", "-C", ROOT, "show", V1_COMMIT + ":bin/agentloop"],
                                  capture_output=True)
        r = show()
        if r.returncode:
            subprocess.run(["git", "-C", ROOT, "fetch", "-q", "--depth", "1", "origin", V1_COMMIT],
                           capture_output=True)
            r = show()
        if r.returncode or hashlib.sha256(r.stdout).hexdigest() != V1_SHA256:
            raise RuntimeError("released agentloop %s not available from git: %s"
                               % (V1_COMMIT[:7], r.stderr.decode(errors="replace").strip()))
        d = tempfile.mkdtemp(prefix="agentloop-v1-")
        atexit.register(shutil.rmtree, d, True)
        _v1["path"] = os.path.join(d, "agentloop-v1.py")
        with open(_v1["path"], "wb") as f:
            f.write(r.stdout)
    return _v1["path"]


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class BlackBox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.base_env = {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_al(self, *argv, stdin="", env=None, tree=None):
        e = dict(self.base_env)
        e["AGENTLOOP_TEST_PROCTREE"] = json.dumps(tree if tree is not None else [])
        e.update(env or {})
        return subprocess.run(CMD + list(argv), input=stdin, capture_output=True, text=True,
                              env=e, timeout=30)

    # session helpers -------------------------------------------------------
    def claude(self, *argv, sid="s1", **kw):
        env = dict(kw.pop("env", {}), CLAUDE_PID="50", CLAUDE_CODE_SESSION_ID=sid)
        return self.run_al(*argv, env=env, tree=kw.pop("tree", CLAUDE_TREE), **kw)

    def codex(self, *argv, sid="x1", **kw):
        env = dict(kw.pop("env", {}), CODEX_THREAD_ID=sid)
        return self.run_al(*argv, env=env, tree=kw.pop("tree", CODEX_TREE), **kw)

    def stop(self, agent, sid, turn, tree=None, env=None, **extra):
        payload = {"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": False}
        payload["prompt_id" if agent == "claude" else "turn_id"] = turn
        payload.update(extra)
        r = self.run_al("hook", agent, stdin=json.dumps(payload), env=env,
                        tree=tree if tree is not None else (CLAUDE_TREE if agent == "claude" else CODEX_TREE))
        self.assertEqual(r.returncode, 0, r.stderr)
        if not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        self.assertEqual(data["decision"], "block")
        return data["reason"]


class CliTest(BlackBox):
    def test_usage_forms_identical_and_write_nothing(self):
        outs = [self.run_al(*a) for a in ([], ["-h"], ["--help"], ["help"])]
        self.assertTrue(all(o.returncode == 0 for o in outs))
        self.assertEqual(len({o.stdout for o in outs}), 1)
        self.assertTrue(outs[0].stdout.startswith("usage: agentloop"))
        # (Apple's system Python may create ~/Library itself; agentloop must write nothing.)
        self.assertEqual([d for d in os.listdir(self.home) if d != "Library"], [])

    def test_unknown_command(self):
        r = self.run_al("bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown command", r.stderr)

    def test_required_arguments(self):
        self.assertEqual(self.claude("plan").returncode, 2)
        self.assertEqual(self.claude("verified").returncode, 2)

    def test_record_and_status_flow(self):
        r = self.claude("plan", "https://example.com/p")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("recorded plan (claude session)", r.stdout)
        st = self.claude("status").stdout
        self.assertIn("session: claude s1", st)
        self.assertIn("phase:   review", st)
        self.assertIn("plan:    https://example.com/p", st)
        for cmd, phase in (("reviewed", "approval"), ("approved", "work"), ("verified", "done")):
            args = [cmd, "evidence"] if cmd == "verified" else [cmd]
            self.assertEqual(self.claude(*args).returncode, 0)
            self.assertIn("phase:   %s" % phase, self.claude("status").stdout)

    def test_wait_keeps_phase(self):
        self.claude("plan", "x")
        self.claude("approved")
        r = self.claude("wait", "question")
        self.assertIn("phase stays work", r.stdout)
        self.assertIn("(wait pass pending)", self.claude("status").stdout)

    def test_state_file_location_and_shape(self):
        self.claude("plan", "x", env={"XDG_STATE_HOME": os.path.join(self.tmp, "xdg")})
        path = os.path.join(self.tmp, "xdg", "agentloop", "sessions", "claude-s1.json")
        data = json.loads(slurp(path))
        self.assertEqual((data["phase"], data["plan"], data["agent"], data["session"]),
                         ("review", "x", "claude", "s1"))


class SessionDetectionTest(BlackBox):
    def test_claude_session(self):
        self.assertIn("session: claude c1", self.claude("status", sid="c1").stdout)

    def test_codex_session(self):
        self.assertIn("session: codex x9", self.codex("status", sid="x9").stdout)

    def test_codex_nested_in_claude_wins(self):
        tree = [[100, "zsh", []], [70, "codex", ["codex"]], [60, "zsh", []], [50, "claude", ["claude"]]]
        r = self.run_al("status", tree=tree, env={"CLAUDE_PID": "50", "CLAUDE_CODE_SESSION_ID": "c1",
                                                  "CODEX_THREAD_ID": "x1"})
        self.assertIn("session: codex x1", r.stdout)

    def test_claude_nested_in_codex_wins(self):
        tree = [[100, "zsh", []], [50, "claude", ["claude"]], [70, "codex", ["codex"]]]
        r = self.run_al("status", tree=tree, env={"CLAUDE_PID": "50", "CLAUDE_CODE_SESSION_ID": "c2",
                                                  "CODEX_THREAD_ID": "x1"})
        self.assertIn("session: claude c2", r.stdout)

    def test_stale_session_id_without_agent_ancestor_is_refused(self):
        # A leaked CLAUDE_CODE_SESSION_ID in a plain shell must not be trusted.
        r = self.run_al("status", tree=[[100, "sh", []]], env={"CLAUDE_CODE_SESSION_ID": "c1"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("no calling Claude Code or Codex session", r.stderr)

    def test_missing_session_id(self):
        r = self.run_al("status", tree=CLAUDE_TREE, env={"CLAUDE_PID": "50"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("session id is not set", r.stderr)


class HookTest(BlackBox):
    def test_no_state_reminds_once_per_turn(self):
        self.assertIn("no lifecycle step", self.stop("claude", "h1", "t1"))
        self.assertIsNone(self.stop("claude", "h1", "t1"))
        self.assertIsNone(self.stop("claude", "h1", "t2", stop_hook_active=True))
        self.assertIn("no lifecycle step", self.stop("claude", "h1", "t3"))

    def test_review_work_approval_done(self):
        self.claude("plan", "x", sid="h2")
        self.assertIn("agentloop reviewed", self.stop("claude", "h2", "t1"))
        self.claude("reviewed", sid="h2")
        self.assertIsNone(self.stop("claude", "h2", "t2"))
        self.claude("approved", sid="h2")
        self.assertIn("agentloop verified", self.stop("claude", "h2", "t3"))
        self.claude("verified", "ok", sid="h2")
        self.assertIsNone(self.stop("claude", "h2", "t4"))      # finishing stop allowed
        self.assertIn("no lifecycle step", self.stop("claude", "h2", "t5"))  # next task fresh

    def test_wait_yields_once(self):
        self.claude("plan", "x", sid="h3")
        self.claude("wait", sid="h3")
        self.assertIsNone(self.stop("claude", "h3", "t1"))
        self.assertIn("agentloop reviewed", self.stop("claude", "h3", "t2"))

    def test_exempt(self):
        self.claude("exempt", sid="h4")
        self.assertIsNone(self.stop("claude", "h4", "t1"))
        self.assertIn("no lifecycle step", self.stop("claude", "h4", "t2"))

    def test_background_tasks_allow(self):
        self.assertIsNone(self.stop("claude", "h5", "t1", background_tasks=[{"id": "1"}]))

    def test_codex_turn_ids_and_isolation(self):
        self.assertIn("no lifecycle step", self.stop("codex", "c1", "u1"))
        self.codex("plan", "x", sid="c2")
        self.assertIn("agentloop reviewed", self.stop("codex", "c2", "u1"))
        self.assertIn("no lifecycle step", self.stop("claude", "c2", "u1"))

    def test_off_and_headless_never_block(self):
        self.assertIsNone(self.stop("claude", "h6", "t1", env={"AGENTLOOP": "off"}))
        self.assertIsNone(self.stop("claude", "h6", "t2", env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        self.assertIsNone(self.stop("claude", "h6", "t3", env={"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}))
        self.assertIsNone(self.stop("codex", "h7", "u1", tree=[[70, "codex", ["codex", "exec", "hi"]]]))
        self.assertIn("no lifecycle step", self.stop("codex", "h7", "u2",
                                                     tree=[[70, "codex", ["codex", "--yolo"]]]))

    def test_fail_open_on_bad_input(self):
        for bad in ("", "not json", "[]", json.dumps({"hook_event_name": "Stop"})):
            r = self.run_al("hook", "claude", stdin=bad)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), bad)
        log = slurp(os.path.join(self.home, ".local", "state", "agentloop", "log"))
        self.assertIn("error", log)

    def test_normal_turns_log_no_errors(self):
        # The hook fails open, so a crash looks like "allow"; the log is where it shows.
        self.assertIsNotNone(self.stop("claude", "n1", "t0"))
        for i, step in enumerate((["plan", "x"], ["wait"], ["reviewed"], ["approved"],
                                  ["verified", "ok"], ["exempt"])):
            self.assertEqual(self.claude(*step, sid="n1").returncode, 0, step)
            self.stop("claude", "n1", "t%d" % (i + 1))
            self.stop("claude", "n1", "t%d" % (i + 1), stop_hook_active=True)
        self.codex("plan", "x", sid="n2")
        self.stop("codex", "n2", "u1")
        self.stop("claude", "n3", "t1", background_tasks=[{"id": "b"}])
        self.stop("claude", "n4", "t1", env={"AGENTLOOP": "off"})
        recs = [json.loads(line) for line in
                slurp(os.path.join(self.home, ".local", "state", "agentloop", "log")).splitlines()]
        self.assertEqual(len(recs), 16)  # one record per hook call above
        self.assertEqual([r for r in recs if "error" in r], [])

    def test_non_stop_event_and_unknown_agent_ignored(self):
        r = self.run_al("hook", "claude", stdin=json.dumps({"hook_event_name": "SessionStart",
                                                              "session_id": "s", "prompt_id": "p"}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self.run_al("hook", "other", stdin="{}")
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class HotPathTest(BlackBox):
    """Keep the Stop hook lean: these modules cost ~2-5 ms each and the hook never needs them."""
    BANNED = {"subprocess", "shutil", "hashlib", "tempfile", "install"}

    def test_hook_and_record_paths_import_nothing_heavy(self):
        probe = (
            "import io, json, sys\n"
            "sys.path.insert(0, %r)\n"
            "from agentloop import main\n"
            "sys.stdin = io.StringIO(sys.argv[2])\n"
            "sys.stdout = io.StringIO()\n"
            "main(sys.argv[1].split())\n"
            "sys.stderr.write(json.dumps(sorted(sys.modules)))\n" % os.path.join(ROOT, "agentloop"))
        payload = json.dumps({"session_id": "m1", "prompt_id": "p1", "hook_event_name": "Stop"})
        env = dict(self.base_env, CLAUDE_PID="50", CLAUDE_CODE_SESSION_ID="m1",
                   AGENTLOOP_TEST_PROCTREE=json.dumps(CLAUDE_TREE))
        for argv in ("hook claude", "plan x", "status"):
            r = subprocess.run([sys.executable, "-sS", "-c", probe, argv, payload],
                               capture_output=True, text=True, env=env)
            loaded = set(json.loads(r.stderr.strip().splitlines()[-1]))
            self.assertEqual(loaded & self.BANNED, set(), argv)
            if argv == "hook claude":  # Claude's hook needs no process walk, so no ctypes
                self.assertNotIn("ctypes", loaded)


SETTINGS = {"permissions": {"defaultMode": "default"},
            "hooks": {"SessionStart": [{"matcher": "startup",
                                        "hooks": [{"type": "command", "command": "echo start"}]}]}}
CODEX_HOOKS = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}


class InstallTest(BlackBox):
    def write(self, rel, text):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        return slurp(os.path.join(self.home, rel))

    def seed(self):
        self.files = {
            ".claude/settings.json": json.dumps(SETTINGS, indent=2) + "\n",
            ".claude/CLAUDE.md": "My notes\n",
            ".codex/hooks.json": json.dumps(CODEX_HOOKS, indent=2),
            ".codex/config.toml": 'model = "m"\n',
            ".codex/AGENTS.md": "# Notes\n",
        }
        for rel, text in self.files.items():
            self.write(rel, text)

    def test_uninstall_keeps_a_share_dir_it_did_not_create(self):
        foreign = os.path.join(self.home, ".local", "share", "agentloop", "notes.txt")
        os.makedirs(os.path.dirname(foreign))
        with open(foreign, "w") as f:
            f.write("mine\n")
        self.assertEqual(self.install().returncode, 0)
        r = self.run_al("uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(slurp(foreign), "mine\n")
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(foreign), "agentloop.py")))

    def install(self, python="/usr/bin/env-python"):
        return self.run_al("install", "--python", python, "--source", ROOT)

    def test_install_contract_and_uninstall_restores_bytes(self):
        self.seed()
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        binp = os.path.join(self.home, ".local", "bin", "agentloop")
        # Contract: hook commands point at ~/.local/bin/agentloop with a 10 s timeout.
        for rel, agent in ((".claude/settings.json", "claude"), (".codex/hooks.json", "codex")):
            stop = json.loads(self.read(rel))["hooks"]["Stop"]
            self.assertEqual(stop, [{"hooks": [{"type": "command", "timeout": 10,
                                                "command": '"%s" hook %s' % (binp, agent)}]}], rel)
        self.assertEqual(self.read(".codex/config.toml"), 'model = "m"\n')  # Codex hooks default on
        self.assertIn("<!-- agentloop:begin -->", self.read(".codex/AGENTS.md"))
        self.assertIn("@../.codex/AGENTS.md", self.read(".claude/CLAUDE.md"))
        self.assertTrue(os.access(binp, os.X_OK))
        self.assertIn("agentloop", slurp(binp)[:400])
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "skills", "review-plan")))
        manifest = json.loads(self.read(".local/state/agentloop/install.json"))
        self.assertIn(binp, manifest["created"])
        # Re-install changes nothing.
        r2 = self.install()
        self.assertIn("nothing changed", r2.stdout)
        # Simulate Codex trusting the hook, then uninstall.
        hooks_path = os.path.join(self.home, ".codex", "hooks.json")
        self.write(".codex/config.toml", self.read(".codex/config.toml") +
                   '\n[hooks.state."%s:stop:0:0"]\ntrusted_hash = "sha256:11"\n' % hooks_path)
        r3 = self.run_al("uninstall")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        for rel, text in self.files.items():
            self.assertEqual(self.read(rel), text, rel)
        self.assertFalse(os.path.exists(binp))
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".claude", "skills", "review-plan")))

    def test_installed_binary_runs_and_hooks_fire(self):
        r = self.install(python=sys.executable)
        self.assertEqual(r.returncode, 0, r.stderr)
        binp = os.path.join(self.home, ".local", "bin", "agentloop")
        e = dict(self.base_env, AGENTLOOP_TEST_PROCTREE="[]")
        out = subprocess.run([binp, "--help"], capture_output=True, text=True, env=e).stdout
        self.assertTrue(out.startswith("usage: agentloop"))
        cmd = json.loads(self.read(".claude/settings.json"))["hooks"]["Stop"][0]["hooks"][0]["command"]
        payload = json.dumps({"session_id": "i1", "prompt_id": "p1", "hook_event_name": "Stop"})
        res = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True, env=e)
        self.assertEqual(json.loads(res.stdout)["decision"], "block")

    def test_invalid_json_aborts_without_writes(self):
        self.write(".claude/settings.json", "{not json")
        r = self.install()
        self.assertEqual(r.returncode, 2)
        self.assertIn("not valid JSON", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "agentloop")))

    def test_refuses_to_overwrite_foreign_binary(self):
        self.write(".local/bin/agentloop", "#!/bin/sh\necho someone else's tool\n")
        r = self.install()
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not agentloop", r.stderr)

    def run_v1(self, *argv):
        """The released single-file agentloop (main at dfffe92), for compatibility tests."""
        e = dict(self.base_env, AGENTLOOP_TEST_PROCTREE="[]")
        return subprocess.run([sys.executable, v1_script()]
                              + list(argv), capture_output=True, text=True, env=e)

    def lib(self, *parts):
        return os.path.join(self.home, ".local", "share", "agentloop", *parts)

    def test_upgrade_from_v1_then_uninstall_restores_bytes(self):
        self.seed()
        r = self.run_v1("install", "--python", "/usr/bin/env-python", "--source", ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        binp = os.path.join(self.home, ".local", "bin", "agentloop")
        self.assertIn("agentloop launcher", slurp(binp))
        self.assertEqual(len(json.loads(self.read(".claude/settings.json"))["hooks"]["Stop"]), 1)
        r = self.run_al("uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel, text in self.files.items():
            self.assertEqual(self.read(rel), text, rel)
        self.assertFalse(os.path.exists(binp))
        self.assertFalse(os.path.exists(self.lib()))

    def test_v1_uninstall_over_new_install(self):
        self.seed()
        self.assertEqual(self.install().returncode, 0)
        r = self.run_v1("uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel, text in self.files.items():
            self.assertEqual(self.read(rel), text, rel)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "agentloop")))
        leftovers = [os.path.join(d, f) for d, _, fs in os.walk(self.lib()) for f in fs]
        self.assertEqual(leftovers, [])  # the old uninstall leaves only empty directories

    def test_launcher_fails_open_when_modules_missing_or_mismatched(self):
        self.assertEqual(self.install(python=sys.executable).returncode, 0)
        binp = os.path.join(self.home, ".local", "bin", "agentloop")
        e = dict(self.base_env, AGENTLOOP_TEST_PROCTREE="[]")
        payload = json.dumps({"session_id": "f1", "prompt_id": "p1", "hook_event_name": "Stop"})
        runtime = self.lib("agentloop.py")
        text = slurp(runtime)
        shutil.rmtree(self.lib("__pycache__"))
        with open(runtime, "w") as f:
            f.write(text.replace('VERSION = "', 'VERSION = "old-'))
        for state in ("version mismatch", "module missing"):
            if state == "module missing":
                os.unlink(runtime)
            h = subprocess.run([binp, "hook", "claude"], input=payload, capture_output=True, text=True, env=e)
            self.assertEqual((h.returncode, h.stdout), (0, ""))
            st = subprocess.run([binp, "status"], capture_output=True, text=True, env=e)
            self.assertEqual(st.returncode, 2)
            self.assertIn("installation is incomplete", st.stderr, state)
        self.assertIn("launcher:", self.read(".local/state/agentloop/log"))

    def test_install_sh_round_trip(self):
        e = dict(self.base_env, AGENTLOOP_TEST_PROCTREE="[]")
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh")], capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--uninstall"],
                           capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "agentloop")))


class StatusTest(BlackBox):
    """`agentloop status` reports each hook problem a user can fix, and says ok otherwise."""
    def write(self, rel, text, mode="w"):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode, encoding="utf-8") as f:
            f.write(text)

    def hooks(self):
        r = self.claude("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        return dict(re.findall(r"^hook (\w+):\s+(.*)$", r.stdout, re.M))

    def test_each_warning_and_ok(self):
        log = os.path.join(self.home, ".local", "state", "agentloop", "log")
        h = self.hooks()
        for agent in ("claude", "codex"):
            self.assertIn("Stop hook not configured (run install.sh)", h[agent])
            self.assertIn("binary missing at " + os.path.join(self.home, ".local", "bin", "agentloop"), h[agent])
        r = self.run_al("install", "--python", "/usr/bin/env-python", "--source", ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        h = self.hooks()
        self.assertEqual(h["claude"], "ok")
        self.assertEqual(h["codex"], "hook not trusted yet: open codex and choose 'Trust all and continue'")
        # Codex's trust record for the first Stop group (Codex's own key format).
        self.write(".codex/config.toml", '[hooks.state."%s:stop:0:0"]\ntrusted_hash = "sha256:11"\n'
                   % os.path.join(self.home, ".codex", "hooks.json"), "a")
        self.assertEqual(self.hooks(), {"claude": "ok", "codex": "ok"})
        trusted = slurp(os.path.join(self.home, ".codex", "config.toml"))
        for off in ("[features]\nhooks = false\n", "features.codex_hooks = false\n"):
            self.write(".codex/config.toml", off + trusted)  # top-level keys go before tables
            self.assertEqual(self.hooks()["codex"], "hooks are turned off in config.toml [features]; "
                             "remove that setting to let the Stop hook run", off)
        self.write(".codex/config.toml", "[features]\nhooks = true\n" + trusted)
        self.assertEqual(self.hooks()["codex"], "ok")
        self.run_al("hook", "claude", stdin="[]")  # a hook crash is logged, not shown
        self.assertEqual(self.hooks(), {a: "1 hook error(s) in the last day (see %s)" % log
                                        for a in ("claude", "codex")})


if __name__ == "__main__":
    unittest.main()
