#!/bin/sh
# Shell-only trust checks used before the RT-054 Python runner is executed.
# This file intentionally has no interpreter, PATH, or environment fallback.

rt054_stat_record() {
    case "$(/usr/bin/uname -s)" in
        Darwin) /usr/bin/stat -f '%u|%Lp|%HT' -- "$1" ;;
        Linux) /usr/bin/stat -c '%u|%a|%F' -- "$1" ;;
        *) return 1 ;;
    esac
}

# Resolve a link without invoking PATH or accepting a caller-provided tool.
rt054_readlink() {
    case "$(/usr/bin/uname -s)" in
        Darwin) /usr/bin/readlink "$1" ;;
        Linux) /usr/bin/readlink -- "$1" ;;
        *) return 1 ;;
    esac
}

# Print an absolute lexical normalization.  It deliberately has no realpath
# fallback: every resulting component is lstat'ed below before it is trusted.
rt054_normalize_absolute() {
    rt054_normalized=
    rt054_pending=$1
    case "$rt054_pending" in /*) ;; *) return 1 ;; esac
    rt054_pending=${rt054_pending#/}
    while [ -n "$rt054_pending" ]; do
        rt054_component=${rt054_pending%%/*}
        if [ "$rt054_component" = "$rt054_pending" ]; then
            rt054_pending=
        else
            rt054_pending=${rt054_pending#*/}
        fi
        case "$rt054_component" in
            ''|.) ;;
            ..) rt054_normalized=${rt054_normalized%/*} ;;
            *) rt054_normalized=$rt054_normalized/$rt054_component ;;
        esac
    done
    [ -n "$rt054_normalized" ] || rt054_normalized=/
    printf '%s\n' "$rt054_normalized"
}

# Accept only an absolute system executable whose resolved path has at most 16
# links.  Each link itself, every parent visited on every hop, and the final
# regular executable must be root-owned.  A symlink's mode is deliberately not
# a trust input: Unix lstat commonly exposes it as 0777, and it says nothing
# about whether its target is writable.  Directories and the final regular
# executable must be group/other non-writable.
rt054_validate_trust_chain() {
    rt054_candidate=$1
    case "$rt054_candidate" in /*) ;; *) return 1 ;; esac

    rt054_record=$(rt054_stat_record /) || return 1
    IFS='|' read -r rt054_uid rt054_mode rt054_kind <<EOF
$rt054_record
EOF
    [ "$rt054_uid" = 0 ] || return 1
    case "$rt054_mode" in ''|*[!0-7]*) return 1 ;; esac
    [ $((0$rt054_mode & 022)) -eq 0 ] || return 1
    case "$rt054_kind" in directory|Directory) ;; *) return 1 ;; esac

    rt054_pending=$(rt054_normalize_absolute "$rt054_candidate") || return 1
    rt054_current=
    rt054_links=0
    rt054_visited='
'
    while [ -n "$rt054_pending" ]; do
        rt054_remaining=${rt054_pending#/}
        [ -n "$rt054_remaining" ] || return 1
        rt054_component=${rt054_remaining%%/*}
        if [ "$rt054_component" = "$rt054_remaining" ]; then
            rt054_remaining=
        else
            rt054_remaining=${rt054_remaining#*/}
        fi
        rt054_current=$rt054_current/$rt054_component
        rt054_record=$(rt054_stat_record "$rt054_current") || return 1
        IFS='|' read -r rt054_uid rt054_mode rt054_kind <<EOF
$rt054_record
EOF
        [ "$rt054_uid" = 0 ] || return 1
        case "$rt054_kind" in
            'symbolic link'|'Symbolic Link'|symlink)
                rt054_links=$((rt054_links + 1))
                [ "$rt054_links" -le 16 ] || return 1
                case "$rt054_visited" in *"
$rt054_current
"*) return 1 ;; esac
                rt054_visited="${rt054_visited}${rt054_current}
"
                rt054_target=$(rt054_readlink "$rt054_current") || return 1
                case "$rt054_target" in
                    /*) rt054_next=$rt054_target ;;
                    *)
                        rt054_parent=${rt054_current%/*}
                        [ -n "$rt054_parent" ] || rt054_parent=/
                        rt054_next=$rt054_parent/$rt054_target
                        ;;
                esac
                [ -z "$rt054_remaining" ] || rt054_next=$rt054_next/$rt054_remaining
                rt054_pending=$(rt054_normalize_absolute "$rt054_next") || return 1
                rt054_current=
                continue
                ;;
        esac
        case "$rt054_mode" in ''|*[!0-7]*) return 1 ;; esac
        [ $((0$rt054_mode & 022)) -eq 0 ] || return 1
        if [ -n "$rt054_remaining" ]; then
            case "$rt054_kind" in directory|Directory) ;; *) return 1 ;; esac
        else
            case "$rt054_kind" in 'regular file'|'Regular File') ;; *) return 1 ;; esac
            [ -x "$rt054_current" ] || return 1
            RT054_VALIDATED_TARGET=$rt054_current
            return 0
        fi
        rt054_pending=/$rt054_remaining
    done
    return 1
}

rt054_validate_candidate() {
    candidate=$1
    case "$candidate" in /usr/bin/python3) ;; *) return 1 ;; esac
    rt054_validate_trust_chain "$candidate"
}
