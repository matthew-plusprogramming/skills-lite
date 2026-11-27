"""Install and uninstall must never lose the user's files.

Black-box: run agentloop in a fake HOME, then compare the user's files byte for byte.
Each test names the loss it guards against.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from test_blackbox import v1_script

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = [sys.executable, os.path.join(ROOT, "bin", "agentloop")]
SKILL_DIR = ".agents/skills/review-plan"
SKILL = SKILL_DIR + "/SKILL.md"
LINK = ".claude/skills/review-plan"
OTHER_HOOK = {"type": "command", "command": "echo other", "timeout": 5}


def bundled_skill():
    with open(os.path.join(ROOT, "skills", "review-plan", "SKILL.md")) as f:
        return f.read()


class Preservation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.dots = os.path.join(self.tmp, "dotfiles")  # where a user keeps the real files
        os.makedirs(self.home)
        os.makedirs(self.dots)
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "AGENTLOOP_TEST_PROCTREE": "[]"}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def p(self, rel):
        return os.path.join(self.home, rel)

    def read(self, path):
        with open(path if os.path.isabs(path) else self.p(path), "rb") as f:
            return f.read()

    def write(self, path, data, mode=None):
        path = path if os.path.isabs(path) else self.p(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
        if mode is not None:
            os.chmod(path, mode)

    def al(self, *argv, cmd=None, source=ROOT):
        if argv[0] == "install":
            argv = argv + ("--python", sys.executable, "--source", source)
        r = subprocess.run((cmd or CMD) + list(argv), capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def source(self, files=None):
        """A copy of the repository whose bundled skill can be changed, as a new release would."""
        src = os.path.join(self.tmp, "src")
        if not os.path.exists(src):
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        for rel, text in (files or {}).items():
            self.write(os.path.join(src, "skills", "review-plan", rel), text)
        return src

    def mode(self, rel):
        return stat.S_IMODE(os.stat(self.p(rel)).st_mode)

    # Shared config files

    def test_untouched_user_files_come_back_byte_for_byte(self):
        files = {".claude/settings.json": '{"model":"x","hooks":{"Stop":[{"hooks":[%s]}]}}' % json.dumps(OTHER_HOOK),
                 ".codex/hooks.json": '{"hooks": {"SessionStart": []}}\n',
                 ".codex/config.toml": 'model = "o"\r\n[features]\r\nhooks = false\r\n',
                 ".codex/AGENTS.md": "# Mine\n\nKeep this.",
                 ".claude/CLAUDE.md": "# Claude notes\n"}
        for rel, text in files.items():
            self.write(rel, text, 0o600)
        self.al("install")
        self.al("uninstall")
        for rel, text in files.items():
            self.assertEqual(self.read(rel), text.encode(), rel)
            self.assertEqual(self.mode(rel), 0o600, rel)

    def test_foreign_hooks_survive_install_and_uninstall(self):
        self.write(".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [OTHER_HOOK]}]}}))
        self.write(".codex/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [OTHER_HOOK]}]}}))
        self.al("install")
        for rel in (".claude/settings.json", ".codex/hooks.json"):
            stop = json.loads(self.read(rel))["hooks"]["Stop"]
            self.assertIn({"hooks": [OTHER_HOOK]}, stop, rel)
            self.assertEqual(len(stop), 2, rel)
        self.al("uninstall")
        for rel in (".claude/settings.json", ".codex/hooks.json"):
            self.assertEqual(json.loads(self.read(rel)), {"hooks": {"Stop": [{"hooks": [OTHER_HOOK]}]}}, rel)

    def test_settings_changed_after_install_are_kept_on_uninstall(self):
        self.write(".claude/settings.json", '{"model": "x"}\n')
        self.al("install")
        s = json.loads(self.read(".claude/settings.json"))
        s["theme"] = "dark"
        s["hooks"]["Stop"].append({"hooks": [OTHER_HOOK]})
        self.write(".claude/settings.json", json.dumps(s))
        with open(self.p(".codex/config.toml"), "a") as f:
            f.write('approval_policy = "never"\n')
        self.al("uninstall")
        self.assertEqual(json.loads(self.read(".claude/settings.json")),
                         {"model": "x", "theme": "dark", "hooks": {"Stop": [{"hooks": [OTHER_HOOK]}]}})
        self.assertIn(b'approval_policy = "never"', self.read(".codex/config.toml"))

    def test_modes(self):
        self.al("install")
        self.assertEqual(self.mode(SKILL), 0o644)
        self.assertEqual(self.mode(".local/bin/agentloop"), 0o755)

    # Skills

    def test_same_name_user_skill_is_never_replaced(self):
        self.write(SKILL, "# my own review-plan\n")
        self.al("install")
        self.al("install")
        self.assertEqual(self.read(SKILL), b"# my own review-plan\n")
        self.assertEqual(os.listdir(self.p(SKILL_DIR)), ["SKILL.md"])
        self.al("uninstall")
        self.assertEqual(self.read(SKILL), b"# my own review-plan\n")

    def test_symlinked_skill_folder_is_never_written_through(self):
        mine = os.path.join(self.dots, "review-plan")
        self.write(os.path.join(mine, "SKILL.md"), "# mine in dotfiles\n")
        os.makedirs(self.p(".agents/skills"))
        os.symlink(mine, self.p(SKILL_DIR))
        self.al("install")
        self.al("uninstall")
        self.assertEqual(os.listdir(mine), ["SKILL.md"])
        self.assertEqual(self.read(os.path.join(mine, "SKILL.md")), b"# mine in dotfiles\n")
        self.assertTrue(os.path.islink(self.p(SKILL_DIR)))

    def test_symlinked_skills_root_is_never_written_through(self):
        root = os.path.join(self.dots, "skills")
        os.makedirs(root)
        os.makedirs(self.p(".agents"))
        os.symlink(root, self.p(".agents/skills"))
        self.al("install")
        self.al("uninstall")
        self.assertEqual(os.listdir(root), [])

    def test_skill_moved_to_dotfiles_after_install_is_not_deleted_through_the_link(self):
        self.al("install")
        mine = os.path.join(self.dots, "review-plan")
        shutil.move(self.p(SKILL_DIR), mine)  # unchanged content, now owned by the user's dotfiles
        os.symlink(mine, self.p(SKILL_DIR))
        self.al("uninstall")
        self.assertEqual(self.read(os.path.join(mine, "SKILL.md")), bundled_skill().encode())

    def test_reinstall_never_writes_through_a_skill_folder_moved_to_dotfiles(self):
        src = self.source()
        self.al("install", source=src)
        mine = os.path.join(self.dots, "review-plan")
        shutil.move(self.p(SKILL_DIR), mine)
        os.symlink(mine, self.p(SKILL_DIR))
        self.write(os.path.join(mine, "SKILL.md"), "# my edit\n")
        self.source({"SKILL.md": "# bundle v2\n"})
        self.al("install", source=src)
        self.assertEqual(sorted(os.listdir(mine)), ["SKILL.md"])  # no backup written there either
        self.assertEqual(self.read(os.path.join(mine, "SKILL.md")), b"# my edit\n")
        self.al("uninstall")
        self.assertEqual(self.read(os.path.join(mine, "SKILL.md")), b"# my edit\n")

    def test_symlinked_subfolder_inside_a_skill_is_never_written_through(self):
        src = self.source({"ref/a.md": "bundle a\n"})
        self.al("install", source=src)
        ref = os.path.join(self.dots, "ref")
        shutil.move(self.p(SKILL_DIR + "/ref"), ref)
        os.symlink(ref, self.p(SKILL_DIR + "/ref"))
        self.write(os.path.join(ref, "a.md"), "my a\n")
        self.source({"ref/a.md": "bundle a v2\n", "ref/b.md": "bundle b\n"})
        self.al("install", source=src)
        self.assertEqual(sorted(os.listdir(ref)), ["a.md"])  # nothing new created through the link
        self.assertEqual(self.read(os.path.join(ref, "a.md")), b"my a\n")
        self.al("uninstall")
        self.assertEqual(self.read(os.path.join(ref, "a.md")), b"my a\n")

    def test_symlinked_dot_agents_is_never_written_through(self):
        agents = os.path.join(self.dots, "agents")
        os.makedirs(os.path.join(agents, "skills"))
        os.symlink(agents, self.p(".agents"))
        self.al("install")
        self.al("uninstall")
        self.assertEqual(os.listdir(os.path.join(agents, "skills")), [])

    def test_symlinked_claude_skills_folder_outside_home_gets_no_link(self):
        cfg = os.path.join(self.tmp, "claude-config")
        os.makedirs(cfg)
        os.makedirs(os.path.join(self.dots, "claude-skills"))
        os.symlink(os.path.join(self.dots, "claude-skills"), os.path.join(cfg, "skills"))
        self.env["CLAUDE_CONFIG_DIR"] = cfg
        self.al("install")
        self.assertEqual(os.listdir(os.path.join(self.dots, "claude-skills")), [])
        self.al("uninstall")
        self.assertEqual(os.listdir(os.path.join(self.dots, "claude-skills")), [])

    def test_skill_file_replaced_by_a_symlink_is_never_written_through(self):
        self.al("install")
        dot = os.path.join(self.dots, "SKILL.md")
        self.write(dot, "# my notes\n")
        os.unlink(self.p(SKILL))
        os.symlink(dot, self.p(SKILL))
        self.al("install")
        self.assertEqual(self.read(dot), b"# my notes\n")
        self.al("uninstall")
        self.assertEqual(self.read(dot), b"# my notes\n")
        self.assertTrue(os.path.islink(self.p(SKILL)))

    def test_repointed_claude_skill_link_is_kept(self):
        self.al("install")
        mine = os.path.join(self.dots, "other-review")
        os.makedirs(mine)
        os.unlink(self.p(LINK))
        os.symlink(mine, self.p(LINK))
        self.al("uninstall")
        self.assertEqual(os.readlink(self.p(LINK)), mine)

    def test_edited_skill_is_kept_on_uninstall(self):
        self.al("install")
        self.write(SKILL, "my edit\n")
        self.al("uninstall")
        self.assertEqual(self.read(SKILL), b"my edit\n")

    def test_dropped_skill_file_removed_only_if_unchanged(self):
        src = self.source({"old.md": "bundled old\n", "edited.md": "bundled edited\n"})
        self.al("install", source=src)
        self.write(SKILL_DIR + "/edited.md", "my change\n")
        for name in ("old.md", "edited.md"):
            os.unlink(os.path.join(src, "skills", "review-plan", name))
        self.al("install", source=src)
        self.assertFalse(os.path.exists(self.p(SKILL_DIR + "/old.md")))
        self.assertEqual(self.read(SKILL_DIR + "/edited.md"), b"my change\n")

    def test_identical_existing_skill_is_synced_but_kept_on_uninstall(self):
        # e.g. installed by an earlier version that did not record it, or copied by hand
        self.write(SKILL, bundled_skill())
        os.makedirs(self.p(".claude/skills"))
        os.symlink(self.p(SKILL_DIR), self.p(LINK))
        src = self.source()
        self.al("install", source=src)
        self.source({"SKILL.md": "# bundle v2\n"})
        self.al("install", source=src)
        self.assertEqual(self.read(SKILL), b"# bundle v2\n")  # kept in sync
        self.al("uninstall")
        self.assertTrue(os.path.exists(self.p(SKILL)))  # it was there before: left in place
        self.assertTrue(os.path.islink(self.p(LINK)))

    def test_lost_manifest_does_not_freeze_or_delete_the_skill(self):
        src = self.source()
        self.al("install", source=src)
        os.unlink(self.p(".local/state/agentloop/install.json"))
        self.al("install", source=src)  # same version: the copy is exact, so it is taken back
        self.source({"SKILL.md": "# bundle v2\n"})
        self.al("install", source=src)
        self.assertEqual(self.read(SKILL), b"# bundle v2\n")  # updates resume
        self.al("uninstall")
        self.assertEqual(self.read(SKILL), b"# bundle v2\n")  # and it is never deleted

    def test_lost_manifest_and_new_bundle_leaves_the_old_copy_alone(self):
        src = self.source()
        self.al("install", source=src)
        os.unlink(self.p(".local/state/agentloop/install.json"))
        self.source({"SKILL.md": "# bundle v2\n"})
        out = self.al("install", source=src)
        self.assertIn("left %s alone" % self.p(SKILL_DIR), out)  # cannot prove it is agentloop's
        self.assertEqual(self.read(SKILL), bundled_skill().encode())
        self.al("uninstall")
        self.assertEqual(self.read(SKILL), bundled_skill().encode())

    def test_released_v1_install_upgrades_and_uninstalls_without_loss(self):
        self.write(".claude/settings.json", '{"model":"x"}')
        self.write(SKILL, "# mine\n")
        self.al("install", cmd=[sys.executable, v1_script()])
        self.al("install")
        self.al("uninstall")
        self.assertEqual(json.loads(self.read(".claude/settings.json")), {"model": "x"})
        # v1 itself replaced the skill and kept the user's text in a backup; nothing after it
        # may lose that backup or the installed copy.
        baks = [f for f in os.listdir(self.p(SKILL_DIR)) if ".agentloop-bak-" in f]
        self.assertEqual([self.read(os.path.join(SKILL_DIR, f)) for f in baks], [b"# mine\n"])
        self.assertEqual(self.read(SKILL), bundled_skill().encode())


if __name__ == "__main__":
    unittest.main()
