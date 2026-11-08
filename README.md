# ram-guard

A Claude Code skill that keeps your machine and Claude alive when agents use too much
memory. Instead of letting the kernel or systemd-oomd kill your session, it pauses the
heaviest agent-started work, holds new launches until memory recovers, and kills only as
a last resort.

Linux only. One Python file, standard library only, no root needed.

## Why

Agents start heavy things in parallel: builds, test runners, dev servers, more agents.
When RAM runs out, the out-of-memory killer picks a large victim, and that is often the
agent session or the whole desktop app. You lose the conversation along with the work.
ram-guard acts earlier, and it only ever acts on processes the agent itself started.

## What it does

| Level | Default trigger | Action |
|---|---|---|
| warn | 80% RAM used | Claude is told memory is tight and how to lighten the load |
| critical | 90% RAM used, or heavy memory-pressure stalls | The fastest-growing or largest agent-started process tree is paused (SIGSTOP). New Bash, Agent and Workflow calls are held, then denied with an explanation |
| emergency | 97% RAM and 90% swap used | The single largest agent-started tree is killed |

Paused work resumes by itself once usage drops below 75%. Agent sessions, MCP servers,
the desktop app and anything you started yourself are never paused or killed.

It also makes agent-started processes the kernel's preferred out-of-memory victims, so
if the kernel does have to kill something, it takes a build rather than Claude.

## Install

```bash
git clone https://github.com/dhivadhiva/ram-guard ~/.claude/skills/ram-guard
```

```bash
python3 ~/.claude/skills/ram-guard/scripts/ram_guard.py install
```

The installer:

- writes default settings to `~/.config/ram-guard/config.json`
- links the `ram-guard` command into `~/.local/bin`
- enables a systemd user service that runs the monitor from login
- merges three hooks into `~/.claude/settings.json` and keeps a backup beside it

Restart any Claude Code sessions that were already open so they load the hooks. Use
`--no-service` or `--no-hooks` to skip a part.

## Use

```bash
ram-guard status
```

```
level:    OK
memory:   RAM 14.2% used (26.1 GB available of 30.4 GB), swap 36% used, pressure 0.0
limits:   warn 80%, pause 90%, resume 75%, last-resort kill at 97% RAM + 90% swap
daemon:   running (pid 4242)
paused:   nothing
```

| Command | What it does |
|---|---|
| `ram-guard status` | Level, memory, paused processes, recent events. `--json` for scripts |
| `ram-guard top` | Agent sessions and the process trees they started, largest first |
| `ram-guard kill <pid>` | End one agent-started tree. Refuses agent sessions and anything no agent started |
| `ram-guard run --max 4G -- <command>` | Run a command under a memory cap in its own cgroup |
| `ram-guard resume` | Thaw everything now and snooze pausing for two minutes |
| `ram-guard config [key] [value]` | Show or change settings |
| `ram-guard uninstall` | Remove the service, hooks and command |

Logs: `journalctl --user -u ram-guard -f`

## Settings

`ram-guard config pause_pct 85` changes one setting; the daemon reloads within seconds.

| Key | Default | Meaning |
|---|---|---|
| `warn_pct` / `pause_pct` / `resume_pct` | 80 / 90 / 75 | RAM-in-use thresholds |
| `kill_enabled` | true | Set to false for pause-only, never kill |
| `kill_pct` / `kill_swap_pct` | 97 / 90 | Both must be exceeded before anything is killed |
| `psi_warn` / `psi_pause` | 10 / 25 | Memory-pressure stall thresholds, 0 turns them off |
| `min_tree_mb` | 100 | Process trees smaller than this are never paused |
| `max_pause_sec` | 300 | Release a pause after this long if RAM is back under `pause_pct` |
| `hook_wait_sec` | 45 | How long a launch is held before it is denied |
| `agent_names` | `["claude"]` | Process names treated as agents |
| `protect_names` | system services, tmux, screen | Process names that are never paused or killed |
| `protect_cmdline` | MCP servers | Command-line patterns that are never paused or killed |
| `oom_score_adj` | 500 | Kernel OOM preference given to agent-started processes, 0 turns it off |
| `hooks_enabled` / `notify` | true | Turn the hooks or desktop notifications off |

## How it works

- **Monitor.** A small daemon reads `/proc/meminfo` and `/proc/pressure/memory` every two
  seconds, faster under pressure. It finds agent processes by name, treats their
  descendants as agent-started, and groups them into trees, one per tool invocation.
- **Pausing.** At the pause threshold it stops one whole tree and waits. It stops another
  only if usage keeps rising, so the least possible work is frozen. Everything it stopped
  is recorded with its start time, so a recycled pid is never signalled.
- **Hooks.** A `PreToolUse` hook holds Bash, Agent and Workflow calls while memory is
  critical and denies them with instructions if it does not recover. Inspection commands
  and file tools are never blocked, so Claude can still look around and clean up.
  `UserPromptSubmit` and `SessionStart` hooks tell Claude the state at the start of a turn.
- **Failing safe.** If the daemon stops for any reason, everything it paused is resumed.
  If the hook itself fails, it allows the call.

## Test

```bash
bash ~/.claude/skills/ram-guard/tests/selftest.sh
```

The self-test fakes memory readings and watches only a throwaway process it starts
itself, so it is safe to run on a busy machine. It covers warn, pause, resume, manual
resume, last-resort kill, clean shutdown, the hooks, the installer and the memory cap.

## Limits

- Pausing stops growth; it does not free memory. Memory returns when paused work is
  swapped out, finishes, or is killed.
- If programs no agent started hold the memory, ram-guard can only hold agent work and
  notify you.
- A foreground command that is paused can hit Claude's Bash timeout.
- A GUI program an agent launched counts as agent-started. Add its process name to
  `protect_names` to exempt it.

## License

MIT
