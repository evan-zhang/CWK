#!/bin/sh
# Closed first-process launcher for RT-054's pure-local acceptance gate.
set -eu

for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -f "$candidate" ] && [ -x "$candidate" ] || continue
    resolved=$candidate
    while [ -L "$resolved" ]; do
        link=$(/usr/bin/readlink "$resolved")
        case "$link" in
            /*) resolved=$link ;;
            *) resolved=${resolved%/*}/$link ;;
        esac
    done
    resolved_dir=${resolved%/*}
    resolved_base=${resolved##*/}
    resolved_dir=$(CDPATH= cd -P "$resolved_dir" && pwd -P)
    resolved=$resolved_dir/$resolved_base
    [ -f "$resolved" ] && [ -x "$resolved" ] || continue
    case "$resolved" in
        /opt/homebrew/*|/usr/local/*|/usr/bin/*|/System/Library/*|/Library/Frameworks/*) ;;
        *) continue ;;
    esac
    launcher_dir=$(CDPATH= cd -P "${0%/*}" && pwd -P)
    project=${launcher_dir%/scripts}
    [ -f "$project/scripts/rt054_pure_local.py" ] || exit 127
    exec "$resolved" -I -S "$project/scripts/rt054_pure_local.py"
done

exit 127
