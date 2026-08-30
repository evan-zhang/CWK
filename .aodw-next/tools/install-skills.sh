#!/usr/bin/env bash
# =============================================================================
# install-skills.sh — 把 AODW 自带的 skill 安装到宿主项目的 skill 目录
# =============================================================================
# 为什么需要它：`.aodw-next/` 是可整体拷贝到其它项目的 AODW 框架目录，它自带的
# skill（`.aodw-next/skills/*`）跟着一起走；但各家 AI 工具是从**宿主目录**
# （`.agent/skills/` 或 `.claude/skills/`）发现 skill 的。本脚本负责把前者接到后者。
#
# 用法：
#   bash .aodw-next/tools/install-skills.sh                 # 安装（默认符号链接）
#   bash .aodw-next/tools/install-skills.sh --copy          # 复制而非链接
#   bash .aodw-next/tools/install-skills.sh --target <dir>  # 指定宿主 skill 目录
#   bash .aodw-next/tools/install-skills.sh --check         # 只检查状态，不改动
#   bash .aodw-next/tools/install-skills.sh --uninstall     # 移除本脚本装过的入口
#
# 默认用**符号链接**：源码只有一份（在 .aodw-next/ 里），改了立刻生效，
# 也不会出现「宿主目录里的副本和 AODW 里的源不一致」这种老问题。
# 若你的环境不支持符号链接（部分 Windows / 某些同步盘），用 --copy。
#
# 退出码：0 成功 / 1 有失败项 / 2 用法错误
# =============================================================================
set -uo pipefail

MODE="link"      # link | copy
ACTION="install" # install | check | uninstall
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy)      MODE="copy"; shift ;;
    --check)     ACTION="check"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --target)    TARGET="${2:-}"; shift 2 ;;
    -h|--help)   sed -n '2,22p' "$0"; exit 0 ;;
    *)           echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AODW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_SRC="$AODW_DIR/skills"
PROJECT_ROOT="$(cd "$AODW_DIR/.." && pwd)"

[[ -d "$SKILLS_SRC" ]] || { echo "AODW 未自带 skill（$SKILLS_SRC 不存在），无需安装"; exit 0; }

# ── 探测宿主 skill 目录 ──────────────────────────────────────────────────────
# 顺序：显式 --target > 已存在的 .agent/skills > 已存在的 .claude/skills > 新建 .agent/skills
detect_target() {
  [[ -n "$TARGET" ]] && { echo "$TARGET"; return; }
  for d in "$PROJECT_ROOT/.agent/skills" "$PROJECT_ROOT/.claude/skills"; do
    [[ -d "$d" ]] && { echo "$d"; return; }
  done
  echo "$PROJECT_ROOT/.agent/skills"   # 都不存在时的默认落点
}
TARGET_DIR="$(detect_target)"

echo "AODW skill 安装器"
echo "  源：    $SKILLS_SRC"
echo "  目标：  $TARGET_DIR"
echo "  模式：  $([[ "$MODE" == link ]] && echo '符号链接（改源即生效）' || echo '复制')"
echo

fails=0
installed=0

for src in "$SKILLS_SRC"/*/; do
  [[ -d "$src" ]] || continue
  name="$(basename "$src")"
  [[ -f "$src/SKILL.md" ]] || { echo "  跳过 $name（无 SKILL.md，不是合法 skill）"; continue; }
  dst="$TARGET_DIR/$name"

  case "$ACTION" in
    check)
      if [[ -L "$dst" ]]; then
        printf '  %-18s 已链接 → %s\n' "$name" "$(readlink "$dst")"
      elif [[ -d "$dst" ]]; then
        printf '  %-18s 已复制（注意：源更新后需重装）\n' "$name"
      else
        printf '  %-18s ✗ 未安装\n' "$name"; fails=$((fails+1))
      fi
      ;;
    uninstall)
      if [[ -L "$dst" ]]; then
        rm -f "$dst"; printf '  %-18s 已移除链接\n' "$name"
      elif [[ -d "$dst" ]]; then
        # 只删我们装的副本：有 SKILL.md 且与源同名才删，避免误删宿主自有 skill
        if [[ -f "$dst/SKILL.md" ]]; then
          rm -rf "$dst"; printf '  %-18s 已移除副本\n' "$name"
        fi
      else
        printf '  %-18s 本就未安装\n' "$name"
      fi
      ;;
    install)
      mkdir -p "$TARGET_DIR"
      # 已存在且不是我们装的链接 → 不覆盖，报出来让人决定
      if [[ -e "$dst" && ! -L "$dst" ]]; then
        printf '  %-18s ⚠ 目标已存在且非本脚本所建，跳过（如需覆盖请先手动移除）\n' "$name"
        fails=$((fails+1)); continue
      fi
      rm -f "$dst"
      if [[ "$MODE" == "link" ]]; then
        # 用相对路径链接：仓库整体移动位置后仍然有效
        rel="$(python3 -c "import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))" "$src" "$TARGET_DIR" 2>/dev/null)"
        if [[ -n "$rel" ]] && ln -s "${rel%/}" "$dst" 2>/dev/null; then
          printf '  %-18s ✓ 已链接 → %s\n' "$name" "${rel%/}"
        elif ln -s "$src" "$dst" 2>/dev/null; then
          printf '  %-18s ✓ 已链接（绝对路径）\n' "$name"
        else
          printf '  %-18s ✗ 链接失败——改用 --copy 重试\n' "$name"; fails=$((fails+1)); continue
        fi
      else
        cp -R "${src%/}" "$dst" && printf '  %-18s ✓ 已复制\n' "$name" \
          || { printf '  %-18s ✗ 复制失败\n' "$name"; fails=$((fails+1)); continue; }
      fi
      installed=$((installed+1))
      ;;
  esac
done

echo
if [[ "$ACTION" == "install" ]]; then
  echo "  完成：成功 $installed 个，失败 $fails 个"
  if [[ $installed -gt 0 ]]; then
    echo
    echo "  ⚠️ 两件事："
    echo "     1. 把宿主 skill 目录的入口加进版本控制或忽略清单——符号链接方式下，"
    echo "        建议把 $(basename "$TARGET_DIR") 下的链接**加入 .gitignore**，"
    echo "        因为源已经在 .aodw-next/ 里受控，两处都跟踪会重复。"
    echo "     2. 新会话可能需要重启才能发现新装的 skill。"
  fi
elif [[ "$ACTION" == "check" ]]; then
  echo "  检查完成：未安装 $fails 个"
fi

[[ $fails -eq 0 ]] || exit 1
exit 0
