#!/usr/bin/env bash
# =============================================================================
# aodw-check.sh — CWK 的仓库级 AODW 自检入口（RT-029 建立）
# =============================================================================
# 为什么需要它：AODW 自带三个互不相干的检查入口（框架 fixture、逐 RT 门禁、
# 宿主 skill 安装状态），此前没有任何地方把它们串起来，于是本地和 CI 各跑各的，
# 谁也说不清「AODW 这一层现在是绿的吗」。本脚本就是那个唯一答案。
#
# 用法：
#   make aodw-check                         # 推荐入口
#   bash .aodw-next/06-project/aodw-check.sh [--root <dir>]
#
# 检查项（硬失败 = 影响退出码）：
#   1. 框架 fixture 套件                          硬
#   2. 受管 RT 的 rt-guard 门禁                   硬（error 级判据）
#   3. RT 花名册一致性：RT/ 目录 ↔ RT/index.yaml  硬
#   4. 宿主 skill 安装状态                        告警（属本机状态，见下）
#
# 为什么第 4 项只告警：`.agent/skills/` 下的入口是符号链接、且已在 .gitignore 里
# （源在 .aodw-next/skills/ 受控，两处都跟踪会重复）。全新 checkout 和 CI 里它
# 必然不存在——把它做成硬失败等于要求 CI 去装本机开发工具，是错的。
#
# 为什么门禁面不是全部 RT：见 .aodw-next/project.yaml 的 rt_gate_scope。
# 想做全量人工体检：bash .aodw-next/tools/rt-guard.sh --root .
#
# 退出码：0 全绿 / 1 有硬失败 / 2 用法或内部错误
# =============================================================================
set -uo pipefail

ROOT="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

[[ -d "$ROOT/.aodw-next" ]] || { echo "aodw-check: $ROOT 下没有 .aodw-next/" >&2; exit 2; }
ROOT="$(cd "$ROOT" && pwd)"

AODW="$ROOT/.aodw-next"
INDEX="$ROOT/RT/index.yaml"
PROJECT_YAML="$AODW/project.yaml"

hard_fail=0
warned=0

section() { printf '\n=== %s ===\n' "$1"; }
fail()    { printf 'aodw-check: [FAIL] %s\n' "$1" >&2; hard_fail=1; }
warn()    { printf 'aodw-check: [WARN] %s\n'  "$1" >&2; warned=1; }
ok()      { printf 'aodw-check: [ok]   %s\n'  "$1"; }

# ── 1. 框架 fixture ──────────────────────────────────────────────────────────
section "1/4 AODW 框架 fixture"
if [[ -f "$AODW/tools/fixtures/run-fixtures.sh" ]]; then
  fixture_out="$(bash "$AODW/tools/fixtures/run-fixtures.sh" 2>&1)"
  fixture_code=$?
  printf '%s\n' "$fixture_out" | tail -1
  if [[ $fixture_code -eq 0 ]]; then
    ok "框架 fixture 套件通过"
  else
    printf '%s\n' "$fixture_out" | grep -E '^FAIL' >&2 || true
    fail "框架 fixture 套件未通过（exit=$fixture_code）——判据行为已偏离预期，先修这里"
  fi
else
  fail "找不到 $AODW/tools/fixtures/run-fixtures.sh"
fi

# ── 2. 受管 RT 门禁 ──────────────────────────────────────────────────────────
# managed_from 从 project.yaml 读；用 sed 而不是 YAML 库，CI 的干净解释器没有
# PyYAML，本脚本不该因此挂掉（rt-guard 自己也是同一个理由退回子集解析）。
section "2/4 受管 RT 门禁"
managed_from="$(sed -n 's/^[[:space:]]*managed_from:[[:space:]]*\(RT-[0-9]\{3\}\).*$/\1/p' "$PROJECT_YAML" | head -1)"
if [[ -z "$managed_from" ]]; then
  fail "$PROJECT_YAML 里读不到 rt_gate_scope.managed_from——门禁面无从确定，fail closed"
else
  from_num="${managed_from#RT-}"
  from_num="$((10#$from_num))"
  managed=()
  for d in "$ROOT"/RT/RT-*; do
    [[ -d "$d" ]] || continue
    rt="$(basename "$d")"
    n="${rt#RT-}"
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    (( 10#$n >= from_num )) && managed+=("$rt")
  done
  if [[ ${#managed[@]} -eq 0 ]]; then
    fail "受管 RT 为空（managed_from=$managed_from）——门禁面为空说明配置错了，不是通过"
  else
    ok "门禁面：${managed[*]}（managed_from=$managed_from；更早的 RT 是接入前存量，只作证据）"
    for rt in "${managed[@]}"; do
      if bash "$AODW/tools/rt-guard.sh" --root "$ROOT" --rt "$rt" >/dev/null 2>&1; then
        ok "$rt 门禁通过（error 级判据全过）"
      else
        code=$?
        fail "$rt 门禁未过（exit=$code）——重跑看明细：bash .aodw-next/tools/rt-guard.sh --root . --rt $rt"
      fi
    done
  fi
fi

# ── 3. RT 花名册一致性 ───────────────────────────────────────────────────────
# 编号规则取「目录 ∪ 索引」的最大序号 +1。索引漏号会直接导致撞号，宪章记了
# DI-011 的实证：撞号两起、复发三次、还丢过一个 RT。所以这条是硬失败。
section "3/4 RT 花名册一致性（RT/ 目录 ↔ RT/index.yaml）"
if [[ ! -f "$INDEX" ]]; then
  fail "$INDEX 不存在"
else
  idx_ids="$(sed -n 's/^[[:space:]]*-[[:space:]]*id:[[:space:]]*["'\'']\{0,1\}\(RT-[0-9]\{3\}\)["'\'']\{0,1\}[[:space:]]*$/\1/p' "$INDEX" | sort -u)"
  dir_ids="$(for d in "$ROOT"/RT/RT-*; do [[ -d "$d" ]] && basename "$d"; done | sort -u)"
  only_dir="$(comm -23 <(printf '%s\n' "$dir_ids") <(printf '%s\n' "$idx_ids") | tr '\n' ' ')"
  only_idx="$(comm -13 <(printf '%s\n' "$dir_ids") <(printf '%s\n' "$idx_ids") | tr '\n' ' ')"
  if [[ -n "${only_dir// }" ]]; then
    fail "有目录没索引条目：${only_dir% }——新 RT 会照着索引取号，漏号就会撞号"
  fi
  if [[ -n "${only_idx// }" ]]; then
    fail "有索引条目没目录：${only_idx% }——索引指向不存在的 RT"
  fi
  if [[ -z "${only_dir// }" && -z "${only_idx// }" ]]; then
    ok "花名册一致（$(printf '%s\n' "$dir_ids" | grep -c . ) 个 RT，目录与索引一一对应）"
  fi
fi

# ── 4. 宿主 skill 安装状态（告警） ───────────────────────────────────────────
section "4/4 宿主 skill 安装状态（告警级）"
skill_out="$(bash "$AODW/tools/install-skills.sh" --check 2>&1)"
skill_code=$?
printf '%s\n' "$skill_out" | sed -n '/^  /p'
if [[ $skill_code -eq 0 ]]; then
  ok "AODW 自带 skill 的宿主入口已安装"
else
  warn "有 skill 未安装到宿主目录——源在 .aodw-next/skills/ 里，随仓库走；宿主入口是本机状态（已在 .gitignore），要用就跑：bash .aodw-next/tools/install-skills.sh"
fi

# ── 收口 ─────────────────────────────────────────────────────────────────────
section "结果"
if [[ $hard_fail -ne 0 ]]; then
  echo "aodw-check: 未通过（有硬失败项）"
  exit 1
fi
if [[ $warned -ne 0 ]]; then
  echo "aodw-check: 通过（有告警，不阻断）"
else
  echo "aodw-check: 通过"
fi
exit 0
