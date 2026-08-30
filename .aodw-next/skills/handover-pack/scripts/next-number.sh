#!/usr/bin/env bash
# =============================================================================
# next-number.sh — 给新交接包取一个不会撞的 H 序号
# =============================================================================
# 用法：
#   next-number.sh              打印下一个可用序号（如 H004）与占用情况
#   next-number.sh --quiet      只打印序号本身，便于脚本取用
#   next-number.sh --list       列出全部已占用序号及其来源
#
# 为什么不能只 ls 一下工作区：
#   本仓库刚因人工取号出过两次事故——DI-013/DI-014 在**未合并的分支**上占了号，
#   合并时整块丢失、4 天后才靠翻 git 历史找回；DI-013 与 DI-047 则是同一个问题
#   隔 9 天被登记两次。只看当前工作区，这两类坑都挡不住。
#   故本脚本扫三处：①工作区文件 ②HEAD 的历史提交 ③**全部分支（含未合并的）**。
#
# 退出码：0 正常 / 2 用法错误
# =============================================================================
set -uo pipefail

MODE="normal"
case "${1:-}" in
  --quiet) MODE="quiet" ;;
  --list)  MODE="list" ;;
  "")      ;;
  -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
  *) echo "未知参数：${1}（用 --help 看用法）" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "不在 git 仓库内，无法可靠取号" >&2; exit 2; }
cd "$REPO"

DIR="docs/handover"
PAT='H[0-9]\{3\}-'

# ① 工作区
scan_worktree() {
  [[ -d "$DIR" ]] || return 0
  ls "$DIR" 2>/dev/null | grep -o "^H[0-9]\{3\}" | sed 's/^/工作区 /'
}
# ② 历史提交（文件可能已改名或删除，但号被用过就不该复用）
scan_history() {
  git log --all --pretty=format: --name-only --diff-filter=A -- "$DIR" 2>/dev/null \
    | grep -o "$DIR/H[0-9]\{3\}" | sed "s|$DIR/||" | sed 's/^/历史   /'
}
# ③ 全部分支的当前内容（含未合并分支——DI-013/014 正是栽在这）
scan_branches() {
  for ref in $(git for-each-ref --format='%(refname)' refs/heads refs/remotes 2>/dev/null); do
    git ls-tree -r --name-only "$ref" -- "$DIR" 2>/dev/null \
      | grep -o "$DIR/H[0-9]\{3\}" | sed "s|$DIR/||" \
      | sed "s|^|$(basename "$ref") |"
  done
}

ALL="$(printf '%s\n%s\n%s\n' "$(scan_worktree)" "$(scan_history)" "$(scan_branches)" | grep -v '^$' || true)"
USED="$(printf '%s' "$ALL" | awk '{print $2}' | sort -u | grep -v '^$' || true)"

if [[ "$MODE" == "list" ]]; then
  echo "已占用的 H 序号（来源含未合并分支）："
  if [[ -z "$USED" ]]; then echo "  （无）"; else
    while read -r n; do
      [[ -n "$n" ]] || continue
      src="$(printf '%s' "$ALL" | awk -v n="$n" '$2==n{printf "%s ", $1}' | tr ' ' '\n' | sort -u | tr '\n' ' ')"
      printf '  %s  ← %s\n' "$n" "$src"
    done <<< "$USED"
  fi
  exit 0
fi

max=0
while read -r n; do
  [[ -n "$n" ]] || continue
  v=$((10#${n#H}))
  (( v > max )) && max=$v
done <<< "$USED"

next="$(printf 'H%03d' $((max + 1)))"

if [[ "$MODE" == "quiet" ]]; then
  echo "$next"; exit 0
fi

count="$(printf '%s' "$USED" | grep -c . || true)"
echo "交接包取号"
echo "  已占用：$count 个（扫描范围：工作区 + 历史提交 + 全部分支含未合并）"
[[ -n "$USED" ]] && echo "  已用号：$(printf '%s' "$USED" | tr '\n' ' ')"
echo
echo "  下一个可用：$next"
echo
echo "  用法：docs/handover/${next}-<主题>.md"
echo "  ⚠️ 取号后请尽快提交占位，长期只在本地分支持有会让别人看不到（DI-011 的坑）。"
