"""agentloop runtime: record work-loop steps, report status and decide Claude Code / Codex Stop hooks.

Kept lean: everything here runs on every Stop, so install-only code lives in agentloop.install.
"""
import json
import os
import subprocess
import sys
import time

from agentloop.common import (UserError, atomic_write, bin_path, claude_dir, codex_dir, fcntl,
                              find_hook, load_json, log, read_text, state_dir,
                              toml_features_hooks, trust_key)

VERSION = "2"


USAGE = """usage: agentloop <command>
  plan <link>           plan published; next: independent review
  reviewed              review addressed; now wait for approval
  approved              human explicitly approved; start work
  verified <evidence>   separate verifier accepted the result
  wait [why]            ending this turn to wait for the human
  exempt [why]          leave the loop: one-off ask or mistaken nudge
  status                show phase, next step and hook health
The current session is detected automatically.
"""


MAX_TURNS = 64


NEXT = {
    None: "record the current step: `agentloop plan <link>`, or `agentloop exempt` for a one-off",
    "review": "get an independent read-only review, send the revised plan, then `agentloop reviewed`",
    "approval": "wait for the human's explicit approval, then `agentloop approved`",
    "work": "collect evidence, have a separate verifier judge it, then `agentloop verified <evidence>`",
    "done": "loop complete; the next task starts fresh",
    "exempt": "exempt; the next task starts fresh",
}


REASONS = {
    None: ("agentloop: no lifecycle step is recorded for this task. Follow the AGENTS.md work loop "
           "and record the current step with `agentloop` (run `agentloop` for usage). For a one-off "
           "or a mistaken nudge, run `agentloop exempt`. If you are waiting for the human, run "
           "`agentloop wait`."),
    "review": ("agentloop: the plan is recorded but not reviewed. Have an independent read-only "
               "reviewer review it, address the findings, send the revised plan to the human, then "
               "run `agentloop reviewed`. If you are waiting for the human, run `agentloop wait`."),
    "work": ("agentloop: work is in progress but not verified. Collect evidence, have a separate "
             "verifier judge it against the plan, then run `agentloop verified \"<evidence>\"`. If "
             "you need the human, run `agentloop wait`; for a one-off, `agentloop exempt`."),
}


class Session:
    def __init__(self, env, agent, sid):
        if not sid or "/" in sid or sid.startswith("."):
            raise UserError("agentloop: invalid session id")
        self.path = os.path.join(state_dir(env), "sessions", "%s-%s.json" % (agent, sid))
        self.agent, self.sid = agent, sid
        self.lockf = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if fcntl:
            self.lockf = open(self.path + ".lock", "w")
            fcntl.flock(self.lockf, fcntl.LOCK_EX)
        text = read_text(self.path)
        self.exists = text is not None
        self.data = json.loads(text) if text else {}
        self.data.setdefault("phase", None)
        self.data.setdefault("turns", {})
        return self

    def save(self):
        turns = self.data["turns"]
        while len(turns) > MAX_TURNS:
            turns.pop(next(iter(turns)))
        self.data["agent"], self.data["session"] = self.agent, self.sid
        self.data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write(self.path, json.dumps(self.data, indent=1) + "\n")

    def __exit__(self, *exc):
        if self.lockf:
            self.lockf.close()


def _darwin_proc(pid):
    """(ppid, name) via sysctl KERN_PROC_PID: no exec, so it works inside the Codex sandbox."""
    import ctypes
    libc = ctypes.CDLL(None)
    buf = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(buf))
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 564:
        return None
    raw = buf.raw[:size.value]
    # struct kinfo_proc (64-bit): kp_proc.p_comm at 243, kp_eproc.e_ppid at 560
    name = raw[243:260].split(b"\0")[0].decode("utf-8", "replace")
    return int.from_bytes(raw[560:564], sys.byteorder), name


def _darwin_argv(pid):
    """Exact argv via sysctl KERN_PROCARGS2 (keeps arguments that contain spaces intact)."""
    import ctypes
    libc = ctypes.CDLL(None)
    size = ctypes.c_size_t(0)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    raw = buf.raw[:size.value]
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    rest = rest[rest.index(b"\0"):].lstrip(b"\0")  # skip exec path and padding
    return [a.decode("utf-8", "replace") for a in rest.split(b"\0")[:argc]]


def _ps(pid, field):
    out = subprocess.run(["ps", "-o", field + "=", "-p", str(pid)], capture_output=True,
                         text=True, timeout=5).stdout.strip()
    return out or None


def proc_info(pid, want_args=False):
    """Return (ppid, name, args) for pid, or None. args is [] unless want_args."""
    if os.path.isdir("/proc/%d" % pid):
        try:
            with open("/proc/%d/stat" % pid) as f:
                stat = f.read()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            try:
                name = os.path.basename(os.readlink("/proc/%d/exe" % pid))
            except OSError:
                pass
            args = []
            if want_args:
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    args = [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
            return ppid, name, args
        except (OSError, ValueError, IndexError):
            return None
    info = None
    if sys.platform == "darwin":
        try:
            info = _darwin_proc(pid)
        except Exception:
            info = None
    args = []
    try:
        if info is None:
            out = _ps(pid, "ppid")
            name = _ps(pid, "comm")
            if not out or name is None:
                return None
            info = (int(out), name)
        if want_args:
            argv = None
            if sys.platform == "darwin":
                try:
                    argv = _darwin_argv(pid)
                except Exception:
                    argv = None
            args = argv if argv is not None else (_ps(pid, "args") or "").split()
    except (OSError, ValueError, subprocess.SubprocessError):
        if info is None:
            return None
    return info[0], info[1], args


def ancestors(start, want_args=False):
    pid, seen = start, set()
    while pid and pid > 1 and pid not in seen and len(seen) < 64:
        seen.add(pid)
        info = proc_info(pid, want_args)
        if not info:
            return
        ppid, name, args = info
        yield pid, name, args
        pid = ppid


def test_tree(env):
    """Test seam: AGENTLOOP_TEST_PROCTREE='[[pid, name, [args]], ...]' replaces the real
    process tree, nearest first, so black-box tests can describe nested and headless runs."""
    raw = env.get("AGENTLOOP_TEST_PROCTREE")
    if not raw:
        return None
    return [(int(p), str(n), [str(a) for a in args]) for p, n, args in json.loads(raw)]


def nearest_agent(env, start=None, want_args=False):
    """Return (agent, pid, args) for the nearest Claude or Codex ancestor."""
    claude_pid = env.get("CLAUDE_PID", "")
    tree = test_tree(env)
    for pid, name, args in (tree if tree is not None else ancestors(start or os.getppid(), want_args)):
        base = os.path.basename(name)
        if claude_pid and str(pid) == claude_pid:
            return "claude", pid, args
        if base == "codex":
            return "codex", pid, args
    return None


def current_session(env, start=None):
    found = nearest_agent(env, start)
    if not found:
        raise UserError("agentloop: no calling Claude Code or Codex session found "
                        "(run it from an agent's shell)")
    agent = found[0]
    sid = env.get("CLAUDE_CODE_SESSION_ID" if agent == "claude" else "CODEX_THREAD_ID")
    if not sid:
        raise UserError("agentloop: %s session id is not set in this shell" % agent)
    return agent, sid


def is_headless(agent, env, start=None):
    found = nearest_agent(env, start, want_args=True)
    args = found[2][1:] if found and found[0] == agent else []
    if agent == "claude":
        ep = env.get("CLAUDE_CODE_ENTRYPOINT", "")
        return ep.startswith("sdk") or "-p" in args or "--print" in args
    return codex_subcommand(args) in ("exec", "e", "review")


CODEX_VALUE_FLAGS = {"-c", "--config", "-i", "--image", "-m", "--model", "-p", "--profile",
                     "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "--enable",
                     "--disable", "--add-dir", "--local-provider", "--remote",
                     "--remote-auth-token-env"}


def codex_subcommand(args):
    """First positional argument after codex's global options (None if there is none)."""
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in CODEX_VALUE_FLAGS:
            skip = True
        elif not a.startswith("-"):
            return a
    return None


def cmd_record(env, session, cmd, rest):
    agent, sid = session
    if cmd == "plan" and not rest:
        raise UserError("usage: agentloop plan <link>")
    if cmd == "verified" and not rest:
        raise UserError("usage: agentloop verified <evidence>")
    note = " ".join(rest)
    with Session(env, agent, sid) as s:
        d = s.data
        if cmd == "wait":
            d["wait"] = True
        else:
            d["phase"] = {"plan": "review", "reviewed": "approval", "approved": "work",
                          "verified": "done", "exempt": "exempt"}[cmd]
            d["wait"] = False
            d["finish_turn"] = None
            if cmd == "plan":
                d["plan"] = note
        d.setdefault("history", []).append([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                            cmd, note])
        d["history"] = d["history"][-50:]
        s.save()
        phase = d["phase"]
    msg = "agentloop: recorded %s (%s session)." % (cmd, agent)
    if cmd == "wait":
        msg += " The next Stop yields; phase stays %s." % (phase or "unset")
    else:
        msg += " Next: %s." % NEXT[phase]
    print(msg)
    if cmd == "plan":
        for w in health(env)[agent]:
            print("agentloop warning: " + w)


def cmd_status(env, session):
    agent, sid = session
    path = Session(env, agent, sid).path
    text = read_text(path)
    d = json.loads(text) if text else {}
    phase = d.get("phase")
    print("session: %s %s" % (agent, sid))
    print("phase:   %s%s" % (phase or "none", " (wait pass pending)" if d.get("wait") else ""))
    if d.get("plan"):
        print("plan:    %s" % d["plan"])
    print("next:    %s" % NEXT[phase])
    problems = health(env)
    for a in ("claude", "codex"):
        print("hook %-6s %s" % (a + ":", "; ".join(problems[a]) or "ok"))


def health(env):
    out = {"claude": [], "codex": []}
    s = load_json(os.path.join(claude_dir(env), "settings.json"), quiet=True)
    if not s or not find_hook(s, "claude"):
        out["claude"].append("Stop hook not configured (run install.sh)")
    h = load_json(os.path.join(codex_dir(env), "hooks.json"), quiet=True)
    idx = find_hook(h or {}, "codex")
    if not idx:
        out["codex"].append("Stop hook not configured (run install.sh)")
    else:
        toml = read_text(os.path.join(codex_dir(env), "config.toml")) or ""
        if not toml_features_hooks(toml):
            out["codex"].append("features.hooks is not true in config.toml")
        if trust_key(env, idx[0]) not in toml:
            out["codex"].append("hook not trusted yet: open codex and choose 'Trust all and continue'")
    if not os.path.exists(bin_path(env)):
        for a in out:
            out[a].append("binary missing at " + bin_path(env))
    errs = recent_errors(env)
    if errs:
        for a in out:
            out[a].append("%d hook error(s) in the last day (see %s)" % (
                errs, os.path.join(state_dir(env), "log")))
    return out


def recent_errors(env):
    text = read_text(os.path.join(state_dir(env), "log")) or ""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    n = 0
    for line in text.splitlines()[-500:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("error") and rec.get("ts", "") >= cutoff:
            n += 1
    return n


def turn_key(payload):
    for k in ("prompt_id", "turn_id"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def decide(env, agent, payload, start=None):
    """Return (block_reason_or_None, why) and update state."""
    if env.get("AGENTLOOP", "").lower() == "off":
        return None, "disabled"
    if is_headless(agent, env, start):
        return None, "headless"
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("payload has no session_id")
    turn = turn_key(payload)
    if not turn:
        log(env, agent=agent, session=sid, error="payload has no prompt_id/turn_id")
        return None, "no-turn-id"
    with Session(env, agent, sid) as s:
        d = s.data
        turns = d["turns"]
        if d["phase"] in ("done", "exempt"):
            if not d.get("finish_turn"):
                d["finish_turn"] = turn
                turns[turn] = "allow"
                s.save()
                return None, "finish"
            if turn == d["finish_turn"] or turn in turns:
                return None, "finish-repeat"
            d["phase"], d["finish_turn"], d["wait"] = None, None, False
            d.pop("plan", None)
        if turn in turns or payload.get("stop_hook_active") is True:
            if turn not in turns:
                turns[turn] = "allow"
                s.save()
            return None, "repeat"
        decision, why = None, "allowed"
        if d.get("wait"):
            d["wait"], why = False, "wait"
        elif payload.get("background_tasks"):
            why = "background"
        elif d["phase"] == "approval":
            why = "approval"
        else:
            decision, why = REASONS[d["phase"]], "remind-%s" % (d["phase"] or "none")
        turns[turn] = "block" if decision else "allow"
        s.save()
        return decision, why


def cmd_hook(env, agent, stdin_text, start=None):
    if agent not in ("claude", "codex"):
        return ""
    try:
        payload = json.loads(stdin_text or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        if payload.get("hook_event_name") not in (None, "Stop"):
            return ""
        reason, why = decide(env, agent, payload, start)
        log(env, agent=agent, session=payload.get("session_id"), turn=turn_key(payload),
            cwd=payload.get("cwd"), decision="block" if reason else "allow", why=why)
        if reason:
            return json.dumps({"decision": "block", "reason": reason})
        return ""
    except Exception as e:  # fail open
        log(env, agent=agent, error="%s: %s" % (type(e).__name__, e))
        return ""


def main(argv, env=None, session=None, stdin=None, start=None):
    env = dict(os.environ) if env is None else env
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "hook":
            out = cmd_hook(env, rest[0] if rest else "", (stdin or sys.stdin).read(), start)
            if out:
                print(out)
            return 0
        if cmd == "install":
            from agentloop.install import install
            python = source = None
            while rest:
                flag = rest.pop(0)
                if flag == "--python" and rest:
                    python = rest.pop(0)
                elif flag == "--source" and rest:
                    source = rest.pop(0)
            install(env, python, source)
            return 0
        if cmd == "uninstall":
            from agentloop.install import uninstall
            uninstall(env)
            return 0
        if cmd in ("plan", "reviewed", "approved", "verified", "wait", "exempt", "status"):
            session = session or current_session(env, start)
            if cmd == "status":
                cmd_status(env, session)
            else:
                cmd_record(env, session, cmd, rest)
            return 0
        sys.stderr.write("agentloop: unknown command %r\n%s" % (cmd, USAGE))
        return 2
    except UserError as e:
        sys.stderr.write("%s\n" % e)
        return 2
    except OSError as e:
        sys.stderr.write("agentloop: cannot write lifecycle state in %s (%s). A sandboxed agent "
                         "shell may not allow writes there; rerun the command with your agent's "
                         "approval to run outside the sandbox.\n" % (state_dir(env), e))
        return 2
