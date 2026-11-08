#!/usr/bin/env bash
# ram-guard self-test. Safe to run anywhere: memory readings are faked through
# RAM_GUARD_MEMINFO and only a throwaway "agent" started by this script is watched,
# so real agent sessions and real memory are never touched.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RG="python3 $HERE/scripts/ram_guard.py"
T="$(mktemp -d)"
export RAM_GUARD_CONFIG="$T/config.json" RAM_GUARD_RUNTIME="$T/run" \
       RAM_GUARD_MEMINFO="$T/meminfo" RAM_GUARD_PSI="/dev/null"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
pstate(){ awk '{print $3}' "/proc/$1/stat" 2>/dev/null || echo gone; }
setmem(){ # used_pct swap_used_pct
  local total=32000000 swap=8000000
  printf 'MemTotal: %d kB\nMemFree: 1000 kB\nMemAvailable: %d kB\nSwapTotal: %d kB\nSwapFree: %d kB\n' \
    $total $(( total * (100 - $1) / 100 )) $swap $(( swap * (100 - $2) / 100 )) > "$T/meminfo.new"
  mv "$T/meminfo.new" "$T/meminfo"
}
waitfor(){ # description, seconds, condition
  local end=$(( $(date +%s) + $2 ))
  while [ "$(date +%s)" -le "$end" ]; do eval "$3" && { ok "$1"; return; }; sleep 0.2; done
  bad "$1"
}
cleanup(){
  [ -n "${DPID:-}" ] && kill "$DPID" 2>/dev/null
  [ -n "${APID:-}" ] && pkill -KILL -P "$APID" 2>/dev/null
  [ -n "${APID:-}" ] && kill -KILL "$APID" 2>/dev/null
  rm -rf "$T"
}
trap cleanup EXIT

cat > "$T/config.json" <<EOF
{"agent_names": ["rgfakeagent"], "min_tree_mb": 40, "interval_sec": 0.2, "notify": false,
 "hook_wait_sec": 2, "max_pause_sec": 0, "remind_sec": 300, "snooze_sec": 3}
EOF

# A fake agent with two children: a big one (~200 MB) and a small one (~60 MB).
# (#!/bin/bash, not env: the kernel names the process after the script only when
# the interpreter is exec'd directly, and the daemon finds agents by process name.)
cat > "$T/rgfakeagent" <<'EOF'
#!/bin/bash
python3 -c "import time; x = b'x' * (200 << 20); time.sleep(600)" &
echo $! > "$1/big.pid"
python3 -c "import time; x = b'x' * (60 << 20); time.sleep(600)" &
echo $! > "$1/small.pid"
wait
EOF
chmod +x "$T/rgfakeagent"
setmem 50 10
"$T/rgfakeagent" "$T" & APID=$!
sleep 1.5
BIG=$(cat "$T/big.pid"); SMALL=$(cat "$T/small.pid")
$RG daemon > "$T/daemon.log" 2>&1 & DPID=$!
sleep 1

echo "1. healthy"
check "status reports OK"            '$RG status | grep -q "level:    OK"'
check "daemon is running"            '$RG status | grep -q "daemon:   running"'
check "top lists the fake agent"     '$RG top | grep -q rgfakeagent'
check "hook is silent when healthy"  '[ -z "$(echo "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"make\"},\"session_id\":\"s1\"}" | $RG hook)" ]'

echo "2. warn (85%)"
setmem 85 10; sleep 1
check "status reports WARN"          '$RG status | grep -q "level:    WARN"'
check "nothing is paused at warn"    '[ "$(pstate $BIG)" != T ]'
OUT=$(echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"make"},"session_id":"s1"}' | $RG hook)
check "hook adds a reminder"         'echo "$OUT" | grep -q additionalContext && ! echo "$OUT" | grep -q permissionDecision'
OUT=$(echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"make"},"session_id":"s1"}' | $RG hook)
check "reminder is rate limited"     '[ -z "$OUT" ]'

echo "3. critical (92%)"
setmem 92 10
waitfor "largest tree is paused"     5 '[ "$(pstate $BIG)" = T ]'
sleep 1
check "smaller tree keeps running while usage is flat" '[ "$(pstate $SMALL)" != T ]'
check "the fake agent itself is never paused"          '[ "$(pstate $APID)" != T ]'
S=$(date +%s)
OUT=$(echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"make -j8"},"session_id":"s1"}' | $RG hook)
check "heavy launch is held, then denied" 'echo "$OUT" | grep -q "\"permissionDecision\": \"deny\"" && [ $(( $(date +%s) - S )) -ge 2 ]'
check "inspection command is let through" '[ -z "$(echo "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ram-guard status\"},\"session_id\":\"s1\"}" | $RG hook)" ]'
check "chained command is not treated as light" 'echo "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ps; make\"},\"session_id\":\"s1\"}" | $RG hook | grep -q deny'
check "prompt hook explains the hold" 'echo "{\"hook_event_name\":\"UserPromptSubmit\",\"session_id\":\"s1\"}" | $RG hook | grep -q "on hold"'
setmem 93 10
waitfor "next tree is paused once usage rises" 5 '[ "$(pstate $SMALL)" = T ]'

echo "4. recovery (70%)"
setmem 70 10
waitfor "smallest tree resumes first" 8 '[ "$(pstate $SMALL)" != T ]'
waitfor "largest tree resumes"        8 '[ "$(pstate $BIG)" != T ]'

echo "5. manual resume and snooze"
setmem 92 10
waitfor "paused again"                5 '[ "$(pstate $BIG)" = T ]'
$RG resume > /dev/null
waitfor "ram-guard resume thaws it"   5 '[ "$(pstate $BIG)" != T ]'
sleep 1
check "snooze keeps it running"       '[ "$(pstate $BIG)" != T ]'

echo "6. last resort (98% RAM, swap 95%)"
setmem 98 95
waitfor "largest tree is killed"      6 '[ "$(pstate $BIG)" = gone ] || [ "$(pstate $BIG)" = Z ]'
check "the fake agent survives"       'kill -0 $APID 2>/dev/null'
setmem 50 10; sleep 1.5

echo "7. daemon exit resumes everything"
python3 -c "import time; x = b'x' * (120 << 20); time.sleep(600)" & STRAY=$!   # not agent-started
"$T/rgfakeagent" "$T" & APID2=$!
sleep 3; BIG2=$(cat "$T/big.pid")   # also lets the snooze from step 5 run out
setmem 92 10
waitfor "new tree is paused"          5 '[ "$(pstate $BIG2)" = T ]'
check "a process no agent started is left alone" '[ "$(pstate $STRAY)" != T ]'
kill -TERM "$DPID"; wait "$DPID" 2>/dev/null; DPID=
check "processes run again after the daemon stops" '[ "$(pstate $BIG2)" != T ]'
pkill -KILL -P "$APID2" 2>/dev/null; kill -KILL "$APID2" "$STRAY" 2>/dev/null

echo "8. ram-guard kill"
"$T/rgfakeagent" "$T" & APID3=$!
sleep 1.5; BIG3=$(cat "$T/big.pid")
check "refuses a pid no agent started"   '! $RG kill $$ 2>/dev/null'
check "refuses to kill the agent itself" '! $RG kill $APID3 2>/dev/null && kill -0 $APID3'
$RG kill "$BIG3" > /dev/null
check "kills an agent-started tree"      '[ "$(pstate $BIG3)" = gone ] || [ "$(pstate $BIG3)" = Z ]'
pkill -KILL -P "$APID3" 2>/dev/null; kill -KILL "$APID3" 2>/dev/null

echo "9. install merges into settings.json and uninstall cleans up"
H="$T/home"; mkdir -p "$H/.claude"
echo '{"theme":"dark","hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"echo mine"}]}]}}' > "$H/.claude/settings.json"
inst(){ HOME="$H" XDG_CONFIG_HOME="$H/.config" CLAUDE_CONFIG_DIR="$H/.claude" $RG "$@" --no-service > /dev/null; }
count(){ python3 -c "
import json,sys
s=json.load(open('$H/.claude/settings.json'))
cmds=[h['command'] for g in sum(s.get('hooks',{}).values(),[]) for h in g['hooks']]
print(s.get('theme'), sum('ram_guard.py' in c for c in cmds), sum(c=='echo mine' for c in cmds))"; }
inst install; inst install
check "hooks added once, existing settings kept" '[ "$(count)" = "dark 3 1" ]'
check "settings backup written"                  '[ -f "$H/.claude/settings.json.ram-guard.bak" ]'
check "ram-guard command linked"                 '[ -L "$H/.local/bin/ram-guard" ]'
inst uninstall
check "uninstall removes only its own hooks"     '[ "$(count)" = "dark 0 1" ]'

echo "10. ram-guard run (memory cap)"
if command -v systemd-run >/dev/null && systemd-run --user --scope --quiet true 2>/dev/null; then
  check "a job under its cap finishes" '$RG run --max 300M -- python3 -c "x = b\"x\" * (100 << 20)" 2>/dev/null'
  ( $RG run --max 150M --swap 0 -- python3 -c "x = b'x' * (600 << 20)" ) 2>/dev/null; RC=$?
  check "a job over its cap is killed in its own cgroup" '[ "$RC" -eq 137 ]'
else
  echo "  skip (no systemd user session)"
fi

echo
echo "daemon log:"; sed 's/^/  /' "$T/daemon.log"
echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
