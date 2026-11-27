"""Paths, file helpers and hook ownership checks shared by the runtime and the installer."""
import json
import os
import re  # json imports re anyway, so this costs nothing extra
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None


MARK_BEGIN = "<!-- agentloop:begin -->"


MARK_END = "<!-- agentloop:end -->"


HOOK_TIMEOUT = 10


class UserError(Exception):
    pass


def home(env):
    return env.get("HOME") or os.path.expanduser("~")


def state_dir(env):
    base = env.get("XDG_STATE_HOME") or os.path.join(home(env), ".local", "state")
    return os.path.join(base, "agentloop")


def claude_dir(env):
    return env.get("CLAUDE_CONFIG_DIR") or os.path.join(home(env), ".claude")


def codex_dir(env):
    return env.get("CODEX_HOME") or os.path.join(home(env), ".codex")


def bin_path(env):
    return os.path.join(home(env), ".local", "bin", "agentloop")


def lib_dir(env):
    """Installed modules live here; ~/.local/bin/agentloop is a launcher that imports them."""
    return os.path.join(home(env), ".local", "share", "agentloop")


def atomic_write(path, data):
    """Write via a temp file in the same directory and rename, keeping the file's mode.
    Plain os calls: tempfile and shutil would add ~5 ms of imports to every Stop."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".agentloop-%d-%s" % (os.getpid(), os.path.basename(path)))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def log(env, **rec):
    try:
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        d = state_dir(env)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:
        pass


def hook_command(env, agent):
    return '"%s" hook %s' % (bin_path(env), agent)


def load_json(path, quiet=False):
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        if quiet:
            return None
        raise UserError("agentloop: %s is not valid JSON (%s); nothing was changed" % (path, e))
    if not isinstance(data, dict):
        if quiet:
            return None
        raise UserError("agentloop: %s is not a JSON object; nothing was changed" % path)
    return data


def is_ours(handler, agent):
    cmd = str((handler or {}).get("command", "")) if isinstance(handler, dict) else ""
    return re.search(r'agentloop["\']?\s+hook\s+%s\b' % agent, cmd) is not None


def find_hook(data, agent):
    """Return (group_index, handler_index) of our Stop handler for agent, or None."""
    groups = (data.get("hooks") or {}).get("Stop") or []
    for gi, g in enumerate(groups):
        for hi, h in enumerate((g or {}).get("hooks") or []):
            if is_ours(h, agent):
                return gi, hi
    return None


def toml_features_hooks(text):
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s
        elif section == "[features]" and s.replace(" ", "").startswith("hooks="):
            return s.split("=", 1)[1].split("#")[0].strip() == "true"
    return False


def trust_key(env, group_index):
    path = os.path.join(codex_dir(env), "hooks.json")
    return '[hooks.state."%s:stop:%d:0"]' % (path, group_index)
