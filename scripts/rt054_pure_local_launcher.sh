#!/bin/sh
# Closed first-process launcher for RT-054's pure-local acceptance gate.
set -eu

launcher_dir=$(CDPATH= cd -P "${0%/*}" && pwd -P)
project=${launcher_dir%/scripts}
[ -f "$project/scripts/rt054_pure_local.py" ] || exit 127
[ -f "$project/scripts/rt054_pure_local_path_validation.sh" ] || exit 127
. "$project/scripts/rt054_pure_local_path_validation.sh"

# Do not accept Homebrew, /usr/local, or PATH.  The fixed spelling is resolved
# only through a bounded, root-owned trust chain; exec uses that final target.
candidate=/usr/bin/python3
rt054_validate_candidate "$candidate" || exit 127
exec "$RT054_VALIDATED_TARGET" -I -S "$project/scripts/rt054_pure_local.py"

exit 127
