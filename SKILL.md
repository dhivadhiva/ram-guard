---
name: ram-guard
description: Sizes agent work to the memory that is actually free, and protects the machine and Claude from running out of it. Gives a memory budget (how many heavy jobs fit right now), pauses the fastest-growing agent-started job only when RAM is nearly full, and kills only as a last resort. Use this skill whenever a message or tool result starts with "ram-guard:", a Bash, Agent or Workflow call is held or blocked for memory, or the user mentions RAM, memory usage, out-of-memory, OOM kills, swapping, the system freezing or lagging, or Claude crashing or slowing down during agent runs. Also use it before starting memory-heavy or parallel work (several agents, large builds, big test suites, dev servers, data or ML jobs) to check the budget first, and to install, check, tune or troubleshoot the ram-guard monitor, even if the user does not name it.
---

# ram-guard

ram-guard lets agents use nearly all of a machine's memory without tipping it over. When
memory runs out, the kernel or systemd-oomd kills whatever is biggest, often the agent
session or the whole desktop app, and the conversation is lost with it. ram-guard prevents
that in two ways: it tells you how much work fits **before** you start it, and it freezes a
runaway job at the last moment instead of letting the machine die.

It is built to stay out of the way. While memory is plentiful it does nothing, says
nothing, and its hooks cost about a millisecond per call.

Two parts, both in `scripts/`:

- **A monitor daemon** (systemd user service `ram-guard`) that reads memory every couple of
  seconds and acts only on processes that agents started.
- **Claude Code hooks** that state the memory budget at session start and hold new launches
  when there is no room. Their messages always start with `ram-guard:`.

"Agent-started" means any descendant of a `claude` process. Agent sessions themselves, MCP
servers, the desktop app and everything the user started are never paused or killed.

## Plan from the budget

This is the main habit, because it avoids every other situation below. Starting work that
does not fit costs far more than checking: the job gets frozen, the call times out, and
you spend tokens finding out why.

```bash
ram-guard budget
```

```
ram-guard budget: RAM 18% used, 24.8 GB available. Budget: up to 8 heavy jobs in parallel (about 2.0 GB each, limited by CPU count). Cap a big command: `ram-guard run --max 16G -- <cmd>`.
```

- Before fanning out agents, builds or test runs, read the budget and start no more heavy
  jobs at once than it allows. If it says one at a time, run them one after another.
- If you know a job's size, ask directly: `ram-guard budget --need 6G` answers whether it
  fits now. `--per-job 4G` resizes the parallelism estimate.
- Subagents inside a session cost almost nothing until they start processes. A separate
  Claude session costs a few hundred megabytes. The heavy part is what they run.
- For one big command, cap it: `ram-guard run --max 6G -- <command>`. A capped command
  that outgrows its cap is killed inside its own cgroup and nothing else is hurt.

The session-start line already carries the budget, so you often do not need to run the
command at all for the first piece of work.

## Levels

Defaults, as a share of RAM in use. They are deliberately late: on a 32 GB machine agents
can use about 30 GB before anything is frozen.

| Level | Default | What ram-guard does |
|---|---|---|
| ok | below 87% | Nothing. Hooks exit in about a millisecond. |
| warn | 87% | One short reminder with the budget, at most every five minutes. |
| hold | about 1 GB before the pause line | New Bash, Agent and Workflow calls are held, so work is not started only to be frozen. |
| critical | 93% | Freezes (SIGSTOP) the fastest-growing agent-started process tree. Freezes another only if usage keeps rising. |
| emergency | 97% and 90% of swap used | Kills the single largest agent-started tree. |

Frozen jobs thaw by themselves below 88%, smallest first. A job that was just thawed runs
for at least 30 seconds before it can be frozen again, so work never stutters. Memory
pressure stalls are also watched, but only count once RAM is above the warn line.

When other applications need memory, this is how agents make room: the frozen job stops
touching its memory, so the kernel swaps it out and gives the RAM to whoever needs it.

## When ram-guard speaks

### "memory is tight"

Nothing is blocked. Follow the budget in the message: run heavy jobs one after another,
stop dev servers and watchers you no longer need (`ram-guard top`, `ram-guard kill <pid>`),
and cap what remains.

### A call was held, then went through

A short spike cleared while the hook waited. Carry on with low parallelism.

### A call was blocked

The call was fine; there was no room for it. Retrying in a loop wastes tokens and time.

1. Run `ram-guard wait` once. It blocks until memory has recovered and costs no tokens
   while it waits. It gives up after 100 seconds; for longer, pass `--timeout` with a
   matching Bash timeout.
2. If it reports ready, retry the call once, sized to the new budget.
3. If it does not, look: `ram-guard top`. Free jobs of yours that you no longer need with
   `ram-guard kill <pid>`. If nothing of yours is large, other applications hold the
   memory: tell the user so with the numbers, and stop.

Inspection commands (`ram-guard status|top|budget|wait|kill`, `ps`, `free`) and file
reads and edits are never blocked.

### Some of your processes are paused

A paused command is frozen, not failed, and continues where it stopped. A foreground Bash
call whose process is frozen can hit the Bash timeout, so check `ram-guard status` before
assuming a hang or rerunning. If you want the memory back rather than the work, end it
with `ram-guard kill <pid>`.

`ram-guard resume` thaws everything and turns pausing off for two minutes. It overrides a
safety decision, so use it when the user asks for it, not to unstick your own work.

### Something was killed

`ram-guard status` shows a `KILLED` line with the command. Tell the user what was lost,
then rerun it capped or with less parallelism. Unchanged, it will end the same way.

## Commands

```bash
ram-guard budget            # how much work fits now; --need 6G, --per-job 4G, --json
ram-guard wait              # block until launches are allowed again; --timeout, --need
ram-guard status            # level, memory, paused processes, recent events; --json
ram-guard top               # agent sessions and the process trees they started
ram-guard kill <pid>        # end one agent-started tree; `largest` also works
ram-guard run --max 4G -- <command>   # run under a memory cap; --swap sets the swap cap
ram-guard resume            # thaw everything and snooze pausing for two minutes
ram-guard config            # show settings; `ram-guard config pause_pct 90` changes one
```

`ram-guard kill` refuses agent sessions and anything no agent started.

## Installing, checking and removing

```bash
python3 ~/.claude/skills/ram-guard/scripts/ram_guard.py install
```

Idempotent, so rerun it after an update. It writes `~/.config/ram-guard/config.json`,
links the `ram-guard` command into `~/.local/bin`, enables the systemd user service, and
merges three hooks into `~/.claude/settings.json` with a backup beside it.

```bash
ram-guard status
journalctl --user -u ram-guard -n 50
bash ~/.claude/skills/ram-guard/tests/selftest.sh
```

The self-test fakes memory readings and never touches real sessions. Remove everything
with `ram-guard uninstall`.

## Settings

`ram-guard config <key> <value>`; the daemon reloads within seconds.

| Key | Default | Meaning |
|---|---|---|
| `warn_pct` / `pause_pct` / `resume_pct` | 87 / 93 / 88 | RAM-in-use thresholds |
| `launch_reserve_mb` | 1024 | Hold new launches this far before the pause line |
| `job_mb` | 2048 | Assumed size of one heavy job, for the budget |
| `min_run_sec` | 30 | Guaranteed run time after a thaw |
| `kill_enabled`, `kill_pct`, `kill_swap_pct` | true, 97, 90 | Last-resort kill; `kill_enabled false` means pause only |
| `psi_warn` / `psi_pause` | 10 / 25 | Pressure-stall thresholds, 0 turns them off |
| `min_tree_mb` | 100 | Trees smaller than this are never paused |
| `max_pause_sec` | 300 | Release a pause after this long if RAM is under `pause_pct` |
| `hook_wait_sec` | 45 | How long a launch is held before it is denied |
| `agent_names` | `["claude"]` | Process names treated as agents |
| `protect_names`, `protect_cmdline` | see `ram-guard config` | Never paused or killed |
| `hooks_enabled`, `notify` | true | Turn the hooks or desktop notifications off |

On a small machine (8 GB or less) the defaults leave little slack; lower `pause_pct`.

## Limits worth knowing

- Linux only (it reads `/proc` and uses cgroup v2 for `run`).
- Pausing stops a process from growing; memory comes back when the frozen work is
  swapped out, finishes, or is killed.
- If programs no agent started hold the memory, ram-guard can only hold agent work and
  notify the user.
- A GUI program an agent launched counts as agent-started. Add its process name to
  `protect_names` if it should never be paused.
