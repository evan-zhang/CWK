#!/usr/bin/env bash
# Drive CWK wiki summary compile batches until remaining is 0 (or max batches).
set -euo pipefail
cd "$(dirname "$0")/.."
MIRROR="${MIRROR:-../CWK-20260708-001/knowledge/工作协同镜像}"
MODEL="${MODEL:-newapi/BD-MiniMax}"
REPAIR_MODEL="${REPAIR_MODEL:-newapi/BD-glm}"
LIMIT="${LIMIT:-50}"
TIMEOUT="${TIMEOUT:-180}"
MAX_BATCHES="${MAX_BATCHES:-10}"
MAX_PARALLEL="${MAX_PARALLEL:-1}"
REFINE_FALLBACKS="${REFINE_FALLBACKS:-false}"
SYNC_WIKI="${SYNC_WIKI:-false}"
LOG_DIR=runs

mkdir -p "$LOG_DIR"

count_summaries() {
  ls "$MIRROR/wiki/summaries"/*.md 2>/dev/null | wc -l | tr -d ' '
}

manifest_stats() {
  python3 - <<PY
from pathlib import Path
import json
from collections import Counter
m=json.loads(Path("$MIRROR/wiki/_system/manifest.json").read_text())
compiled=len(m.get("compiled_report_ids",[]) or [])
src=int(m.get("source_count") or 530)
fq=m.get("failure_queue") or []
fallback=set(m.get("fallback_report_ids",[]) or [])
terminal={str(x.get("report_id")) for x in fq if x.get("report_id") and int(x.get("attempts",1)) >= 3}
outs=m.get("last_compile_outcomes") or []
print(f"compiled={compiled}")
print(f"source={src}")
print(f"remaining={max(src-compiled,0)}")
print(f"fallback={len(m.get('fallback_report_ids',[]) or [])}")
print(f"fallback_pending={len(fallback-terminal)}")
print(f"terminal_failures={len(terminal)}")
print(f"failures={len(fq)}")
print(f"outcomes={dict(Counter(o.get('status') for o in outs))}")
print(f"last={m.get('last_compile_at')}")
PY
}

sync_wiki() {
  python3 scripts/cwk_sync_mirror_to_docdb.py \
    --mirror-root "$MIRROR" \
    --only-prefix wiki/ \
    --manifest runs/docdb-mirror-sync-manifest.json \
    --retry-queue runs/docdb-sync-retry-queue.json
  python3 - <<'PY'
import json
from pathlib import Path
d=json.loads(Path("runs/docdb-mirror-sync-manifest.json").read_text())
print("sync_counts", d.get("counts"))
PY
}

run_one_batch() {
  local log="$LOG_DIR/compile-batch-$(date +%Y%m%d-%H%M%S).log"
  local refine_args=()
  if [[ "$REFINE_FALLBACKS" == "true" ]]; then
    refine_args+=(--refine-fallbacks)
  fi
  echo "START_BATCH model=$MODEL repair_model=$REPAIR_MODEL limit=$LIMIT max_parallel=$MAX_PARALLEL log=$log"
  python3 scripts/cwk_cloud_wiki_compile.py \
    --mirror-root "$MIRROR" \
    --model "$MODEL" \
    --repair-model "$REPAIR_MODEL" \
    --limit "$LIMIT" \
    --max-parallel "$MAX_PARALLEL" \
    --timeout-seconds "$TIMEOUT" \
    "${refine_args[@]}" \
    >"$log" 2>&1
  local rc=$?
  echo "BATCH_EXIT=$rc summaries=$(count_summaries)"
  manifest_stats
  if [[ "$SYNC_WIKI" == "true" ]]; then
    echo "SYNC_BEGIN"
    sync_wiki
    echo "SYNC_DONE"
  else
    echo "SYNC_SKIPPED"
  fi
  return $rc
}

# If an existing compile is running, wait for it first.
existing=$(pgrep -f 'scripts/cwk_cloud_wiki_compile.py' || true)
if [[ -n "${existing}" ]]; then
  echo "WAIT_EXISTING pid=$existing"
  while pgrep -f 'scripts/cwk_cloud_wiki_compile.py' >/dev/null; do
    echo "$(date +%H:%M:%S) waiting existing compile n=$(count_summaries)"
    sleep 60
  done
  echo "EXISTING_DONE"
  manifest_stats
  if [[ "$SYNC_WIKI" == "true" ]]; then
    sync_wiki
  fi
fi

for b in $(seq 1 "$MAX_BATCHES"); do
  stats=$(manifest_stats)
  echo "$stats"
  rem=$(echo "$stats" | awk -F= '/^remaining=/{print $2}')
  if [[ "$REFINE_FALLBACKS" == "true" && "${rem:-0}" -le 0 ]]; then
    rem=$(echo "$stats" | awk -F= '/^fallback_pending=/{print $2}')
  fi
  if [[ "${rem:-0}" -le 0 ]]; then
    echo "ALL_DONE"
    exit 0
  fi
  echo "BATCH_INDEX=$b remaining=$rem"
  run_one_batch || echo "batch returned non-zero, continuing"
done

echo "MAX_BATCHES_REACHED"
manifest_stats
