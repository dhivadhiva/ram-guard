#!/bin/sh
# ram-guard hook fast path.
#
# Hooks run before every Bash call, so while memory is plentiful this must cost
# next to nothing. The daemon keeps a "quiet" file holding the MemAvailable value
# (kB) above which the hooks have nothing to say. If memory is above that line we
# exit here: shell builtins only, no Python, no forks. Otherwise, or when the
# daemon is not running, hand over to the full hook.
d="${RAM_GUARD_RUNTIME:-${XDG_RUNTIME_DIR:-/tmp/ram-guard-$(id -u)}/ram-guard}"
if [ -r "$d/quiet" ]; then
    read -r limit < "$d/quiet"
    while read -r key val _; do
        if [ "$key" = "MemAvailable:" ]; then
            [ "$val" -gt "${limit:-999999999999}" ] 2>/dev/null && exit 0
            break
        fi
    done < "${RAM_GUARD_MEMINFO:-/proc/meminfo}"
fi
exec "${RAM_GUARD_PYTHON:-python3}" "${0%/*}/ram_guard.py" hook
