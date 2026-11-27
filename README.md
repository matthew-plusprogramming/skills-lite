The biggest bang for your buck built on 1000 hours of agentic development

## Install

Requires macOS or Linux and Python 3.8 or newer.

```sh
git clone https://github.com/matthew-plusprogramming/skills-lite.git
cd skills-lite
./install.sh
```

The installer is safe to re-run. Before it first changes a file it saves the original under `~/.local/state/agentloop/originals`, keeps your existing settings, and stops without writing anything if a settings file does not parse. If `~/.local/bin` is not on your `PATH`, it prints the line to add.

Codex runs a new hook only after you trust it: open `codex` once (in an empty folder) and choose **Trust all and continue**.

## Scope

The installer works user-wide, for Claude Code and Codex:

- Installs the `agentloop` CLI to `~/.local/bin/agentloop`, a small launcher for the code in `~/.local/share/agentloop`.
- Adds one `Stop` hook to `~/.claude/settings.json` and one to `~/.codex/hooks.json`, and sets `features.hooks = true` in `~/.codex/config.toml`. Hooks are enabled on install.
- Installs the full work loop from the repository’s `AGENTS.md` into a marked section of `~/.codex/AGENTS.md`. Claude imports that file from `~/.claude/CLAUDE.md`. Re-running the installer upgrades the previous reminder and preserves personal instructions outside the marked section.
- Copies `skills/` into `~/.agents/skills` (saving the original of any file that differs) and links each skill into `~/.claude/skills` where no entry exists yet.

Lifecycle state is kept per session under `~/.local/state/agentloop`. The installer never changes sandbox or approval settings. In a sandboxed agent shell (for example Codex `workspace-write`) that folder may not be writable, so `agentloop` commands need your agent's approval to run outside the sandbox.

## Behavior

1. AGENTS.md already tells agents to start the lifecycle.
2. If no lifecycle state is recorded, a premature Stop gets one reminder. This is on by default.
3. Normal sessions get no extra turn. A fully verified loop's finishing Stop is allowed and ends that loop, and the next Stop starts fresh.
4. Waiting for approval pauses and keeps state, and the Stop yields.
5. `agentloop exempt` ends the current loop for one-offs or mistaken nudges. That Stop is allowed, and the next independent Stop starts fresh.

## Limits

Run `agentloop` with no arguments for its commands. Headless runs (`claude -p`, `codex exec`) are never blocked, and `AGENTLOOP=off` disables the hook for one process. Stop hooks run only when a turn ends, so they catch a forgotten step but cannot prevent work done within a turn.

## Uninstall

```sh
./install.sh --uninstall
```

This removes the CLI and its code, both hooks, Codex's trust record for the hook, the guidance block and any skill files the installer created, and puts back skill files it replaced. Files are restored byte for byte when nothing else changed them; files you edited after installing are kept.
