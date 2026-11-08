#!/usr/bin/env python3
"""ram-guard: keep AI coding agents from running the machine out of memory.

One file, Python standard library only, Linux only. Subcommands:

  daemon     watch memory; pause, resume and (last resort) kill agent-started processes
  hook       Claude Code hook entry point (PreToolUse, UserPromptSubmit, SessionStart)
  status     show the memory level, paused processes and recent events
  top        list the heaviest agent-started process trees
  resume     resume everything ram-guard paused and hold off pausing for a while
  kill       kill one agent-started process tree (by pid, or "largest")
  run        run a command under a hard memory cap (systemd scope, cgroup v2)
  config     show or change settings
  install    install the CLI, the systemd user service and the Claude Code hooks
  uninstall  remove them again

What is an "agent-started process"? Any descendant of an agent process (by
default a process named `claude`). The agent itself, the desktop app and
everything else on the machine are never signalled.
"""

import argparse
import collections
import fcntl
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time

VERSION = "1.0.0"
HOME = os.path.expanduser("~")
UID = os.getuid()
MYPID = os.getpid()
SCRIPT = os.path.realpath(__file__)
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


def _env(name, default):
    return os.environ.get(name) or default


CONFIG_PATH = _env("RAM_GUARD_CONFIG", os.path.join(
    _env("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "ram-guard", "config.json"))
RUNTIME_DIR = _env("RAM_GUARD_RUNTIME", os.path.join(
    _env("XDG_RUNTIME_DIR", "/tmp/ram-guard-%d" % UID), "ram-guard"))
STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")
# Overridable so the self-test can simulate memory pressure without causing any.
MEMINFO = _env("RAM_GUARD_MEMINFO", "/proc/meminfo")
PSI = _env("RAM_GUARD_PSI", "/proc/pressure/memory")

DEFAULTS = {
    # Percent of RAM in use (MemTotal - MemAvailable).
    "warn_pct": 80,          # tell the agent to lighten up
    "pause_pct": 90,         # freeze agent-started processes, hold new launches
    "resume_pct": 75,        # thaw them again below this
    # Last resort: kill the largest agent-started tree when RAM *and* swap are nearly gone.
    "kill_enabled": True,
    "kill_pct": 97,
    "kill_swap_pct": 90,
    # Memory pressure stall (PSI "full avg10", percent of time everything stalls). 0 disables.
    "psi_warn": 10,
    "psi_pause": 25,
    "interval_sec": 2,       # polling interval while healthy (faster under pressure)
    "min_tree_mb": 100,      # never bother pausing process trees smaller than this
    "max_pause_sec": 300,    # stale-pause release: thaw after this long if RAM is below pause_pct
    "snooze_sec": 120,       # `ram-guard resume` holds off pausing for this long
    "hook_wait_sec": 45,     # how long a tool call is held before it is denied
    "remind_sec": 300,       # min gap between "memory is tight" reminders per session
    "hooks_enabled": True,
    "notify": True,          # desktop notifications via notify-send
    "oom_score_adj": 500,    # make agent-started processes the kernel's preferred OOM victims; 0 disables
    "agent_names": ["claude"],
    "protect_names": ["ram-guard", "systemd", "dbus-daemon", "dbus-broker", "sshd",
                      "tmux.*", "screen", "gnome-shell", "Xorg", "Xwayland",
                      "plasmashell", "kwin_.*", "pipewire.*", "wireplumber"],
    # Trees whose root command line matches one of these are never paused or killed.
    # The default keeps MCP servers alive, because the agent talks to them constantly.
    "protect_cmdline": [r"(^|[\s/@_.-])mcp([\s/_.-]|$)", "modelcontextprotocol"],
}

SCAN_IDLE_SEC = 10     # process-table scan cadence while healthy
PAUSE_STEP_PCT = 0.3   # pause another tree only if usage rose this much since the last pause
PSI_SETTLE_SEC = 5
RESUME_STEP_SEC = 3
STALE_STEP_SEC = 30
KILL_COOLDOWN_SEC = 3
GROWTH_MIN_KB = 20 * 1024
LEVELS = ("ok", "warn", "critical", "emergency")


# ----------------------------------------------------------------- config

def load_config():
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except (OSError, ValueError) as e:
        user = {}
        cfg["_error"] = "%s: %s" % (CONFIG_PATH, e)
    if isinstance(user, dict):
        for k, v in user.items():
            if k not in DEFAULTS:
                continue
            d = DEFAULTS[k]
            try:
                if isinstance(d, bool):
                    cfg[k] = bool(v)
                elif isinstance(d, (int, float)):
                    cfg[k] = float(v)
                elif isinstance(d, list):
                    cfg[k] = [str(x) for x in v]
            except (TypeError, ValueError):
                pass
    cfg["pause_pct"] = min(max(cfg["pause_pct"], 5), 100)
    cfg["warn_pct"] = min(cfg["warn_pct"], cfg["pause_pct"])
    if cfg["resume_pct"] >= cfg["pause_pct"]:
        cfg["resume_pct"] = cfg["pause_pct"] - 5
    cfg["interval_sec"] = max(cfg["interval_sec"], 0.1)
    return cfg


def save_user_config(user):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    atomic_write(CONFIG_PATH, json.dumps(user, indent=2) + "\n")


def atomic_write(path, text):
    tmp = "%s.tmp.%d" % (path, MYPID)
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ----------------------------------------------------------------- memory

def read_mem():
    vals = {}
    with open(MEMINFO) as f:
        for line in f:
            k, _, rest = line.partition(":")
            if k in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
                vals[k] = int(rest.split()[0])
    total = max(vals.get("MemTotal", 1), 1)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    st, sf = vals.get("SwapTotal", 0), vals.get("SwapFree", 0)
    psi = 0.0
    try:
        with open(PSI) as f:
            for line in f:
                if line.startswith("full"):
                    psi = float(re.search(r"avg10=([\d.]+)", line).group(1))
    except (OSError, ValueError, AttributeError):
        pass
    return {
        "total_kb": total, "avail_kb": avail,
        "used_pct": round(100.0 * (total - avail) / total, 1),
        "swap_total_kb": st, "swap_free_kb": sf,
        # No swap at all counts as "swap exhausted" for the last-resort check.
        "swap_pct": round(100.0 * (st - sf) / st, 1) if st else 100.0,
        "psi": psi,
    }


def level_of(m, cfg):
    psi = m["psi"]
    if cfg["kill_enabled"] and m["used_pct"] >= cfg["kill_pct"] and m["swap_pct"] >= cfg["kill_swap_pct"]:
        return "emergency"
    if m["used_pct"] >= cfg["pause_pct"] or (cfg["psi_pause"] > 0 and psi >= cfg["psi_pause"]):
        return "critical"
    if m["used_pct"] >= cfg["warn_pct"] or (cfg["psi_warn"] > 0 and psi >= cfg["psi_warn"]):
        return "warn"
    return "ok"


def gb(kb):
    return "%.1f GB" % (kb / 1048576.0)


def mb(kb):
    return "%.1f GB" % (kb / 1048576.0) if kb >= 1048576 else "%d MB" % (kb // 1024)


def describe(m):
    swap = "swap %.0f%% used" % m["swap_pct"] if m["swap_total_kb"] else "no swap"
    return "RAM %.1f%% used (%s available of %s), %s, pressure %.1f" % (
        m["used_pct"], gb(m["avail_kb"]), gb(m["total_kb"]), swap, m["psi"])


# ----------------------------------------------------------------- processes

class Proc(object):
    __slots__ = ("pid", "ppid", "comm", "state", "start", "rss_kb")

    def __init__(self, pid, ppid, comm, state, start, rss_kb):
        self.pid, self.ppid, self.comm = pid, ppid, comm
        self.state, self.start, self.rss_kb = state, start, rss_kb


def read_stat(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            s = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    l, r = s.find("("), s.rfind(")")
    if l < 0 or r < 0:
        return None
    f_ = s[r + 2:].split()
    if len(f_) < 22:
        return None
    return Proc(pid, int(f_[1]), s[l + 1:r], f_[0], int(f_[19]), int(f_[21]) * PAGE_KB)


def scan():
    """All of this user's processes, keyed by pid."""
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if os.stat("/proc/" + name).st_uid != UID:
                continue
        except OSError:
            continue
        p = read_stat(int(name))
        if p:
            procs[p.pid] = p
    return procs


def cmdline(pid, limit=None):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return [] if limit is None else ""
    if limit is None:
        return args
    s = " ".join(args)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def swap_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmSwap:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def is_agent(p, names, node_cache):
    if p.comm in names:
        return True
    if p.comm in ("node", "bun", "deno"):
        key = (p.pid, p.start)
        if key not in node_cache:
            hit = False
            for a in cmdline(p.pid)[:3]:
                base = os.path.basename(a)
                if base in names or ("claude-code" in a and base in ("cli.js", "cli.mjs")):
                    hit = True
            node_cache[key] = hit
        return node_cache[key]
    return False


_protect_cache = {}


def protected(comm, cfg):
    key = tuple(cfg["protect_names"])
    if key not in _protect_cache:
        pats = []
        for x in key:
            try:
                pats.append(re.compile(x))
            except re.error:
                pass
        _protect_cache[key] = pats
    return any(p.fullmatch(comm) for p in _protect_cache[key])


_cmd_cache = {}


def cmd_protected(pid, cfg):
    key = tuple(cfg["protect_cmdline"])
    if key not in _cmd_cache:
        pats = []
        for x in key:
            try:
                pats.append(re.compile(x, re.I))
            except re.error:
                pass
        _cmd_cache[key] = pats
    if not _cmd_cache[key]:
        return False
    line = " ".join(cmdline(pid))
    return any(p.search(line) for p in _cmd_cache[key])


def owned_trees(procs, cfg, known, node_cache=None):
    """Group agent-started processes into trees.

    `known` (pid -> starttime) remembers processes seen earlier as agent
    descendants, so one that detaches and gets re-parented stays tracked.
    Returns (trees, owned_pids, agent_pids).
    """
    node_cache = {} if node_cache is None else node_cache
    names = set(cfg["agent_names"])
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    agents = set(pid for pid, p in procs.items() if is_agent(p, names, node_cache))
    owned = set()
    stack = [c for a in agents for c in children.get(a, [])]
    stack += [pid for pid, start in known.items() if pid in procs and procs[pid].start == start]
    while stack:
        pid = stack.pop()
        if pid in owned or pid in agents or pid == MYPID:
            continue
        if protected(procs[pid].comm, cfg):
            continue
        owned.add(pid)
        stack.extend(children.get(pid, []))
    known.clear()
    known.update((pid, procs[pid].start) for pid in owned)
    trees = []
    for root in owned:
        if procs[root].ppid in owned:
            continue
        pids, st = [], [root]
        while st:
            x = st.pop()
            pids.append(x)
            st.extend(c for c in children.get(x, []) if c in owned)
        trees.append({"root": root, "start": procs[root].start, "comm": procs[root].comm,
                      "pids": pids, "rss_kb": sum(procs[x].rss_kb for x in pids)})
    trees.sort(key=lambda t: -t["rss_kb"])
    return trees, owned, agents


def subtree(procs, pid):
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    out, st = [], [pid]
    while st:
        x = st.pop()
        out.append(x)
        st.extend(children.get(x, []))
    return out


def send(pid, start, signo):
    """Signal a process only if it is still the one we looked at."""
    p = read_stat(pid)
    if p is None or p.start != start or p.state == "Z":
        return False
    try:
        os.kill(pid, signo)
        return True
    except OSError:
        return False


def terminate(pairs, grace=3.0):
    """SIGTERM (+SIGCONT so stopped processes see it), then SIGKILL survivors."""
    for pid, start in pairs:
        send(pid, start, signal.SIGTERM)
    for pid, start in pairs:
        send(pid, start, signal.SIGCONT)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(_alive(pid, start) for pid, start in pairs):
            return
        time.sleep(0.1)
    for pid, start in pairs:
        send(pid, start, signal.SIGKILL)


def _alive(pid, start):
    p = read_stat(pid)
    return p is not None and p.start == start and p.state != "Z"


# ----------------------------------------------------------------- state file

def read_state(max_age=None):
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    if max_age is not None and time.time() - st.get("ts", 0) > max_age:
        return None
    return st


def daemon_alive(st):
    if not st or not st.get("daemon_pid"):
        return False
    if time.time() - st.get("ts", 0) > 30:
        return False
    return os.path.exists("/proc/%d" % st["daemon_pid"])


# ----------------------------------------------------------------- daemon

class Daemon(object):
    def __init__(self):
        self.cfg = load_config()
        self.cfg_mtime = self._cfg_mtime()
        self.paused = []            # [{root, comm, cmd, rss_mb, since, pids:[[pid,start],...]}]
        self.known = {}
        self.node_cache = {}
        self.oom_done = set()
        self.events = collections.deque(maxlen=30)
        self.procs, self.trees = {}, []
        self.prev_rss, self.growth = {}, {}
        self.level = "ok"
        self.last_scan = 0.0
        self.last_stop_t = self.last_resume_t = self.last_kill_t = 0.0
        self.last_stop_used = 0.0
        self.snooze_until = 0.0
        self.force_resume = False
        self.stopping = False
        self.noted_nothing = False
        self.kids = []
        self.last_notify = {}
        self.last_err = ("", 0.0)

    # -- helpers
    def _cfg_mtime(self):
        try:
            return os.stat(CONFIG_PATH).st_mtime
        except OSError:
            return None

    def log(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        self.events.append(line)
        print(line, flush=True)

    def notify(self, title, body, urgency="normal"):
        if not self.cfg["notify"] or not shutil.which("notify-send"):
            return
        now = time.time()
        if now - self.last_notify.get(title, 0) < 20:
            return
        self.last_notify[title] = now
        try:
            self.kids.append(subprocess.Popen(
                ["notify-send", "-a", "ram-guard", "-u", urgency, title, body],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except OSError:
            pass  # fork can fail when memory is exhausted; never let that stop the guard

    def reap(self):
        self.kids = [k for k in self.kids if k.poll() is None]

    def maybe_reload(self):
        mt = self._cfg_mtime()
        if mt != self.cfg_mtime:
            self.cfg_mtime = mt
            self.cfg = load_config()
            self.log("config reloaded")

    # -- main loop
    def run(self):
        os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
        self.lock = open(os.path.join(RUNTIME_DIR, "lock"), "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("ram-guard: a daemon is already running", file=sys.stderr)
            return 1
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        signal.set_wakeup_fd(w)
        signal.signal(signal.SIGTERM, self._on_stop)
        signal.signal(signal.SIGINT, self._on_stop)
        signal.signal(signal.SIGUSR1, self._on_usr1)
        signal.signal(signal.SIGHUP, self._on_hup)
        self.adopt()
        c = self.cfg
        self.log("ram-guard %s started: warn %g%% / pause %g%% / resume %g%%, last-resort kill %s" % (
            VERSION, c["warn_pct"], c["pause_pct"], c["resume_pct"],
            "at %g%% RAM + %g%% swap" % (c["kill_pct"], c["kill_swap_pct"]) if c["kill_enabled"] else "off"))
        if c.get("_error"):
            self.log("config error, using defaults: " + c["_error"])
        try:
            while not self.stopping:
                try:
                    lvl = self.tick()
                except Exception as e:  # the guard must outlive its own bugs
                    lvl = "warn"
                    msg, now = repr(e), time.time()
                    if msg != self.last_err[0] or now - self.last_err[1] > 60:
                        self.last_err = (msg, now)
                        self.log("tick error: " + msg)
                if lvl in ("critical", "emergency") or self.paused:
                    delay = min(0.5, self.cfg["interval_sec"])
                elif lvl == "warn":
                    delay = min(1.0, self.cfg["interval_sec"])
                else:
                    delay = self.cfg["interval_sec"]
                try:
                    if select.select([r], [], [], delay)[0]:
                        os.read(r, 512)
                except (InterruptedError, OSError):
                    pass
        finally:
            self.resume_all("ram-guard stopping")
            try:
                self.write_state(read_mem(), time.time(), stopped=True)
            except Exception:
                pass
        return 0

    def _on_stop(self, *_):
        self.stopping = True

    def _on_usr1(self, *_):
        self.force_resume = True

    def _on_hup(self, *_):
        self.cfg_mtime = -1

    def adopt(self):
        """Take over processes a previous daemon left paused."""
        st = read_state()
        for e in (st or {}).get("paused", []):
            pids = []
            for pid, start in e.get("pids", []):
                p = read_stat(pid)
                if p and p.start == start and p.state in "Tt":
                    pids.append([pid, start])
            if pids:
                e["pids"] = pids
                self.paused.append(e)
        if self.paused:
            self.log("adopted %d paused tree(s) from a previous run" % len(self.paused))

    def tick(self):
        now = time.time()
        self.maybe_reload()
        cfg = self.cfg
        m = read_mem()
        raw = level_of(m, cfg)
        self.reap()
        if self.force_resume:
            self.force_resume = False
            self.resume_all("resumed on request; pausing snoozed for %ds" % cfg["snooze_sec"])
            self.snooze_until = now + cfg["snooze_sec"]
        if raw != "ok" or self.paused or now - self.last_scan >= SCAN_IDLE_SEC:
            self.procs = scan()
            self.trees, owned, _ = owned_trees(self.procs, cfg, self.known, self.node_cache)
            self.last_scan = now
            rss = dict((t["root"], t["rss_kb"]) for t in self.trees)
            self.growth = dict((r, v - self.prev_rss.get(r, v)) for r, v in rss.items())
            self.prev_rss = rss
            self.oom_protect(owned)
            self.prune_paused()
        if raw != self.level:
            self.log("level %s -> %s (%s)" % (self.level, raw, describe(m)))
            if raw == "ok":
                self.noted_nothing = False
            self.level = raw
        if raw == "emergency" and now - self.last_kill_t >= KILL_COOLDOWN_SEC:
            self.kill_largest(m, now)
        if raw in ("critical", "emergency") and now >= self.snooze_until:
            self.pause_next(m, raw, now)
        elif self.paused:
            self.maybe_resume(m, raw, now)
        self.write_state(m, now)
        return raw

    # -- actions
    def oom_protect(self, owned):
        adj = int(self.cfg["oom_score_adj"])
        if adj <= 0:
            return
        self.oom_done &= owned
        for pid in owned - self.oom_done:
            self.oom_done.add(pid)
            path = "/proc/%d/oom_score_adj" % pid
            try:
                with open(path) as f:
                    cur = int(f.read().strip())
                if cur < adj:
                    with open(path, "w") as f:
                        f.write(str(adj))
            except (OSError, ValueError):
                pass

    def prune_paused(self):
        keep = []
        for e in self.paused:
            live = [[pid, start] for pid, start in e["pids"]
                    if pid in self.procs and self.procs[pid].start == start
                    and self.procs[pid].state != "Z"]
            if not live:
                self.log("paused tree %d (%s) is gone" % (e["root"], e["comm"]))
                continue
            if time.time() - e["since"] > 2 and not any(self.procs[pid].state in "Tt" for pid, _ in live):
                self.log("paused tree %d (%s) was resumed by someone else" % (e["root"], e["comm"]))
                continue
            e["pids"] = live
            keep.append(e)
        self.paused = keep

    def pause_next(self, m, raw, now):
        cfg = self.cfg
        min_kb = cfg["min_tree_mb"] * 1024
        held = set(pid for e in self.paused for pid, _ in e["pids"])
        cands = [t for t in self.trees
                 if t["rss_kb"] >= min_kb and not (held & set(t["pids"]))
                 and any(self.procs[x].state not in "TtZ" for x in t["pids"])
                 and not cmd_protected(t["root"], cfg)]
        if not cands:
            if not self.paused and not self.noted_nothing:
                self.noted_nothing = True
                self.log("memory is %s but no agent-started process over %d MB is running; "
                         "other applications hold the memory" % (raw, cfg["min_tree_mb"]))
                self.notify("Memory is critically low",
                            "%s. Nothing started by an agent is large enough to pause. "
                            "New agent work is on hold; close other applications." % describe(m),
                            "critical")
            return
        mem_crit = m["used_pct"] >= cfg["pause_pct"]
        psi_crit = cfg["psi_pause"] > 0 and m["psi"] >= cfg["psi_pause"]
        go = (not self.paused or raw == "emergency"
              or (mem_crit and m["used_pct"] >= self.last_stop_used + PAUSE_STEP_PCT)
              or (psi_crit and now - self.last_stop_t >= PSI_SETTLE_SEC))
        if not go:
            return
        # Fastest grower first, otherwise the largest.
        def score(t):
            g = self.growth.get(t["root"], 0)
            return (g if g >= GROWTH_MIN_KB else 0, t["rss_kb"])
        t = max(cands, key=score)
        pids = []
        for pid in t["pids"]:
            p = self.procs[pid]
            if p.state in "TtZ":
                continue  # already stopped by someone else: not ours to resume later
            if send(pid, p.start, signal.SIGSTOP):
                pids.append([pid, p.start])
        if not pids:
            return
        entry = {"root": t["root"], "comm": t["comm"], "cmd": cmdline(t["root"], 100),
                 "rss_mb": t["rss_kb"] // 1024, "since": now, "pids": pids}
        self.paused.append(entry)
        self.last_stop_t, self.last_stop_used = now, m["used_pct"]
        self.log("PAUSED pid %d %s (%s, %d processes): %s" % (
            t["root"], t["comm"], mb(t["rss_kb"]), len(pids), describe(m)))
        self.notify("Paused agent work to protect memory",
                    "%s\nPaused: %s (pid %d, %s). It resumes below %g%%.\n"
                    "`ram-guard status` shows details." % (
                        describe(m), t["comm"], t["root"], mb(t["rss_kb"]), cfg["resume_pct"]),
                    "critical")

    def maybe_resume(self, m, raw, now):
        cfg = self.cfg
        if now - self.last_stop_t < RESUME_STEP_SEC or now - self.last_resume_t < RESUME_STEP_SEC:
            return
        psi_ok = cfg["psi_pause"] <= 0 or m["psi"] < cfg["psi_pause"]
        recovered = m["used_pct"] < cfg["resume_pct"] and psi_ok
        oldest = min(e["since"] for e in self.paused)
        stale = (cfg["max_pause_sec"] > 0 and raw in ("ok", "warn")
                 and now - oldest >= cfg["max_pause_sec"]
                 and now - self.last_resume_t >= STALE_STEP_SEC)
        if not (recovered or stale):
            return
        e = min(self.paused, key=lambda x: x["rss_mb"])  # smallest first
        self.resume_entry(e, "memory recovered" if recovered else
                          "paused %ds with RAM below the pause threshold" % int(now - e["since"]), m)
        self.last_resume_t = now

    def resume_entry(self, e, why, m=None):
        for pid, start in reversed(e["pids"]):
            send(pid, start, signal.SIGCONT)
        if e in self.paused:
            self.paused.remove(e)
        self.log("RESUMED pid %d %s (%s)%s" % (e["root"], e["comm"], why,
                                               ": " + describe(m) if m else ""))
        if not self.paused:
            self.notify("Agent work resumed", "All paused processes are running again.")

    def resume_all(self, why):
        for e in list(self.paused):
            self.resume_entry(e, why)

    def kill_largest(self, m, now):
        best, best_kb = None, 0
        for t in self.trees:
            if cmd_protected(t["root"], self.cfg):
                continue
            kb = t["rss_kb"] + sum(swap_kb(pid) for pid in t["pids"])
            if kb > best_kb:
                best, best_kb = t, kb
        floor_kb = max(50, self.cfg["min_tree_mb"] / 2.0) * 1024
        if best is None or best_kb < floor_kb:
            if not self.noted_nothing:
                self.noted_nothing = True
                self.log("EMERGENCY but nothing agent-started is worth killing; "
                         "other applications hold the memory")
            return
        cmd = cmdline(best["root"], 100)
        n = 0
        for pid in best["pids"]:
            p = self.procs.get(pid)
            if p and send(pid, p.start, signal.SIGKILL):
                n += 1
        dead = set(best["pids"])
        self.paused = [e for e in self.paused if not (dead & set(pid for pid, _ in e["pids"]))]
        self.trees = [t for t in self.trees if t is not best]
        self.last_kill_t = now
        self.log("KILLED pid %d %s (%s, %d processes) as a last resort: %s | %s" % (
            best["root"], best["comm"], mb(best_kb), n, describe(m), cmd))
        self.notify("Killed an agent process to save the system",
                    "%s\nKilled: %s (pid %d, %s)\n%s" % (
                        describe(m), best["comm"], best["root"], mb(best_kb), cmd), "critical")

    def write_state(self, m, now, stopped=False):
        st = {
            "ts": now, "version": VERSION, "daemon_pid": None if stopped else MYPID,
            "level": self.level, "mem": m, "paused": self.paused,
            "snooze_until": self.snooze_until, "events": list(self.events)[-20:],
            "known": dict((str(k), v) for k, v in self.known.items()),
        }
        atomic_write(STATE_PATH, json.dumps(st))


# ----------------------------------------------------------------- hook

LIGHT_CMDS = set("""ram-guard free ps kill pkill pgrep killall uptime vmstat ls cat head tail grep rg
wc pwd echo date df du stat sort cut uniq true sleep""".split())
GIT_READONLY = set("status diff log show branch rev-parse remote".split())


def is_light_command(cmd):
    """Cheap inspection commands stay available so the agent can look around and clean up."""
    if not cmd or len(cmd) > 400 or re.search(r"[;&`<>\n]|\$\(", cmd):
        return False
    for seg in cmd.split("|"):
        try:
            tok = shlex.split(seg)
        except ValueError:
            return False
        if not tok:
            return False
        base = os.path.basename(tok[0])
        if base.startswith("python") and len(tok) > 1 and tok[1].endswith("ram_guard.py"):
            continue
        if base == "git" and len(tok) > 1 and tok[1] in GIT_READONLY:
            continue
        if base not in LIGHT_CMDS:
            return False
    return True


def emit(event, **spec):
    spec["hookEventName"] = event
    sys.stdout.write(json.dumps({"hookSpecificOutput": spec}))
    sys.stdout.flush()


def paused_lines(paused):
    now = time.time()
    return ["  paused: pid %d %s, %d MB, for %ds: %s" % (
        e["root"], e["comm"], e["rss_mb"], int(now - e["since"]), e.get("cmd", ""))
        for e in paused]


def tree_lines(cfg, st, limit=5):
    try:
        procs = scan()
        known = dict((int(k), v) for k, v in ((st or {}).get("known") or {}).items())
        trees, _, _ = owned_trees(procs, cfg, known)
    except Exception:
        return []
    return ["  pid %d  %s  %s" % (t["root"], mb(t["rss_kb"]).rjust(8), cmdline(t["root"], 90))
            for t in trees[:limit] if t["rss_kb"] >= 20 * 1024]


def due_reminder(session, gap):
    try:
        os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(RUNTIME_DIR, "remind-" + re.sub(r"[^A-Za-z0-9_-]", "_", session)[:80])
        try:
            if time.time() - os.stat(path).st_mtime < gap:
                return False
        except OSError:
            pass
        with open(path, "w"):
            pass
        os.utime(path, None)
    except OSError:
        pass
    return True


def hook_view(cfg):
    m = read_mem()
    lvl = level_of(m, cfg)
    st = read_state(max_age=15)
    paused = (st or {}).get("paused") or []
    snoozed = time.time() < (st or {}).get("snooze_until", 0)
    hold = lvl == "emergency" or (not snoozed and (
        lvl == "critical" or (paused and m["used_pct"] >= cfg["resume_pct"])))
    return m, lvl, st, paused, hold


WARN_ADVICE = (
    "Lighten the load before starting more work: run agents and builds one at a time, "
    "lower parallelism (for example -j2, fewer test workers), stop dev servers and watchers "
    "you no longer need, and wrap heavy commands as `ram-guard run --max 4G -- <command>` "
    "so they cannot take the whole machine. `ram-guard top` lists what your work is using.")


def hook_main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cfg = load_config()
    if not cfg["hooks_enabled"]:
        return 0
    event = str(data.get("hook_event_name") or "")
    session = str(data.get("session_id") or "nosession")
    m, lvl, st, paused, hold = hook_view(cfg)

    if event == "PreToolUse":
        tool = str(data.get("tool_name") or "tool")
        if not hold:
            if (lvl == "warn" or paused) and due_reminder(session, cfg["remind_sec"]):
                emit(event, additionalContext="\n".join(
                    ["ram-guard: memory is getting tight. " + describe(m) + ".",
                     "At %g%% ram-guard pauses the heaviest processes you started. %s" % (
                         cfg["pause_pct"], WARN_ADVICE)] + paused_lines(paused)))
            return 0
        if tool == "Bash" and is_light_command(str((data.get("tool_input") or {}).get("command") or "")):
            return 0
        t0 = time.time()
        while time.time() - t0 < cfg["hook_wait_sec"]:
            time.sleep(1)
            m, lvl, st, paused, hold = hook_view(cfg)
            if not hold:
                emit(event, additionalContext=(
                    "ram-guard: this call was held %ds because memory was critical; it recovered "
                    "(%s) so the call is going ahead. %s" % (int(time.time() - t0), describe(m), WARN_ADVICE)))
                return 0
        lines = [
            "ram-guard: memory is critical, so this %s call was held %ds and then blocked to keep "
            "the system and Claude from crashing. Nothing is wrong with the call itself." % (
                tool, int(time.time() - t0)),
            describe(m) + ". Launches resume below %g%%." % cfg["resume_pct"],
        ]
        lines += paused_lines(paused)
        trees = tree_lines(cfg, st)
        if trees:
            lines.append("Largest process trees started by agents:")
            lines += trees
        lines.append(
            "What to do: 1) do not retry heavy work straight away; 2) free memory you own with "
            "`ram-guard kill <pid>` for anything above you no longer need (inspection commands such "
            "as `ram-guard status`, `ram-guard top`, ps, free and kill are always allowed); 3) if "
            "nothing of yours is large, other applications hold the memory: tell the user and wait; "
            "4) when you retry, use less memory (one job at a time, lower parallelism, or "
            "`ram-guard run --max 4G -- <command>`). File reads and edits are not blocked.")
        emit(event, permissionDecision="deny", permissionDecisionReason="\n".join(lines))
        return 0

    if event in ("UserPromptSubmit", "SessionStart") and (lvl != "ok" or paused):
        head = {"warn": "memory is getting tight", "critical": "memory is critical",
                "emergency": "memory is nearly exhausted"}.get(lvl, "some of your processes are paused")
        lines = ["ram-guard: %s. %s." % (head, describe(m))] + paused_lines(paused)
        if hold:
            lines.append("New Bash, agent and workflow launches are on hold until usage falls below "
                         "%g%%. Free memory first (`ram-guard top`, `ram-guard kill <pid>`) or tell the "
                         "user what is using it." % cfg["resume_pct"])
        else:
            lines.append(WARN_ADVICE)
        emit(event, additionalContext="\n".join(lines))
    return 0


# ----------------------------------------------------------------- CLI commands

def cmd_status(args):
    cfg = load_config()
    m = read_mem()
    st = read_state()
    alive = daemon_alive(st)
    lvl = level_of(m, cfg)
    if args.json:
        print(json.dumps({"level": lvl, "mem": m, "daemon_running": alive,
                          "paused": (st or {}).get("paused", []) if alive else [],
                          "events": (st or {}).get("events", []),
                          "thresholds": dict((k, cfg[k]) for k in (
                              "warn_pct", "pause_pct", "resume_pct", "kill_enabled",
                              "kill_pct", "kill_swap_pct"))}, indent=2))
        return 0
    print("level:    %s" % lvl.upper())
    print("memory:   %s" % describe(m))
    print("limits:   warn %g%%, pause %g%%, resume %g%%, last-resort kill %s" % (
        cfg["warn_pct"], cfg["pause_pct"], cfg["resume_pct"],
        "at %g%% RAM + %g%% swap" % (cfg["kill_pct"], cfg["kill_swap_pct"]) if cfg["kill_enabled"] else "off"))
    print("daemon:   %s" % ("running (pid %d)" % st["daemon_pid"] if alive else
                            "NOT running (hooks still hold launches; nothing gets paused)"))
    if cfg.get("_error"):
        print("config:   ERROR, using defaults: %s" % cfg["_error"])
    paused = (st or {}).get("paused", []) if alive else []
    if paused:
        print("paused:")
        for line in paused_lines(paused):
            print(line.replace("  paused: ", "  "))
    else:
        print("paused:   nothing")
    if alive and time.time() < st.get("snooze_until", 0):
        print("snoozed:  pausing is off for another %ds" % int(st["snooze_until"] - time.time()))
    ev = (st or {}).get("events", [])
    if ev:
        print("recent events:")
        for e in ev[-8:]:
            print("  " + e)
    return 0


def _trees_now(cfg):
    st = read_state()
    procs = scan()
    known = dict((int(k), v) for k, v in ((st or {}).get("known") or {}).items())
    trees, owned, agents = owned_trees(procs, cfg, known)
    return st, procs, trees, agents


def cmd_top(args):
    cfg = load_config()
    st, procs, trees, agents = _trees_now(cfg)
    held = set(pid for e in (st or {}).get("paused", []) for pid, _ in e["pids"])
    print(describe(read_mem()))
    print("\nagent sessions (never paused or killed):")
    for pid in sorted(agents, key=lambda x: -procs[x].rss_kb)[:args.n]:
        print("  pid %-8d %8s  %s" % (pid, mb(procs[pid].rss_kb), cmdline(pid, 70)))
    if not agents:
        print("  none found (agent_names: %s)" % ", ".join(cfg["agent_names"]))
    print("\nprocess trees started by agents (pause and kill candidates; 'keep' = protected):")
    for t in trees[:args.n]:
        flag = "PAUSED" if held & set(t["pids"]) else ("keep" if cmd_protected(t["root"], cfg) else "")
        print("  pid %-8d %8s  %2d proc  %-6s %s" % (
            t["root"], mb(t["rss_kb"]), len(t["pids"]), flag, cmdline(t["root"], 90)))
    if not trees:
        print("  none")
    return 0


def cmd_resume(args):
    st = read_state()
    if not args.direct and daemon_alive(st):
        os.kill(st["daemon_pid"], signal.SIGUSR1)
        print("asked the daemon to resume everything; pausing is snoozed for %ds" %
              load_config()["snooze_sec"])
        return 0
    n = 0
    for e in (st or {}).get("paused", []):
        for pid, start in reversed(e.get("pids", [])):
            n += bool(send(pid, start, signal.SIGCONT))
    print("resumed %d process(es) directly" % n)
    return 0


def cmd_kill(args):
    cfg = load_config()
    st, procs, trees, agents = _trees_now(cfg)
    if args.target == "largest":
        if not trees:
            print("nothing started by an agent is running", file=sys.stderr)
            return 1
        tree = trees[0]
        pids = tree["pids"]
    else:
        try:
            pid = int(args.target)
        except ValueError:
            print("target must be a pid or 'largest'", file=sys.stderr)
            return 2
        tree = next((t for t in trees if pid in t["pids"]), None)
        if tree:
            pids = tree["pids"]
        elif pid in agents:
            print("pid %d is an agent session; ram-guard never kills those" % pid, file=sys.stderr)
            return 1
        elif args.force and pid in procs:
            pids = subtree(procs, pid)
        else:
            print("pid %d was not started by an agent (use --force to kill it anyway)" % pid,
                  file=sys.stderr)
            return 1
    kb = sum(procs[x].rss_kb for x in pids if x in procs)
    root = pids[0]
    desc = cmdline(root, 90)
    terminate([(x, procs[x].start) for x in pids if x in procs])
    print("killed pid %d and %d child process(es), about %s freed: %s" % (
        root, len(pids) - 1, mb(kb), desc))
    return 0


def parse_size(s, total_kb):
    mm = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgGtT%]?)[iI]?[bB]?\s*", s)
    if not mm:
        raise ValueError("bad size %r (examples: 4G, 800M, 25%%)" % s)
    n, unit = float(mm.group(1)), mm.group(2).upper()
    if unit == "%":
        return int(total_kb * 1024 * n / 100)
    return int(n * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[unit])


def cmd_run(args):
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: ram-guard run [--max 4G] [--swap 2G] -- command [args...]", file=sys.stderr)
        return 2
    m = read_mem()
    try:
        limit = parse_size(args.max, m["total_kb"]) if args.max else max(512 << 20, m["avail_kb"] * 512)
        swap = parse_size(args.swap, m["total_kb"]) if args.swap else limit // 2
    except ValueError as e:
        print("ram-guard: %s" % e, file=sys.stderr)
        return 2
    sr = shutil.which("systemd-run")
    if not sr:
        print("ram-guard: systemd-run not found, running without a memory cap", file=sys.stderr)
        os.execvp(cmd[0], cmd)
    print("ram-guard: memory cap %s (+%s swap) for: %s" % (
        mb(limit // 1024), mb(swap // 1024), " ".join(cmd)), file=sys.stderr)
    os.execv(sr, [sr, "--user", "--scope", "--quiet", "--collect",
                  "-p", "MemoryMax=%d" % limit, "-p", "MemoryHigh=%d" % int(limit * 0.9),
                  "-p", "MemorySwapMax=%d" % swap, "--"] + cmd)


def cmd_config(args):
    cfg = load_config()
    if args.key is None:
        print("# %s%s" % (CONFIG_PATH, "" if os.path.exists(CONFIG_PATH) else " (not created yet; defaults shown)"))
        print(json.dumps(dict((k, cfg[k]) for k in DEFAULTS), indent=2))
        return 0
    if args.key not in DEFAULTS:
        print("unknown setting %r; known: %s" % (args.key, ", ".join(sorted(DEFAULTS))), file=sys.stderr)
        return 2
    if args.value is None:
        print(json.dumps(cfg[args.key]))
        return 0
    d = DEFAULTS[args.key]
    try:
        if isinstance(d, bool):
            if args.value.lower() not in ("true", "false", "on", "off", "yes", "no", "1", "0"):
                raise ValueError("expected true or false")
            val = args.value.lower() in ("true", "on", "yes", "1")
        elif isinstance(d, list):
            val = [x.strip() for x in args.value.split(",") if x.strip()]
        else:
            val = float(args.value)
            val = int(val) if val == int(val) else val
    except ValueError as e:
        print("bad value for %s: %s" % (args.key, e), file=sys.stderr)
        return 2
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        if not isinstance(user, dict):
            user = {}
    except (OSError, ValueError):
        user = {}
    user[args.key] = val
    save_user_config(user)
    print("%s = %s (the daemon picks this up within a few seconds)" % (args.key, json.dumps(val)))
    return 0


# ----------------------------------------------------------------- install

HOOK_RE = re.compile(r"ram_guard\.py['\"]? hook")
UNIT_NAME = "ram-guard.service"


def _unit_path():
    return os.path.join(_env("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "systemd", "user", UNIT_NAME)


def _settings_path():
    return os.path.join(_env("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude")), "settings.json")


def _bin_link():
    return os.path.join(HOME, ".local", "bin", "ram-guard")


def _systemctl(*a):
    return subprocess.run(["systemctl", "--user"] + list(a), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)


def _strip_hooks(settings):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                kept = [h for h in g["hooks"]
                        if not (isinstance(h, dict) and HOOK_RE.search(str(h.get("command", ""))))]
                removed += len(g["hooks"]) - len(kept)
                if not kept:
                    continue
                g = dict(g, hooks=kept)
            new_groups.append(g)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def _load_settings(path):
    try:
        with open(path) as f:
            s = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(s, dict):
        raise ValueError("top level is not an object")
    return s


def cmd_install(args):
    py = shutil.which("python3") or sys.executable
    print("ram-guard %s installing from %s" % (VERSION, SCRIPT))
    os.chmod(SCRIPT, os.stat(SCRIPT).st_mode | 0o111)

    if not os.path.exists(CONFIG_PATH):
        save_user_config(dict((k, DEFAULTS[k]) for k in (
            "warn_pct", "pause_pct", "resume_pct", "kill_enabled", "kill_pct", "kill_swap_pct")))
        print("  config     created %s" % CONFIG_PATH)
    else:
        print("  config     kept    %s" % CONFIG_PATH)

    link = _bin_link()
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.islink(link) or not os.path.exists(link):
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(SCRIPT, link)
        print("  command    %s" % link)
        if os.path.dirname(link) not in os.environ.get("PATH", "").split(":"):
            print("             note: %s is not on your PATH" % os.path.dirname(link))
    else:
        print("  command    skipped, %s exists and is not a symlink" % link)

    if args.no_service:
        print("  service    skipped (--no-service)")
    elif not shutil.which("systemctl"):
        print("  service    skipped, systemctl not found. Start it yourself: ram-guard daemon")
    else:
        unit = _unit_path()
        os.makedirs(os.path.dirname(unit), exist_ok=True)
        atomic_write(unit, "\n".join([
            "[Unit]",
            "Description=ram-guard: pause agent work before the machine runs out of memory",
            "",
            "[Service]",
            "Type=simple",
            'ExecStart="%s" -u "%s" daemon' % (py, SCRIPT),
            'ExecStopPost="%s" "%s" resume --direct' % (py, SCRIPT),
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]))
        _systemctl("daemon-reload")
        r = _systemctl("enable", UNIT_NAME)
        r2 = _systemctl("restart", UNIT_NAME)
        if r.returncode or r2.returncode:
            print("  service    FAILED: %s %s" % (r.stdout.strip(), r2.stdout.strip()))
        else:
            print("  service    enabled and started (%s)" % unit)

    if args.no_hooks:
        print("  hooks      skipped (--no-hooks)")
    else:
        path = _settings_path()
        try:
            settings = _load_settings(path)
        except (OSError, ValueError) as e:
            print("  hooks      FAILED: could not parse %s (%s); file left untouched" % (path, e))
            return 1
        backup = None
        if os.path.exists(path) and not _strip_hooks(json.loads(json.dumps(settings))):
            # Only back up a file that does not have our hooks yet, so a re-install
            # never replaces the pre-install copy with an already modified one.
            backup = path + ".ram-guard.bak"
            shutil.copy2(path, backup)
        _strip_hooks(settings)
        command = "%s %s hook" % (shlex.quote(py), shlex.quote(SCRIPT))
        hooks = settings.setdefault("hooks", {})
        hooks.setdefault("PreToolUse", []).append({
            "matcher": "Bash|Agent|Task|Workflow",
            "hooks": [{"type": "command", "command": command,
                       "timeout": int(load_config()["hook_wait_sec"]) + 15,
                       "statusMessage": "ram-guard checking memory"}]})
        for event in ("UserPromptSubmit", "SessionStart"):
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write(path, json.dumps(settings, indent=2) + "\n")
        print("  hooks      added to %s%s" % (path, " (backup: %s)" % backup if backup else ""))
        print("             sessions that are already open pick the hooks up after a restart")
    print("done. Try: ram-guard status")
    return 0


def cmd_uninstall(args):
    if not args.no_service:
        if shutil.which("systemctl"):
            _systemctl("disable", "--now", UNIT_NAME)
        cmd_resume(argparse.Namespace(direct=True))
        unit = _unit_path()
        if os.path.exists(unit):
            os.unlink(unit)
            if shutil.which("systemctl"):
                _systemctl("daemon-reload")
            print("removed service")
    path = _settings_path()
    try:
        settings = _load_settings(path)
        if _strip_hooks(settings):
            atomic_write(path, json.dumps(settings, indent=2) + "\n")
            print("removed hooks from %s" % path)
    except (OSError, ValueError) as e:
        print("could not update %s: %s" % (path, e))
    link = _bin_link()
    if os.path.islink(link) and os.path.realpath(link) == SCRIPT:
        os.unlink(link)
        print("removed %s" % link)
    print("kept config %s and the skill folder; delete them by hand if you want" % CONFIG_PATH)
    return 0


# ----------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="ram-guard", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version="ram-guard " + VERSION)
    sub = ap.add_subparsers(dest="sub")
    sub.add_parser("daemon", help="run the monitor in the foreground")
    sub.add_parser("hook", help="Claude Code hook entry point (reads JSON on stdin)")
    p = sub.add_parser("status", help="memory level, paused processes, recent events")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("top", help="heaviest agent-started process trees")
    p.add_argument("-n", type=int, default=10)
    p = sub.add_parser("resume", help="resume everything paused and snooze pausing")
    p.add_argument("--direct", action="store_true", help="signal processes directly, bypassing the daemon")
    p = sub.add_parser("kill", help="kill an agent-started process tree")
    p.add_argument("target", help="pid, or 'largest'")
    p.add_argument("--force", action="store_true", help="allow a pid that no agent started")
    p = sub.add_parser("run", help="run a command under a memory cap")
    p.add_argument("--max", help="RAM cap, e.g. 4G, 800M, 25%% (default: half of available RAM)")
    p.add_argument("--swap", help="swap cap (default: half of --max)")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p = sub.add_parser("config", help="show settings, or set one: config pause_pct 85")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p = sub.add_parser("install", help="install CLI, systemd user service and Claude Code hooks")
    p.add_argument("--no-service", action="store_true")
    p.add_argument("--no-hooks", action="store_true")
    p = sub.add_parser("uninstall", help="remove service, hooks and CLI link")
    p.add_argument("--no-service", action="store_true", help="leave the systemd service alone")
    args = ap.parse_args(argv)

    if args.sub == "hook":
        try:
            return hook_main()
        except Exception:
            return 0  # a broken guard must never block the agent
    if args.sub == "daemon":
        return Daemon().run()
    fn = {"status": cmd_status, "top": cmd_top, "resume": cmd_resume, "kill": cmd_kill,
          "run": cmd_run, "config": cmd_config, "install": cmd_install,
          "uninstall": cmd_uninstall}.get(args.sub)
    if fn is None:
        ap.print_help()
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
