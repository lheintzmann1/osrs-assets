#!/usr/bin/env bash
# Cold-start a local database: reference data, all aggregate windows, and 365
# days of daily history for every index seed. Takes about a minute, most of it
# the backfill (one request per seed item, throttled to be polite).
#
# Safe to re-run: every collection job upserts.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${OSRS_INDEX_USER_AGENT:-}" ]]; then
    cat >&2 <<'EOF'
OSRS_INDEX_USER_AGENT is not set.

The wiki API asks every client to identify itself with a contact route so the
maintainers can reach you before they block you. Set it:

  export OSRS_INDEX_USER_AGENT='osrs-assets/0.1 - @yourhandle on Discord'

EOF
    exit 1
fi

PYTHON="${PYTHON:-python3}"
export PYTHONPATH="${PYTHONPATH:-src}"

echo "==> collecting reference data and aggregate windows"
$PYTHON -m osrs_index collect

echo "==> backfilling 365d of daily history for index seeds"
$PYTHON -m osrs_index backfill

echo "==> screening"
$PYTHON -m osrs_index screen

echo "==> building indices"
$PYTHON -m osrs_index build

echo
echo "Done. Try:"
echo "  $PYTHON -m osrs_index attack     # manipulation cost by NAV window"
echo "  $PYTHON -m osrs_index status     # collection health"
