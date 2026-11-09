# ram-guard

A Claude Code skill that lets agents use nearly all of your RAM without taking the
machine down. It tells Claude how much work fits before it starts any, freezes a runaway
job at the last moment instead of letting the kernel or systemd-oomd kill your session,
and kills only as a last resort.

It is built to stay out of the way. While memory is plentiful it does nothing, and its
hooks cost about a millisecond per call.

Linux only. One Python file, standard library only, no root needed.

## Why

Agents start heavy things in parallel: builds, test runners, dev servers, more agents.
When RAM runs out, the out-of-memory killer picks a large victim, and that is often the
agent session or the whole desktop app. You lose the conversation along with the work.
ram-guard acts earlier, and it only ever acts on processes the agent itself started.

## What it does

**Before work starts**, Claude gets a memory budget: one line at session start, and a
command it can check before fanning out.

```
ram-guard budget: RAM 18% used, 24.8 GB available. Budget: up to 8 heavy jobs in parallel (about 2.0 GB each, limited by CPU count). Cap a big command: `ram-guard run --max 16G -- <cmd>`.
```

**While work runs**, it steps in late. On a 32 GB machine agents can use about 30 GB
before anything is frozen.

| Level | Default trigger | Action |
|---|---|---|
| warn | 87% RAM used | One short reminder with the budget, at most every five minutes |
| hold | about 1 GB before the pause line | New Bash, Agent and Workflow calls are held, so work is not started only to be frozen |
| critical | 93% RAM used | The fastest-growing agent-started process tree is frozen (SIGSTOP). Another is frozen only if usage keeps rising |
| emergency | 97% RAM and 90% swap used | The single largest agent-started tree is killed |

Frozen work thaws by itself below 88%. Agent sessions, MCP servers, the desktop app and
anything you started yourself are never paused or killed.

What keeps it from slowing Claude down:

- **No overhead when memory is free.** The per-call hooks exit from a shell fast path
  without starting Python.
- **No false alarms.** Memory-pressure stalls only count once RAM is above the warn line,
  so a container thrashing against its own limit does not freeze anything.
- **No stop-start churn.** A job that was just thawed runs for at least 30 seconds before
  it can be frozen again.
- **No notification spam.** A brief pause and a resume show nothing. You get one quiet
  notice if work stays frozen for a minute, and an urgent one only if something is killed.
- **No retry loops.** A blocked call is answered with one instruction: run
  `ram-guard wait`, which blocks until memory recovers and costs no tokens.

When other applications need memory, agents make room: a frozen job stops touching its
memory, so the kernel swaps it out and hands the RAM over.

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
limits:   warn 87%, hold launches 89.6%, pause 93%, resume 88%, last-resort kill at 97% RAM + 90% swap
budget:   up to 8 heavy jobs in parallel (about 2.0 GB each, limited by CPU count). Cap a big command: `ram-guard run --max 16G -- <cmd>`.
daemon:   running (pid 4242)
paused:   nothing
```

| Command | What it does |
|---|---|
| `ram-guard budget` | How much work fits right now. `--need 6G` asks about one job, `--json` for scripts |
| `ram-guard wait` | Block until launches are allowed again. One call instead of a retry loop |
| `ram-guard status` | Level, memory, paused processes, recent events. `--json` for scripts |
| `ram-guard top` | Agent sessions and the process trees they started, largest first |
| `ram-guard kill <pid>` | End one agent-started tree. Refuses agent sessions and anything no agent started |
| `ram-guard run --max 4G -- <command>` | Run a command under a memory cap in its own cgroup |
| `ram-guard resume` | Thaw everything now and snooze pausing for two minutes |
| `ram-guard config [key] [value]` | Show or change settings |
| `ram-guard uninstall` | Remove the service, hooks and command |

Logs: `journalctl --user -u ram-guard -f`

## Settings

`ram-guard config pause_pct 90` changes one setting; the daemon reloads within seconds.
On a machine with 8 GB or less the defaults leave little slack, so lower `pause_pct`.

| Key | Default | Meaning |
|---|---|---|
| `warn_pct` / `pause_pct` / `resume_pct` | 87 / 93 / 88 | RAM-in-use thresholds |
| `launch_reserve_mb` | 1024 | Hold new launches this far before the pause line |
| `job_mb` | 2048 | Assumed size of one heavy job, for the budget |
| `min_run_sec` | 30 | Guaranteed run time after a thaw |
| `kill_enabled` | true | Set to false for pause-only, never kill |
| `kill_pct` / `kill_swap_pct` | 97 / 90 | Both must be exceeded before anything is killed |
| `psi_warn` / `psi_pause` | 10 / 25 | Memory-pressure stall thresholds, counted only above the warn line. 0 turns them off |
| `min_tree_mb` | 100 | Process trees smaller than this are never paused |
| `max_pause_sec` | 300 | Release a pause after this long if RAM is back under `pause_pct` |
| `hook_wait_sec` | 45 | How long a launch is held before it is denied |
| `agent_names` | `["claude"]` | Process names treated as agents |
| `protect_names` | system services, tmux, screen | Process names that are never paused or killed |
| `protect_cmdline` | MCP servers | Command-line patterns that are never paused or killed |
| `oom_score_adj` | 500 | Kernel OOM preference given to agent-started processes, 0 turns it off |
| `notify`, `notify_after_sec`, `notify_gap_sec` | true, 60, 600 | Desktop notices: none for a brief pause, one if work stays frozen this long, at most one per gap. Kills are always announced |
| `hooks_enabled` | true | Turn the hooks off |

## How it works

- **Monitor.** A small daemon reads `/proc/meminfo` and `/proc/pressure/memory` every two
  seconds, faster under pressure. It finds agent processes by name, treats their
  descendants as agent-started, and groups them into trees, one per tool invocation.
- **Pausing.** At the pause threshold it stops one whole tree and waits. It stops another
  only if usage keeps rising, so the least possible work is frozen. Everything it stopped
  is recorded with its start time, so a recycled pid is never signalled.
- **Hooks.** A `SessionStart` hook gives Claude the budget in one line. A `PreToolUse`
  hook holds Bash, Agent and Workflow calls when there is no room and denies them with
  one instruction if memory does not recover. Inspection commands and file tools are
  never blocked, so Claude can still look around and clean up.
- **Fast path.** The daemon publishes the memory level above which the hooks have
  nothing to say. While memory is above it, the hook is a few shell builtins and exits.
- **Failing safe.** If the daemon stops for any reason, everything it paused is resumed.
  If the hook itself fails, it allows the call.

## Test

```bash
bash ~/.claude/skills/ram-guard/tests/selftest.sh
```

The self-test fakes memory readings and watches only a throwaway process it starts
itself, so it is safe to run on a busy machine. It covers the budget, warn, hold, pause,
resume, churn protection, manual resume, last-resort kill, clean shutdown, the hooks and
their fast path, the installer and the memory cap.

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
