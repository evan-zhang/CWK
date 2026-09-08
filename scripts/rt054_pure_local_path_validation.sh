#!/bin/sh
# Shell-only trust checks used before the RT-054 Python runner is executed.
# This file intentionally has no interpreter, PATH, or environment fallback.

rt054_stat_record() {
    case "$(/usr/bin/uname -s)" in
        Darwin) /usr/bin/stat -f '%u|%Lp|%HT' -- "$1" ;;
        Linux) /usr/bin/stat -Lc '%u|%a|%F' -- "$1" ;;
        *) return 1 ;;
    esac
}

# Accept only a non-symlink, absolute system executable whose every lexical
# component is root-owned and not writable by group or other.  Rejecting
# symlinks makes the candidate and resolved target identical; it avoids a
# shell-time resolution race and any operator-controlled fallback path.
rt054_validate_trust_chain() {
    candidate=$1
    case "$candidate" in /*) ;; *) return 1 ;; esac
    [ -L "$candidate" ] && return 1

    # `/` is the fixed root of the trust chain and is checked like every
    # descendant component rather than assumed safe.
    record=$(rt054_stat_record /) || return 1
    IFS='|' read -r uid mode kind <<EOF
$record
EOF
    [ "$uid" = 0 ] || return 1
    case "$mode" in ''|*[!0-7]*) return 1 ;; esac
    [ $((0$mode & 022)) -eq 0 ] || return 1
    case "$kind" in directory|Directory) ;; *) return 1 ;; esac

    current=
    remaining=${candidate#/}
    while [ -n "$remaining" ]; do
        component=${remaining%%/*}
        if [ "$component" = "$remaining" ]; then
            remaining=
        else
            remaining=${remaining#*/}
        fi
        current=$current/$component
        [ -L "$current" ] && return 1
        record=$(rt054_stat_record "$current") || return 1
        IFS='|' read -r uid mode kind <<EOF
$record
EOF
        [ "$uid" = 0 ] || return 1
        case "$mode" in ''|*[!0-7]*) return 1 ;; esac
        # POSIX shell arithmetic accepts the leading-zero octal form.
        [ $((0$mode & 022)) -eq 0 ] || return 1
        if [ -n "$remaining" ]; then
            case "$kind" in directory|Directory) ;; *) return 1 ;; esac
        else
            case "$kind" in 'regular file'|'Regular File') ;; *) return 1 ;; esac
            [ -x "$current" ] || return 1
        fi
    done
}

rt054_validate_candidate() {
    candidate=$1
    case "$candidate" in /usr/bin/python3) ;; *) return 1 ;; esac
    rt054_validate_trust_chain "$candidate"
}
