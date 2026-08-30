#!/usr/bin/env bash
# =============================================================================
# check-closure.sh — 关会话前检查交接包是否还跟得上 HEAD
# =============================================================================
# 用法：
#   check-closure.sh <交接包路径>           检查，跟得上则 exit 0
#   check-closure.sh <路径> --quiet         只在有缺口时输出
#
# 为什么需要它：交接包写完后往往还会继续干活（用户提新需求、顺手修个问题），
# 于是它立刻就不再覆盖 HEAD。本会话内**连续三次**踩到这个坑，每次都靠人工比对
# 才发现。这条命令把它变成机械判定。
#
# 判定方式：取交接包里引用过的 commit（`abc1234` 形式），找出其中最新的那个，
# 再看它到 HEAD 之间还有多少提交没被交接包提及。**只统计动过版本库内容的提交**，
# 合并提交与纯 merge 记录会一并列出但单独标注。
#
# 退出码：0 跟得上 / 1 有缺口 / 2 用法错误
# =============================================================================
set -uo pipefail

QUIET=0
DOC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet) QUIET=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) DOC="$1"; shift ;;
  esac
done

[[ -n "$DOC" && -f "$DOC" ]] || { echo "用法：check-closure.sh <交接包路径> [--quiet]" >&2; exit 2; }

REPO="$(git -C "$(dirname "$DOC")" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "不在 git 仓库内，无法检查" >&2; exit 2; }
cd "$REPO"

# 交接包里引用的全部 commit
HASHES="$(grep -oE '`[0-9a-f]{7,40}`' "$DOC" | tr -d '`' | sort -u)"
[[ -n "$HASHES" ]] || { echo "交接包未引用任何 commit，无法判定覆盖范围" >&2; exit 2; }

# 找出其中在 HEAD 历史里、且最新的那个
newest=""; newest_ts=0
while read -r h; do
  [[ -n "$h" ]] || continue
  git cat-file -t "$h" >/dev/null 2>&1 || continue
  git merge-base --is-ancestor "$h" HEAD 2>/dev/null || continue
  ts="$(git log -1 --format=%ct "$h" 2>/dev/null || echo 0)"
  if (( ts > newest_ts )); then newest_ts=$ts; newest="$h"; fi
done <<< "$HASHES"

[[ -n "$newest" ]] || { echo "交接包引用的 commit 都不在当前分支历史里，无法判定" >&2; exit 2; }

# 逐个筛：**排除「只动了交接包自身」的提交**。
# 这是一个逻辑不动点——交接包不可能引用「记录它自己的那个提交」，那个 commit
# 在写交接包时还不存在。若不排除，本检查永远报缺口、永远关不了会话（实测踩到）。
DOC_REL="${DOC#./}"
MISSING=""
EXEMPT=""
while read -r line; do
  [[ -n "$line" ]] || continue
  h="${line%% *}"
  files="$(git show --pretty=format: --name-only "$h" 2>/dev/null | grep -v '^$' || true)"
  # 元层面路径 = 交接包本身 + 写交接包的工具。动这些的提交是「交接动作自身」，
  # 不构成需要被交接的新工作。不豁免它们会陷入不动点：补一次交接包就多一个
  # 未覆盖提交，修一次工具又多一个，会话永远关不掉（两次实测踩到）。
  # ⚠️ 豁免不等于隐藏——它们会在下方「已豁免」里逐条列出，由人确认。
  if grep -qvE '^(docs/handover/|\.aodw-next/skills/handover-pack/)' <<< "$files" 2>/dev/null; then
    MISSING+="$line"$'\n'
  else
    EXEMPT+="$line"$'\n'
  fi
  # --no-merges：merge 提交自身不引入内容（其 --name-only 为空，会让上面的判断反向），
  # 真正的改动都在被合并的那些提交里，逐条检查它们即可。
done <<< "$(git log --oneline --no-merges "${newest}..HEAD" --format='%h %s' 2>/dev/null || true)"
n="$(printf '%s' "$MISSING" | grep -c . || true)"
ne="$(printf '%s' "$EXEMPT" | grep -c . || true)"

if [[ "$n" -eq 0 ]]; then
  [[ $QUIET -eq 1 ]] || {
    echo "交接包闭合检查 — $(basename "$DOC")"
    echo "  最新引用：$(git log -1 --format='%h %s' "$newest" | cut -c1-60)"
    echo "  ✅ 已覆盖到 HEAD，可以关会话"
    if [[ "$ne" -gt 0 ]]; then
      echo
      echo "  已豁免 $ne 个「交接动作自身」的提交（只动交接包或其工具，不构成待交接的新工作）："
      printf '%s\n' "$EXEMPT" | grep -v '^$' | sed 's/^/     /'
      echo "  ⚠️ 扫一眼：若其中某条其实是该记的实质变更，补进交接包再跑一次。"
    fi
  }
  exit 0
fi

echo "交接包闭合检查 — $(basename "$DOC")"
echo "  最新引用：$(git log -1 --format='%h %s' "$newest" | cut -c1-60)"
echo "  ⚠️ 之后还有 $n 个提交未被交接包提及："
echo
printf '%s\n' "$MISSING" | sed 's/^/     /'
echo
echo "  处置：把其中与本会话相关的补进交接包（通常补进「现状变化」一节），"
echo "        再重跑本检查。与本会话无关的（并行会话的提交）可忽略——"
echo "        但**要在交接包里说明忽略了什么**，否则接手的人无从判断。"
exit 1
