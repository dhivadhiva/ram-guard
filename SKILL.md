---
name: ram-guard
description: Protects the machine and Claude from running out of memory while agents work. A background monitor pauses the heaviest agent-started processes when RAM gets critical, holds new launches until it recovers, and only kills as a last resort. Use this skill whenever a message or tool result starts with "ram-guard:", a Bash, Agent or Workflow call is held or blocked for memory, or the user mentions RAM, memory usage, out-of-memory, OOM kills, swapping, the system freezing or lagging, or Claude crashing during agent runs. Also use it before starting memory-heavy work (parallel agents, large builds, big test suites, dev servers, data or ML jobs) and to install, check, tune or troubleshoot the ram-guard monitor, even if the user does not name it.
---

# ram-guard

ram-guard keeps a machine usable while AI agents work on it. Agents are good at starting
many heavy processes at once (parallel builds, test runners, dev servers, more agents), and
when memory runs out the kernel or systemd-oomd kills whatever is biggest, which is often
the agent session or the whole desktop app. ram-guard steps in earlier and more gently:
it pauses work instead of losing it.

It has two parts, both in `scripts/ram_guard.py`:

- **A monitor daemon** (systemd user service `ram-guard`) that reads memory every couple of
  seconds and acts on processes that agents started.
- **Claude Code hooks** that tell you when memory is tight and hold new launches while it
  is critical. Their messages always start with `ram-guard:`.

"Agent-started" means any descendant of a `claude` process. Agent sessions themselves, MCP
servers, the desktop app and everything the user started are never paused or killed.

## Levels

| Level | Default trigger | What ram-guard does |
|---|---|---|
| ok | below 80% RAM | Nothing. Hooks stay silent. |
| warn | 80% RAM used | Hooks tell you memory is tight. Nothing is paused. |
| critical | 90% RAM used, or heavy memory-pressure stalls | Pauses (SIGSTOP) the fastest-growing or largest agent-started process tree. Pauses more only if usage keeps rising. Holds new Bash, Agent and Workflow calls. |
| emergency | 97% RAM used and 90% of swap used | Kills the single largest agent-started tree, at most one every few seconds, until out of the emergency. |

Paused processes resume on their own, smallest first, once usage falls below 75%. If usage
sits between the pause and resume lines for five minutes, they are released anyway so
nothing stays frozen forever.

## How to respond

### "memory is getting tight" (warn)

Nothing is blocked yet. The goal is to avoid reaching 90%, because from there your own
processes get frozen mid-run. Before starting more work:

1. Run `ram-guard top` to see what your work is using.
2. Stop what is no longer needed: finished dev servers, watchers, stale test runners
   (`ram-guard kill <pid>`).
3. Run the remaining work with less parallelism: one agent or build at a time, `-j2`
   instead of `-j$(nproc)`, fewer test workers, smaller batch sizes.
4. Wrap anything heavy as `ram-guard run --max 4G -- <command>`. A capped command that
   outgrows its cap is slowed and then killed inside its own cgroup; nothing else is hurt.

### A tool call was held, then went through

Memory was critical when you made the call and recovered while the hook waited. Carry on,
but treat it as a warn: the machine is close to the edge.

### A tool call was blocked (critical)

The call was held for about 45 seconds and denied because memory did not recover. The
command was not wrong, and retrying it immediately will only be held again.

1. Look first. `ram-guard status`, `ram-guard top`, `ps`, `free` and `kill` are always
   allowed, and so are file reads and edits.
2. If trees you started are large and you no longer need them, free them with
   `ram-guard kill <pid>`.
3. If nothing of yours is large, other applications hold the memory. Say so to the user
   plainly, with the numbers from `ram-guard status`, and wait. Do not try to work around
   the hold.
4. When memory recovers, restart the work in a lighter form (see the warn list).

### Some of your processes are paused

A paused command is frozen, not failed: it continues where it stopped once memory
recovers. Two things follow from that:

- A foreground Bash call whose process is paused can hit the Bash tool timeout. If that
  happens, check `ram-guard status` before assuming the command hung or rerunning it.
- If you need the memory back rather than the work, `ram-guard kill <pid>` ends a paused
  tree cleanly.

`ram-guard resume` thaws everything and turns pausing off for two minutes. It overrides a
safety decision, so use it when the user asks for it, not to get your own work unstuck.

### Something was killed (emergency)

`ram-guard status` shows a `KILLED` line with the command. Tell the user what was lost,
then rerun it capped (`ram-guard run --max ...`) or with less parallelism. Rerunning it
unchanged will most likely end the same way.

## Before memory-heavy work

When the task clearly needs a lot of memory, check first rather than finding out at 90%:

```bash
ram-guard status
```

Then size the work to what is available: cap the heavy command with `ram-guard run`, and
prefer running agents or builds one after another over launching them together.

## Commands

```bash
ram-guard status            # level, memory, paused processes, recent events (--json for scripts)
ram-guard top               # agent sessions and the process trees they started, largest first
ram-guard kill <pid>        # end one agent-started tree (TERM, then KILL); `largest` also works
ram-guard run --max 4G -- <command>   # run under a memory cap; --swap sets the swap cap
ram-guard resume            # thaw everything and snooze pausing for two minutes
ram-guard config            # show settings; `ram-guard config pause_pct 85` changes one
```

`ram-guard kill` refuses agent sessions and anything no agent started, so it cannot be
pointed at the user's own programs by mistake.

## Installing, checking and removing

Install (idempotent, safe to rerun after an update):

```bash
python3 ~/.claude/skills/ram-guard/scripts/ram_guard.py install
```

This writes default settings to `~/.config/ram-guard/config.json`, links the `ram-guard`
command into `~/.local/bin`, enables the `ram-guard` systemd user service, and merges three
hooks into `~/.claude/settings.json` (a backup is kept next to it). Sessions that were
already open may need a restart to load the hooks.

Check that it is healthy:

```bash
ram-guard status
systemctl --user status ram-guard
journalctl --user -u ram-guard -n 50
```

Run the self-test, which fakes memory readings and never touches real sessions:

```bash
bash ~/.claude/skills/ram-guard/tests/selftest.sh
```

Remove it with `ram-guard uninstall`.

## Settings

Edit `~/.config/ram-guard/config.json` or use `ram-guard config <key> <value>`; the daemon
reloads within seconds.

| Key | Default | Meaning |
|---|---|---|
| `warn_pct` / `pause_pct` / `resume_pct` | 80 / 90 / 75 | RAM-in-use thresholds |
| `kill_enabled`, `kill_pct`, `kill_swap_pct` | true, 97, 90 | Last-resort kill. Set `kill_enabled` to false for pause-only |
| `psi_warn` / `psi_pause` | 10 / 25 | Memory-pressure stall thresholds (0 turns them off) |
| `min_tree_mb` | 100 | Trees smaller than this are never paused |
| `max_pause_sec` | 300 | Stale-pause release (0 turns it off) |
| `hook_wait_sec` | 45 | How long a launch is held before it is denied |
| `agent_names` | `["claude"]` | Process names treated as agents |
| `protect_names`, `protect_cmdline` | see `ram-guard config` | Processes never paused or killed |
| `hooks_enabled`, `notify` | true | Turn the hooks or desktop notifications off |

## Limits worth knowing

- Linux only (it reads `/proc` and uses cgroup v2 for `run`).
- Pausing stops a process from growing; it does not shrink it. Memory comes back when
  paused work is swapped out, finishes, or is killed.
- If the memory is held by programs no agent started, ram-guard can only hold agent work
  and notify the user. It will not touch those programs.
- A GUI program an agent launched (an editor, a browser) counts as agent-started. Add its
  process name to `protect_names` if it should never be paused.
