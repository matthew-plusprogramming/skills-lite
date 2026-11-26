"""Measure the deployed Stop hook as the agents run it: install into a temporary HOME,
then time fresh processes of the exact hook command from settings.json, per branch.

    python3 tests/bench_hook.py [--runs 60] [--python /path/to/python3]

Not a unit test. Numbers depend on the machine; compare runs on the same machine.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * len(xs))) - 1)] if p < 100 else xs[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()
    home = tempfile.mkdtemp(prefix="agentloop-bench-")
    env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "CLAUDE_CODE_ENTRYPOINT": "cli"}
    for k in ("CLAUDE_PID",):  # keep the real process tree, as in a live session
        if k in os.environ:
            env[k] = os.environ[k]
    r = subprocess.run([a.python, os.path.join(ROOT, "bin", "agentloop"), "install",
                        "--python", a.python, "--source", ROOT], env=env, capture_output=True, text=True)
    if r.returncode:
        sys.exit("install failed: " + r.stderr)
    settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
    hook = shlex.split(settings["hooks"]["Stop"][0]["hooks"][0]["command"])
    binp = hook[0]
    sd = os.path.join(home, ".local", "state", "agentloop", "sessions")
    os.makedirs(sd, exist_ok=True)
    for ph in ("review", "work", "approval"):
        with open(os.path.join(sd, "claude-s-%s.json" % ph), "w") as f:
            json.dump({"phase": ph, "turns": {}}, f)

    def run(name, argv, sid=None, extra=None, via_sh=False):
        e = dict(env, **(extra or {}))
        xs = []
        for i in range(a.runs):
            data = json.dumps({"session_id": sid, "prompt_id": "p%d" % i, "hook_event_name": "Stop"}) if sid else ""
            cmd = ["sh", "-c", " ".join(shlex.quote(x) for x in argv)] if via_sh else argv
            t = time.perf_counter()
            subprocess.run(cmd, input=data, text=True, env=e, capture_output=True)
            xs.append((time.perf_counter() - t) * 1000)
        print("%-44s p50 %6.1f  p95 %6.1f ms" % (name, pct(xs, 50), pct(xs, 95)))
        return pct(xs, 95)

    print("python: %s   runs: %d   platform: %s" % (a.python, a.runs, sys.platform))
    run("floor: python -c pass", [a.python, "-c", "pass"])
    run("hook claude: no state (remind)", hook, "s-none")
    run("hook claude: review (remind)", hook, "s-review")
    run("hook claude: approval (allow)", hook, "s-approval")
    run("hook claude: AGENTLOOP=off", hook, "s-off", {"AGENTLOOP": "off"})
    run("hook codex: no state", [binp, "hook", "codex"], "c-none")
    run("hook claude via sh -c (as agents spawn it)", hook, "s-sh", via_sh=True)
    run("status (no session; error path)", [binp, "status"])


if __name__ == "__main__":
    main()
