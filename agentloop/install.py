"""agentloop installer and uninstaller. Imported only for `agentloop install` / `uninstall`."""
import hashlib
import json
import os
import shutil
import sys
import time

from agentloop import (HOOK_TIMEOUT, MARK_BEGIN, MARK_END, VERSION, UserError, atomic_write,
                       bin_path, claude_dir, codex_dir, find_hook, health, hook_command, home,
                       is_ours, lib_dir, load_json, read_text, state_dir, toml_features_hooks,
                       trust_key)


def add_hook(data, env, agent):
    if find_hook(data, agent):
        return False
    hooks = data.setdefault("hooks", {})
    hooks.setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": hook_command(env, agent), "timeout": HOOK_TIMEOUT}]})
    return True


def remove_hook(data, agent):
    hooks = data.get("hooks") or {}
    groups = hooks.get("Stop") or []
    kept = []
    for g in groups:
        hs = [h for h in (g or {}).get("hooks") or [] if not is_ours(h, agent)]
        if hs:
            g["hooks"] = hs
            kept.append(g)
        elif not (g or {}).get("hooks") and g:
            kept.append(g)
    if kept:
        hooks["Stop"] = kept
    else:
        hooks.pop("Stop", None)
    if "hooks" in data and not hooks:
        data.pop("hooks")


def dump_json(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def toml_restore_features(text, before):
    """Undo `features.hooks = true` written by installers before this one; `before` is the
    line they recorded, or 'no-key'/'no-table'. Kept so older installs still uninstall cleanly."""
    lines = text.splitlines(True)
    section = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            section = s
        elif section == "[features]" and s == "hooks = true":
            if before not in ("no-key", "no-table"):
                lines[i] = before + "\n"
                return "".join(lines)
            del lines[i]
            out = "".join(lines)
            if before == "no-table":
                out = out.replace("\n\n[features]\n", "\n", 1) if "\n\n[features]\n" in out \
                    else out.replace("[features]\n", "", 1)
            return out
    return text


def toml_remove_table(text, header):
    lines = text.splitlines(True)
    out, skipping = [], False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            skipping = (s == header)
            if skipping and out and not out[-1].strip():
                out.pop()  # the blank line Codex writes before each table
        if not skipping:
            out.append(line)
    return "".join(out)


def norm_blank(text):
    return "\n".join(l for l in (text or "").splitlines() if l.strip())


def add_block(text, body):
    block = MARK_BEGIN + "\n" + body.rstrip() + "\n" + MARK_END
    if text and MARK_BEGIN in text:
        start = text.index(MARK_BEGIN)
        end = text.index(MARK_END, start)
        return text[:start] + block + text[end + len(MARK_END):]
    if not text:
        return block + "\n"
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block + "\n"


def remove_block(text):
    if not text or MARK_BEGIN not in text:
        return text
    a = text.index(MARK_BEGIN)
    b = text.find(MARK_END, a)
    b = len(text) if b < 0 else b + len(MARK_END)
    if text[b:b + 1] == "\n":
        b += 1
    head = text[:a]
    if head.endswith("\n\n"):
        head = head[:-1]
    return head + text[b:]


def imports_agents(claude_md, env):
    for line in (claude_md or "").splitlines():
        s = line.strip()
        if s.startswith("@"):
            target = s[1:]
            if target.startswith("~/"):
                target = os.path.join(home(env), target[2:])
            target = os.path.join(claude_dir(env), target)
            if os.path.realpath(target) == os.path.realpath(os.path.join(codex_dir(env), "AGENTS.md")):
                return True
    return False


class Changes:
    """Track file edits with backups and a manifest for uninstall."""

    def __init__(self, env):
        self.env = env
        self.mpath = os.path.join(state_dir(env), "install.json")
        self.manifest = json.loads(read_text(self.mpath) or "{}")
        self.manifest.setdefault("originals", {})
        self.manifest.setdefault("created", [])
        self.stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        self.log = []

    def write(self, path, new, label):
        old = read_text(path)
        if old == new:
            return
        orig_dir = os.path.join(state_dir(self.env), "originals")
        if path not in self.manifest["originals"]:
            if old is None:
                self.manifest["originals"][path] = None
            else:
                os.makedirs(orig_dir, exist_ok=True)
                keep = os.path.join(orig_dir, hashlib.sha1(path.encode()).hexdigest()[:12])
                shutil.copy2(path, keep)
                self.manifest["originals"][path] = keep
        if old is not None:
            shutil.copy2(path, "%s.agentloop-bak-%s" % (path, self.stamp))
        atomic_write(path, new)
        self.log.append(label)

    def save(self):
        atomic_write(self.mpath, json.dumps(self.manifest, indent=1) + "\n")


MODULES = ("agentloop.py", "install.py")

# ~/.local/bin/agentloop keeps its path (the Stop hooks call it); it only imports the modules.
# It fails open: if the modules are missing or from another version, `hook` exits 0 silently.
# -sS (one combined flag: Linux passes a single shebang argument) skips site-packages;
# agentloop is stdlib-only. Not -I: it implies -E, which on Apple's /usr/bin/python3 drops
# the stdlib bytecode cache and recompiles json/re on every Stop (~45 ms instead of ~20).
LAUNCHER = """#!%(python)s -sS
# agentloop launcher, installed by skills-lite. Code: %(lib)s
import sys
sys.path.insert(0, %(lib)r)
try:
    from agentloop import VERSION, main
    if VERSION != %(version)r:
        raise ImportError("modules are version %%s, launcher expects %(version)s" %% VERSION)
except Exception as e:  # never let a broken install block an agent's Stop
    if sys.argv[1:2] == ["hook"]:
        try:
            import json, os, time
            d = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "agentloop")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "log"), "a") as f:
                f.write(json.dumps({"ts": time.strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ", time.gmtime()),
                                    "error": "launcher: %%s" %% e}) + "\\n")
        except Exception:
            pass
        sys.exit(0)
    sys.stderr.write("agentloop: installation is incomplete (%%s); rerun ./install.sh\\n" %% e)
    sys.exit(2)
sys.exit(main(sys.argv[1:]))
"""


def pyc_path(source):
    d, f = os.path.split(source)
    return os.path.join(d, "__pycache__", "%s.%s.pyc" % (f[:-3], sys.implementation.cache_tag))


def install(env, python, source):
    pkg = os.path.dirname(os.path.abspath(__file__))
    prepared = {}
    # Read and validate everything first so a bad file aborts before any write.
    cs = os.path.join(claude_dir(env), "settings.json")
    ch = os.path.join(codex_dir(env), "hooks.json")
    ct = os.path.join(codex_dir(env), "config.toml")
    for path in (cs, ch):
        prepared[path] = load_json(path) or {}
    toml = read_text(ct) or ""
    try:
        import tomllib
    except ImportError:  # Python < 3.11: no stdlib TOML parser; edits stay line-based
        tomllib = None
    if tomllib and toml:
        try:
            tomllib.loads(toml)
        except Exception as e:
            raise UserError("agentloop: %s is not valid TOML (%s); nothing was changed" % (ct, e))
    root = source or os.path.dirname(pkg)
    lifecycle = read_text(os.path.join(root, "AGENTS.md"))
    if not lifecycle or not lifecycle.strip():
        raise UserError("agentloop: missing package AGENTS.md; run install.sh from the repository")
    agents_md = os.path.join(codex_dir(env), "AGENTS.md")
    claude_md = os.path.join(claude_dir(env), "CLAUDE.md")
    guidance = lifecycle.replace("](skills/review-plan/SKILL.md)",
                                 "](~/.agents/skills/review-plan/SKILL.md)")
    guidance = guidance.rstrip() + "\n\nUse `agentloop` to record lifecycle steps; it detects the current session automatically."
    cm = read_text(claude_md) or ""
    for current in (read_text(agents_md) or "", cm):
        if (current.count(MARK_BEGIN), current.count(MARK_END)) not in ((0, 0), (1, 1)) or current.find(MARK_BEGIN) > current.find(MARK_END):
            raise UserError("agentloop: malformed guidance markers; nothing was changed")
    agents_text = add_block(read_text(agents_md), guidance)
    if imports_agents(remove_block(cm), env):
        claude_text = remove_block(cm)
    else:
        target = os.path.relpath(agents_md, claude_dir(env))
        claude_text = add_block(cm, "@" + target)

    ch_ = Changes(env)

    # 1. modules, then the launcher at the path the hooks call
    dest = bin_path(env)
    if os.path.exists(dest) and "agentloop" not in (read_text(dest) or "")[:400]:
        raise UserError("agentloop: %s exists and is not agentloop; nothing was changed" % dest)
    import py_compile
    lib, created = lib_dir(env), ch_.manifest["created"]
    lib_is_ours = lib in created or not os.path.lexists(lib)  # never rmtree a dir we found
    for name in MODULES:
        target = os.path.join(lib, name)
        text = read_text(os.path.join(pkg, name))
        if read_text(target) != text:
            atomic_write(target, text)
            ch_.log.append("installed " + target)
        cfile = pyc_path(target)
        py_compile.compile(target, cfile=cfile, doraise=True)
        created.extend(p for p in (target, cfile) if p not in created)
    if lib_is_ours and lib not in created:
        created.append(lib)
    new_bin = LAUNCHER % {"python": python or sys.executable, "lib": lib, "version": VERSION}
    if read_text(dest) != new_bin:
        if not os.path.exists(dest) and dest not in created:
            created.append(dest)
        atomic_write(dest, new_bin)
        ch_.log.append("installed " + dest)
    os.chmod(dest, 0o755)

    # 2. hooks
    for path, agent in ((cs, "claude"), (ch, "codex")):
        data = prepared[path]
        if add_hook(data, env, agent):
            ch_.write(path, dump_json(data), "added %s Stop hook to %s" % (agent, path))

    # 3. config.toml is left alone: Codex enables hooks by default, and an explicit
    #    `hooks = false` is the user's choice (status warns about it).

    ch_.write(agents_md, agents_text, "updated guidance in " + agents_md)
    ch_.write(claude_md, claude_text, "updated guidance in " + claude_md)

    # 5. skills
    if source and os.path.isdir(os.path.join(source, "skills")):
        install_skills(env, os.path.join(source, "skills"), ch_)

    ch_.save()
    for line in ch_.log or ["already installed; nothing changed"]:
        print("agentloop: " + line)
    bindir = os.path.dirname(dest)
    if bindir not in env.get("PATH", "").split(os.pathsep):
        shell = os.path.basename(env.get("SHELL", "sh"))
        rc = {"zsh": "~/.zshrc", "bash": "~/.bashrc"}.get(shell, "~/.profile")
        print("agentloop: %s is not on PATH. Add this line to %s:" % (bindir, rc))
        print('  export PATH="$HOME/.local/bin:$PATH"')
    if toml_features_hooks(toml) is False:
        print("agentloop: Codex: hooks are turned off in %s [features], so its Stop hook will not "
              "run; agentloop left that setting alone. Remove it to turn hooks on." % ct)
    if not health(env)["codex"] == []:
        print("agentloop: Codex: open `codex` once (in an empty folder) and choose "
              "'Trust all and continue' to enable its Stop hook.")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def through_link(env, path):
    """True if a folder between HOME and path is a symlink. Skill files are never written or
    deleted through one: the real files would be somewhere agentloop did not choose."""
    base = home(env)
    rel = os.path.relpath(os.path.dirname(path), base)
    cur = base
    for part in ([] if rel == "." or rel.startswith("..") else rel.split(os.sep)):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    return False


def install_skills(env, src_root, ch_):
    agents_skills = os.path.join(home(env), ".agents", "skills")
    claude_skills = os.path.join(claude_dir(env), "skills")
    created, hashes = ch_.manifest["created"], ch_.manifest.setdefault("hashes", {})
    # Skill files that were already there, identical to the bundle, when agentloop took them
    # over: kept in sync like its own, but left in place on uninstall.
    preexisting = ch_.manifest.setdefault("preexisting", [])
    for name in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, name)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(agents_skills, name)
        link = os.path.join(claude_skills, name)
        bundled = {os.path.join(dst, os.path.relpath(os.path.join(r, f), src)): os.path.join(r, f)
                   for r, _, fs in os.walk(src) for f in fs}
        if os.path.islink(dst) or through_link(env, dst):
            ch_.log.append("left %s alone: it is reached through a symlink" % dst)
            continue
        mine = [os.path.join(r, f) for r, _, fs in os.walk(dst) for f in fs] if os.path.isdir(dst) else []
        if os.path.lexists(dst) and not any(p in created or p in preexisting for p in mine):
            # Not recorded as agentloop's. Take it over only if it is exactly the bundled copy
            # (an earlier install, or a lost manifest): nothing of the user's can be lost then.
            if not (os.path.isdir(dst) and set(mine) == set(bundled) and all(
                    not os.path.islink(p) and read_text(p) == read_text(bundled[p]) for p in mine)):
                ch_.log.append("left %s alone: a skill with that name exists and agentloop did "
                               "not create it" % dst)
                continue
            preexisting.extend(p for p in mine if p not in preexisting)
            ch_.log.append("now keeping %s in sync: it matches the bundled copy exactly" % dst)
        for p in mine:  # files an earlier version installed that are no longer bundled
            if p in created and p not in bundled and not os.path.islink(p) and \
                    not through_link(env, p) and hashes.get(p) == sha256(read_text(p) or ""):
                os.unlink(p)
                created.remove(p)
                hashes.pop(p, None)
                ch_.log.append("removed old skill file " + p)
        for t, s in sorted(bundled.items()):
            if os.path.islink(t) or through_link(env, t):
                ch_.log.append("left %s alone: it is or is reached through a symlink" % t)
                continue
            new, old = read_text(s), read_text(t)
            if old == new:
                continue
            if old is None and t not in preexisting:
                created.append(t)
            else:
                shutil.copy2(t, "%s.agentloop-bak-%s" % (t, ch_.stamp))
            if t in created:
                hashes[t] = sha256(new)
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.copy2(s, t)
            ch_.log.append("installed skill file " + t)
        if os.path.lexists(link):
            if not (os.path.islink(link) and os.path.realpath(link) == os.path.realpath(dst)):
                ch_.log.append("left existing %s unchanged" % link)
            continue
        if os.path.islink(claude_skills):
            ch_.log.append("left %s alone: it is a symlink" % claude_skills)
            continue
        os.makedirs(claude_skills, exist_ok=True)
        os.symlink(dst, link)
        created.append(link)
        ch_.log.append("linked %s -> %s" % (link, dst))


def uninstall(env):
    ch_ = Changes(env)
    m = ch_.manifest
    cs = os.path.join(claude_dir(env), "settings.json")
    ch = os.path.join(codex_dir(env), "hooks.json")
    ct = os.path.join(codex_dir(env), "config.toml")
    done = []
    codex_idx = find_hook(load_json(ch, quiet=True) or {}, "codex")

    def finish(path, new):
        """Write new content, restoring the original bytes when nothing else changed."""
        orig = m["originals"].get(path, "missing")
        if orig is None and not norm_blank(new).strip("{} \n"):
            if os.path.exists(path):
                os.unlink(path)
                done.append("removed " + path)
            return
        if orig not in (None, "missing"):
            orig_text = read_text(orig)
            same = (orig_text is not None and
                    (norm_blank(orig_text) == norm_blank(new) or
                     (path.endswith(".json") and json.loads(orig_text or "{}") == json.loads(new or "{}"))))
            if same:
                new = orig_text
        if read_text(path) != new:
            atomic_write(path, new)
            done.append("restored " + path)

    for path, agent in ((cs, "claude"), (ch, "codex")):
        data = load_json(path, quiet=True)
        if data is not None and find_hook(data, agent):
            remove_hook(data, agent)
            finish(path, dump_json(data) if data else "{}\n")
    toml = read_text(ct)
    if toml is not None:
        new = toml
        if codex_idx:
            new = toml_remove_table(new, trust_key(env, codex_idx[0]))
        before = m.get("toml_features_before", "unchanged")
        if before != "unchanged":
            new = toml_restore_features(new, before)
        if new != toml:
            finish(ct, new)
    for path in (os.path.join(codex_dir(env), "AGENTS.md"), os.path.join(claude_dir(env), "CLAUDE.md")):
        text = read_text(path)
        if text and MARK_BEGIN in text:
            finish(path, remove_block(text))
    agents_skills = os.path.join(home(env), ".agents", "skills")
    claude_skills = os.path.join(claude_dir(env), "skills")
    for path in reversed(m["created"]):
        want = m.get("hashes", {}).get(path)
        skill = path.startswith((agents_skills + os.sep, claude_skills + os.sep))
        if os.path.islink(path) and want:
            done.append("kept %s: it was replaced by a symlink" % path)
            continue
        if os.path.islink(path) and os.path.dirname(path) == claude_skills and \
                os.readlink(path) != os.path.join(agents_skills, os.path.basename(path)):
            done.append("kept %s: it now points somewhere else" % path)
            continue
        if skill and not os.path.islink(path) and through_link(env, path):
            done.append("kept %s: it is reached through a symlinked folder" % path)
            continue
        if want and os.path.isfile(path) and not os.path.islink(path):
            if hashlib.sha256((read_text(path) or "").encode("utf-8")).hexdigest() != want:
                done.append("kept edited " + path)
                continue
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
            done.append("removed " + path)
        elif path == lib_dir(env) and os.path.isdir(path):
            shutil.rmtree(path)  # includes __pycache__ written later by another Python
            done.append("removed " + path)
    if os.path.exists(bin_path(env)):
        os.unlink(bin_path(env))
        done.append("removed " + bin_path(env))
    if os.path.exists(ch_.mpath):
        os.unlink(ch_.mpath)
    shutil.rmtree(os.path.join(state_dir(env), "originals"), ignore_errors=True)
    for line in done or ["nothing to remove"]:
        print("agentloop: " + line)
