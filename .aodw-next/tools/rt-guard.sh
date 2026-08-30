#!/usr/bin/env bash
# =============================================================================
# rt-guard.sh — AODW RT 门禁脚本（RT-125 U1 骨架）
# =============================================================================
# 判据外置于 .aodw-next/manifests/rt-gates.yaml；本脚本入口签名保持稳定，
# 改判据逻辑先改 YAML / 新增 impl 检查函数，不动入口。
#
# 三形态 I/O（U0 spike 结论 CONFIRMED，见 RT/RT-125/spike/hooks-capability.md §5）：
#   CLI        形态：rt-guard.sh --root <dir> [--rt <RT-ID>] [--format text|json]
#   hook       形态：rt-guard.sh --hook-mode   （stdin 读 Claude Code hook JSON）
#   pre-commit 形态：rt-guard.sh --pre-commit  （git pre-commit hook 入口，U6）
#
# 退出码语义（统一两形态）：
#   0 = 通过 / 放行（hook 形态 deny 的正路也是 exit 0 + hookSpecificOutput JSON）
#   1 = CLI 判据失败（error 级判据未过）
#   2 = 内部错误 / 用法错误（hook 语境的阻断兜底码）
#
# 错误处理裁决（rt-plan.md v5 U1 判据④，第 3 轮审计 P0-1 正式裁决）：
#   本文件豁免全局约束的 -e，采用 set -uo pipefail + 显式 EXIT trap。
#   依据：U0 实测 hook 对非 0/2 退出码 fail-open（静默放行），-e 的 exit 1
#   恰好落进这个洞。任何未经显式出口(rtg_finish)的退出都被改写为 exit 2。
#
# 解析器约束：YAML/JSON 一律走 python3（禁 yq；jq 亦不使用，理由同构——
#   依据 resources/scripts/runtime/preflight-config.sh:77 先例）。
# =============================================================================

set -uo pipefail

RT_GUARD_VERSION="0.6.1"

# ── fail-closed 兜底：一切意外退出改写为 exit 2 ──────────────────────────────
RTG_FINISHED=0

rtg_finish() { # $1 = 显式退出码（唯一合法出口）
  RTG_FINISHED=1
  exit "$1"
}

rtg_on_exit() {
  local code=$?
  if [[ "$RTG_FINISHED" == "1" ]]; then
    exit "$code"
  fi
  printf 'rt-guard: 内部意外退出（原始退出码 %s），fail-closed 改写为 exit 2\n' "$code" >&2
  exit 2
}
trap rtg_on_exit EXIT

rtg_die() { # 内部错误 / 用法错误 → exit 2
  printf 'rt-guard: %s\n' "$1" >&2
  rtg_finish 2
}

# ── 路径与解释器定位 ─────────────────────────────────────────────────────────
RTG_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || RTG_SCRIPT_DIR=""
[[ -n "$RTG_SCRIPT_DIR" ]] || rtg_die "无法定位脚本目录"
RTG_DEFAULT_GATES="$RTG_SCRIPT_DIR/../manifests/rt-gates.yaml"

RTG_PYTHON="${RT_GUARD_PYTHON:-}"
if [[ -z "$RTG_PYTHON" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    RTG_PYTHON="python3"
  elif [[ -x /usr/bin/python3 ]]; then
    RTG_PYTHON="/usr/bin/python3"
  else
    rtg_die "找不到 python3（YAML/JSON 解析依赖之）"
  fi
fi

# ── 用法 ─────────────────────────────────────────────────────────────────────
rtg_usage() {
  cat <<'USAGE'
用法: rt-guard.sh <形态> [选项]

AODW RT 门禁（RT-125）。判据外置于 .aodw-next/manifests/rt-gates.yaml。

CLI 形态（人类 / pre-commit / CI 调用）:
  rt-guard.sh --root <dir> [--rt <RT-ID>] [--format text|json]
              [--gates <file>] [--list-gates]
    --root <dir>     被检仓库根目录（必填）
    --rt <RT-ID>     只检查指定 RT（如 RT-125）；缺省扫描 <root>/RT/RT-*
    --format <fmt>   输出格式 text（默认）或 json
    --gates <file>   判据文件路径（默认取脚本同仓库的 manifests/rt-gates.yaml）
    --list-gates     只加载并列出判据，不执行检查
    --self-test      跑自带 fixture 套件（.aodw-next/tools/fixtures/），验证判据行为

hook 形态（Claude Code hooks 调用，stdin 读 hook JSON）:
  rt-guard.sh --hook-mode [--root <dir>] [--gates <file>]
    按 stdin 的 hook_event_name 分派 PreToolUse / Stop；
    Stop 事件当 stop_hook_active=true 时直接放行（防死循环）；
    诊断信息一律走 stderr（stdout 保留给结构化裁决 JSON）。
    PreToolUse 判定策略（U6）：识别为 git commit（含 -C/cd 变体）时，
    对目标仓库当前分支 feature/RT-NNN-* 提取 RT-ID 并跑 CLI 判据——
    有 error 级未过 → deny；仅 warn → 放行；提不出 RT-ID → 放行（非 RT 车道）。

pre-commit 形态（git pre-commit hook 调用，U6）:
  rt-guard.sh --pre-commit [--gates <file>]
    须在被检 git 仓库内运行（pre-commit 框架保证 cwd=仓库根）；
    按当前分支 feature/RT-NNN-* 提取 RT-ID，提不出则快速通过（非 RT 车道）；
    提出则跑 CLI 判据，error 级未过 → exit 1（阻断提交）。

交叉引用明细扫描（判据 #10 / G110 的明细形态，U2b）:
  rt-guard.sh --root <dir> --rt <RT-ID> --scan-refs [--scan-rules]
    对被检 RT 目录内全部 .md 逐引用输出 file:line 与 §N 的解析结果
    （OK / OK-AMBIG / DANGLING / UNRESOLVED / SEC-OK / SEC-DANGLING /
    XDOC-SKIP）及统计。告警级报告模式：有悬空也 exit 0（内部错误仍 2）。
    --scan-rules  额外并入 .aodw-next/ 全库 .md（独立开关，默认关）

调试 / 测试接口:
  rt-guard.sh --parse-command "<命令行>"   输出 action=git-commit|other
  rt-guard.sh --emit-deny <event> <reason> 输出该事件的 deny 裁决 JSON
  rt-guard.sh --help                       本帮助
  rt-guard.sh --version                    版本

退出码:
  0  通过 / 放行（hook 的 deny 正路 = exit 0 + hookSpecificOutput JSON）
  1  CLI 判据失败（error 级判据未过）
  2  内部错误 / 用法错误（hook 语境的阻断兜底码；fail-closed 由 EXIT trap 保证）
USAGE
}

# ── YAML 判据加载（python3；PyYAML 优先，缺失时退回受限子集解析器）───────────
# 输出：每判据一行 TSV: id \t name \t severity \t scope \t enabled \t impl \t title \t param
rtg_load_gates() { # $1 = gates 文件路径
  "$RTG_PYTHON" - "$1" <<'PY'
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        text = f.read()
except OSError as e:
    print(f"gates 文件不可读: {e}", file=sys.stderr)
    sys.exit(3)


def parse_scalar(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def fallback_parse(text):
    """受限 YAML 子集解析器（无 PyYAML 环境的兜底，超出子集即报错=fail-closed）。
    支持：顶层 key: value、单层 defaults 映射、gates 的扁平条目列表、整行注释。"""
    data, ctx = {}, None
    for ln, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if indent == 0:
            if body == "defaults:":
                data["defaults"], ctx = {}, "defaults"
            elif body == "gates:":
                data["gates"], ctx = [], "gates"
            elif ":" in body:
                k, v = body.split(":", 1)
                data[k.strip()] = parse_scalar(v)
                ctx = None
            else:
                raise ValueError(f"第 {ln} 行超出受限子集: {body!r}")
        elif ctx == "defaults" and ":" in body:
            k, v = body.split(":", 1)
            data["defaults"][k.strip()] = parse_scalar(v)
        elif ctx == "gates" and body.startswith("- "):
            item = {}
            k, v = body[2:].split(":", 1)
            item[k.strip()] = parse_scalar(v)
            data["gates"].append(item)
        elif ctx == "gates" and ":" in body and data.get("gates"):
            k, v = body.split(":", 1)
            data["gates"][-1][k.strip()] = parse_scalar(v)
        else:
            raise ValueError(f"第 {ln} 行超出受限子集: {body!r}")
    return data


try:
    try:
        import yaml  # PyYAML
        data = yaml.safe_load(text)
    except ImportError:
        data = fallback_parse(text)
except Exception as e:
    print(f"gates 文件解析失败: {e}", file=sys.stderr)
    sys.exit(3)

if not isinstance(data, dict) or data.get("schema_version") != 1:
    print("gates 文件缺少 schema_version: 1", file=sys.stderr)
    sys.exit(3)
defaults = data.get("defaults") or {}
gates = data.get("gates")
if not isinstance(gates, list) or not gates:
    print("gates 文件未定义任何判据条目", file=sys.stderr)
    sys.exit(3)

SEVERITIES, SCOPES = {"error", "warn"}, {"rt", "repo"}
seen = set()
for g in gates:
    if not isinstance(g, dict):
        print(f"判据条目不是映射: {g!r}", file=sys.stderr)
        sys.exit(3)
    merged = {**defaults, **g}
    for field in ("id", "impl"):
        if not merged.get(field):
            print(f"判据条目缺少必填字段 {field}: {g!r}", file=sys.stderr)
            sys.exit(3)
    gid = str(merged["id"])
    if gid in seen:
        print(f"判据 id 重复: {gid}", file=sys.stderr)
        sys.exit(3)
    seen.add(gid)
    sev = merged.get("severity", "error")
    scope = merged.get("scope", "rt")
    if sev not in SEVERITIES:
        print(f"判据 {gid} 的 severity 非法: {sev!r}（须为 error|warn）", file=sys.stderr)
        sys.exit(3)
    if scope not in SCOPES:
        print(f"判据 {gid} 的 scope 非法: {scope!r}（须为 rt|repo）", file=sys.stderr)
        sys.exit(3)
    enabled = merged.get("enabled", True)
    if not isinstance(enabled, bool):
        print(f"判据 {gid} 的 enabled 非法: {enabled!r}（须为 true|false）", file=sys.stderr)
        sys.exit(3)
    row = [gid, str(merged.get("name", "")), sev, scope,
           "true" if enabled else "false", str(merged["impl"]),
           str(merged.get("title", "")), str(merged.get("param", ""))]
    for cell in row:
        if "\t" in cell or "\n" in cell:
            print(f"判据 {gid} 字段含制表符/换行，禁止", file=sys.stderr)
            sys.exit(3)
    print("\t".join(row))
PY
}

# ── hook stdin JSON 字段提取（逐字段提取，避免 eval 注入）────────────────────
# $1 = JSON 全文, $2 = 点分字段路径；缺字段输出空串；JSON 非法 → 非零退出
rtg_json_field() {
  printf '%s' "$1" | "$RTG_PYTHON" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(4)
cur = d
for k in sys.argv[1].split("."):
    if isinstance(cur, dict) and k in cur:
        cur = cur[k]
    else:
        sys.exit(0)
if isinstance(cur, bool):
    sys.stdout.write("true" if cur else "false")
elif cur is None:
    pass
else:
    sys.stdout.write(str(cur))
' "$2"
}

# ── deny 裁决 JSON（spike §5.5：PreToolUse 与 Stop 字段名不同）───────────────
rtg_emit_deny() { # $1 = 事件名, $2 = 理由；输出到 stdout，调用方负责 exit 0
  "$RTG_PYTHON" -c '
import json, sys
event, reason = sys.argv[1], sys.argv[2]
if event == "PreToolUse":
    inner = {"hookEventName": event,
             "permissionDecision": "deny",
             "permissionDecisionReason": reason}
else:
    inner = {"hookEventName": event,
             "decision": "deny",
             "decision_reason": reason}
print(json.dumps({"hookSpecificOutput": inner}, ensure_ascii=False))
' "$1" "$2"
}

# ── 命令解析：识别 git commit 动作（U0 §3.3 绕过路径的对策）──────────────────
# token 扫描器而非前缀正则：git 的带参全局选项（-C <dir>、-c k=v、--git-dir x）
# 插在子命令前时，前缀匹配失效——spike §5.6 示例正则同样匹配不到 `git -C x commit`。
# 已知局限（防呆非防攻击）：按空白分词，不感知引号/变量拼接/base64。
rtg_strip_quotes() { # $1 = token；剥一层成对引号（分词后引号是字面字符）
  local v="$1"
  if [[ ${#v} -ge 2 && "${v:0:1}" == "${v: -1}" && ( "${v:0:1}" == '"' || "${v:0:1}" == "'" ) ]]; then
    v="${v:1:${#v}-2}"
  fi
  printf '%s' "$v"
}

rtg_compose_dir() { # $1=基准目录 $2=后续目录；git 多个 -C 的顺序合成语义
  case "$2" in
    /*) printf '%s' "$2" ;;
    "") printf '%s' "$1" ;;
    *)  printf '%s/%s' "${1%/}" "$2" ;;
  esac
}

RTG_COMMIT_CHDIR=""   # rtg_segment_is_git_commit 返回 0 时：该段 -C 选项的合成目录

rtg_segment_is_git_commit() { # $1 = 单个命令段；返回 0 = 是 git commit
  local -a toks=()
  read -r -a toks <<< "$1" || true
  local n=${#toks[@]} i=0 t chdir=""
  RTG_COMMIT_CHDIR=""
  # 1) 跳过包装词与环境变量赋值前缀
  while (( i < n )); do
    t="${toks[i]}"
    case "$t" in
      command|env|nohup|exec) i=$((i + 1)) ;;
      *=*) i=$((i + 1)) ;;
      *) break ;;
    esac
  done
  (( i < n )) || return 1
  # 2) git 本体（裸名或任意路径前缀 /usr/bin/git）
  case "${toks[i]}" in
    git|*/git) ;;
    *) return 1 ;;
  esac
  i=$((i + 1))
  # 3) 跳过 git 全局选项，找到第一个子命令 token（-C 目录顺序合成捕获，U6）
  while (( i < n )); do
    t="${toks[i]}"
    case "$t" in
      -C)
        if (( i + 1 < n )); then
          chdir="$(rtg_compose_dir "$chdir" "$(rtg_strip_quotes "${toks[i+1]}")")"
        fi
        i=$((i + 2)) ;;
      -c|--git-dir|--work-tree|--namespace|--exec-path|--config-env)
        i=$((i + 2)) ;;   # 带独立参数的全局选项：连参数一起跳过
      --*=*|-*)
        i=$((i + 1)) ;;   # --git-dir=x 形式或无参全局选项（-p、--no-pager 等）
      *)
        if [[ "$t" == "commit" ]]; then
          RTG_COMMIT_CHDIR="$chdir"
          return 0
        fi
        return 1 ;;
    esac
  done
  return 1
}

rtg_classify_command() { # $1 = 完整命令行；stdout: git-commit | other
  local s="$1" seg
  # 复合命令拆段：&& || ; | & 与换行统一视作段分隔（顺序：先双字符后单字符）
  s="${s//&&/$'\n'}"
  s="${s//||/$'\n'}"
  s="${s//;/$'\n'}"
  s="${s//|/$'\n'}"
  s="${s//&/$'\n'}"
  while IFS= read -r seg; do
    [[ -n "${seg// /}" ]] || continue
    if rtg_segment_is_git_commit "$seg"; then
      printf 'git-commit'
      return 0
    fi
  done <<< "$s"
  printf 'other'
}

# U6：解析 commit 动作的目标目录——跟踪段间 `cd <dir>`，合成 commit 段的 -C。
# 已知局限同上（防呆非防攻击）：不感知引号内分隔符/变量/子 shell。
rtg_resolve_commit_target() { # $1=完整命令行 $2=基准目录；stdout=目标目录
  local s="$1" base="$2" seg cur="$2"
  s="${s//&&/$'\n'}"
  s="${s//||/$'\n'}"
  s="${s//;/$'\n'}"
  s="${s//|/$'\n'}"
  s="${s//&/$'\n'}"
  while IFS= read -r seg; do
    [[ -n "${seg// /}" ]] || continue
    local -a toks=()
    read -r -a toks <<< "$seg" || true
    if [[ ${#toks[@]} -ge 2 && "${toks[0]}" == "cd" ]]; then
      cur="$(rtg_compose_dir "$cur" "$(rtg_strip_quotes "${toks[1]}")")"
      continue
    fi
    if rtg_segment_is_git_commit "$seg"; then
      rtg_compose_dir "$cur" "$RTG_COMMIT_CHDIR"
      return 0
    fi
  done <<< "$s"
  printf '%s' "$base"
}

# ── 判据实现分派（placeholder 族=U1；meta/text/index 族=U2a；xref 族=U2b）────
# 检查函数契约：入参 $1=root $2=RT-ID [$3=param]；stdout 一行说明；返回 0=PASS 1=FAIL
# 注意：文件/字段缺失是预期内判据结果（返回 1 → 按 severity 计 WARN/FAIL），
#       不是意外错误，绝不走 rtg_die / trap 的 exit 2 路径（任务包判据 9）。
rtg_check_placeholder_rt_dir_exists() {
  local root="$1" rt_id="$2"
  if [[ -d "$root/RT/$rt_id" ]]; then
    printf 'RT/%s 目录存在' "$rt_id"
    return 0
  fi
  printf 'RT/%s 目录不存在' "$rt_id"
  return 1
}

# ── U2a 辅助：meta.yaml 顶层标量字段读取 ────────────────────────────────────
# 输出剥行内注释（# 前须空白）、剥成对引号、trim 后的值；文件/字段缺失输出空串。
rtg_meta_field() { # $1=meta.yaml 路径 $2=字段名
  [[ -f "$1" ]] || return 0
  local line v
  line="$(grep -m1 -E "^$2:" "$1" 2>/dev/null)" || return 0
  v="${line#*:}"
  v="$(printf '%s' "$v" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
  if [[ ${#v} -ge 2 ]]; then
    case "$v" in
      \"*\") v="${v#\"}"; v="${v%\"}" ;;
      \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
  fi
  printf '%s' "$v"
}

rtg_value_in_enum() { # $1=值 $2=枚举（'A|B|C' 形式）；返回 0=在枚举内
  local IFS='|' e
  for e in $2; do
    [[ "$1" == "$e" ]] && return 0
  done
  return 1
}

# profile 分层（任务包 §1）：仅 'Spec-Full' 走严判据；Spec-Lite / 缺失 / 其它值
# 一律按 Spec-Lite 宽判据（缺失另由 G108 单独告警）。
rtg_profile_mode_label() { # $1=profile 原始值；stdout: 宽判据场景的人读标签
  if [[ -z "$1" ]]; then
    printf '缺 profile（按 Spec-Lite 宽判据）'
  elif [[ "$1" != "Spec-Lite" ]]; then
    printf 'profile=%s（按 Spec-Lite 宽判据）' "$1"
  else
    printf 'Spec-Lite'
  fi
}

# ── U2a 检查函数族 ──────────────────────────────────────────────────────────
rtg_check_meta_type_valid() { # 判据 1/2：type 值须在枚举内（剥注释后比对）
  local root="$1" rt_id="$2" enum="${3-}" meta="$1/RT/$2/meta.yaml" v
  local idx="$1/RT/index.yaml"
  if [[ ! -f "$meta" ]]; then
    printf 'meta.yaml 不存在，type 合法性无从判定（缺失由 G103 告警）'
    return 0
  fi
  v="$(rtg_meta_field "$meta" type)"
  if [[ -z "$v" ]]; then
    printf 'type 字段缺失，合法性检查跳过（缺失由 G103 告警）'
    return 0
  fi
  if rtg_value_in_enum "$v" "$enum"; then
    printf 'type=%s 合法（六值枚举内）' "$v"
    return 0
  fi
  # 存量豁免（2026-08-20）：rt-manager.md §3.4d 收敛六值时明写「**存量 RT 不回头
  # 重标**」，且同节把 G103 的存量判据定为「index.yaml 条目不带 backfill 键」。
  # G101 此前没有这一层，于是 45 个存量 RT（Implementation 10 / Validation 7 /
  # Improvement 6 / Enhancement 5 / Reliability 4 / … 及三个组合值）长期硬失败，
  # 让全库门禁的 error 恒为 45——真信号被噪声淹没，每次改 RT 都要人工比对
  # 「这条 error 是不是我引入的」。判据与 G111 完全同形，复用其写法。
  # 新 RT（index.yaml 条目不带 backfill）不受豁免，六值约束照常生效。
  if [[ -f "$idx" ]] && awk -v id="$rt_id" '
      $0 ~ "^- id: "id"$" {inb=1; next}
      inb && /^- id: / {exit}
      inb && /backfill:/ {found=1; exit}
      END {exit !found}' "$idx"; then
    printf 'type=%s 不在六值枚举内，但属存量回填条目（backfill），按 §3.4d「存量不回头重标」豁免' "$v"
    return 0
  fi
  printf 'type 取值非法: %s（合法枚举: %s）' "$v" "$enum"
  return 1
}

rtg_check_meta_type_present() { # 判据 3：type 字段存在性（告警级）
  local meta="$1/RT/$2/meta.yaml" v
  if [[ ! -f "$meta" ]]; then
    printf 'meta.yaml 不存在，type 字段视作缺失'
    return 1
  fi
  v="$(rtg_meta_field "$meta" type)"
  if [[ -n "$v" ]]; then
    printf 'type 字段存在'
    return 0
  fi
  printf 'meta.yaml 缺少 type 字段（§3.4d 未规定必填，告警级）'
  return 1
}

rtg_check_meta_profile_present() { # 判据 8：profile 字段存在性（告警级）
  local meta="$1/RT/$2/meta.yaml" v
  if [[ ! -f "$meta" ]]; then
    printf 'meta.yaml 不存在，profile 字段视作缺失（其余判据按 Spec-Lite 宽判据）'
    return 1
  fi
  v="$(rtg_meta_field "$meta" profile)"
  if [[ -n "$v" ]]; then
    printf 'profile 字段存在（%s）' "$v"
    return 0
  fi
  printf 'meta.yaml 缺少 profile 字段（其余判据按 Spec-Lite 宽判据处理）'
  return 1
}

rtg_check_text_changelog_present() { # 判据 4/5：变更记录（profile 分层）
  local root="$1" rt_id="$2" re="${3-}" dir="$1/RT/$2" profile label
  profile="$(rtg_meta_field "$dir/meta.yaml" profile)"
  if [[ "$profile" == "Spec-Full" ]]; then
    if [[ -f "$dir/changelog.md" ]]; then
      printf 'Spec-Full: 独立变更记录 changelog.md 存在'
      return 0
    fi
    printf 'Spec-Full: 缺独立变更记录文件 changelog.md'
    return 1
  fi
  label="$(rtg_profile_mode_label "$profile")"
  if [[ ! -f "$dir/rt-lite.md" ]]; then
    printf '%s: rt-lite.md 不存在，无从确认变更记录' "$label"
    return 1
  fi
  if grep -qiE "$re" "$dir/rt-lite.md"; then
    printf '%s: rt-lite.md 命中变更记录同义词' "$label"
    return 0
  fi
  printf '%s: rt-lite.md 全文无变更记录同义词（%s）' "$label" "$re"
  return 1
}

rtg_check_text_csf_review_present() { # 判据 6/7：CSF 审查落盘（profile 分层）
  local root="$1" rt_id="$2" dir="$1/RT/$2" profile
  profile="$(rtg_meta_field "$dir/meta.yaml" profile)"
  if [[ "$profile" == "Spec-Full" ]]; then
    if [[ -f "$dir/csf-review.md" ]]; then
      printf 'Spec-Full: csf-review.md 存在'
      return 0
    fi
    printf 'Spec-Full: 缺 csf-review.md（CSF 审查未落盘）'
    return 1
  fi
  printf '%s: csf-review.md 为可选项，不作检查' "$(rtg_profile_mode_label "$profile")"
  return 0
}

rtg_check_index_entry_present() { # 判据 9：index.yaml 含本 RT 条目
  local root="$1" rt_id="$2" idx="$1/RT/index.yaml"
  if [[ ! -f "$idx" ]]; then
    # 文件缺失是预期内状态：按「全部条目缺失」报判据结果，不走 exit 2
    printf 'RT/index.yaml 不存在——按全部条目缺失处理，本 RT 条目视作缺失'
    return 1
  fi
  # 必须锚定「条目 id 行」，不能在全文搜 RT-ID：条目的 related 列表里也会写
  # 兄弟 RT 的 id（如 `  - RT-127`），全文 grep 会把「被别人引用过」误判成
  # 「自己有条目」——那样任何被关联过的 RT 都永远 PASS，这道闸就废了。
  # 匹配 `- id: RT-XXX`（YAML 序列项的 id 键），允许行首缩进与行尾注释/空白。
  if grep -qE "^[[:space:]]*-[[:space:]]+id:[[:space:]]*[\"']?${rt_id}[\"']?[[:space:]]*(#.*)?$" "$idx"; then
    printf 'RT/index.yaml 含本 RT 条目'
    return 0
  fi
  printf 'RT/index.yaml 无本 RT（%s）条目' "$rt_id"
  return 1
}

rtg_check_meta_retrospective_present() { # G111：关闭前必须过收口复盘 Gate
  # 判定链（AODW 0.6.1 rt-manager.md「关闭」）：
  #   status != done            → PASS（未到关闭时点）
  #   index 条目带 backfill 键   → PASS（存量回填豁免，规划「门禁只对新 RT 生效」）
  #   status == done 且非存量    → RT 目录须有 retrospective.md，缺 → 硬失败
  local root="$1" rt_id="$2" meta="$1/RT/$2/meta.yaml" idx="$1/RT/index.yaml"
  local st; st="$(rtg_meta_field "$meta" status)"
  if [[ "$st" != "done" ]]; then
    printf 'status=%s，未到关闭时点，复盘 Gate 不适用' "${st:-<缺失>}"
    return 0
  fi
  # 用条目块内是否含 backfill 判存量：取该 id 起到下一个 "- id:" 前的块
  if [[ -f "$idx" ]] && awk -v id="$rt_id" '
      $0 ~ "^- id: "id"$" {inb=1; next}
      inb && /^- id: / {exit}
      inb && /backfill:/ {found=1; exit}
      END {exit !found}' "$idx"; then
    printf '存量回填条目（backfill），复盘 Gate 豁免'
    return 0
  fi
  if [[ -f "$root/RT/$rt_id/retrospective.md" ]]; then
    printf 'retrospective.md 存在，复盘 Gate 已过'
    return 0
  fi
  printf 'status=done 但缺 retrospective.md——收口复盘 Gate 未过，不得关闭/合并'
  return 1
}

# ── RT-135 U1 任务包自检（G112/G113，对象=被检 RT 的 handoff/*-input.md）──────
# 枚举面钉死（rt-plan v4 U1 步骤 2 ⚠ 裁决）：只 glob RT/<id>/handoff/*-input.md，
# 不扫 fixtures/** 与 replay/**——md_files_under() 对 RT 目录整树 os.walk，prune
# 集不含这两个目录，会把「G112 必 FAIL」fixture 吃进真实扫描面，使 U2 自证判据
# 「无 error」永不可满足（两单元互斥，计划审计 001 P0-3）。
# G112：§3 零命中类判据（`<grep cmd>` → **0**，spike 方案 A）对本包 §1/§2 正文
#   实跑 grep 模式，命中即自冲突（照抄执行恒不通过；RT-125 unit-U5 事故原型）。
# G113：§3 须含判据溯源声明行且加减账自洽、删除项带非空理由；不反查 rt-plan。
# 两判据 msg 形态钉死「扫描 K 份 / 执行 N 条 / skip M 条」（出口判据⑥与 U2 判据
# ⑦机械读取）。python 退出码与 xref 扫描器同构：0=过 1=未过 3=内部错误。
rtg_handoff_scan() { # $1=mode(g112|g113) $2=root $3=RT-ID
  "$RTG_PYTHON" - "$1" "$2" "$3" <<'PY'
import glob, os, re, sys

mode, root, rt_id = sys.argv[1], sys.argv[2], sys.argv[3]


def die(msg):
    print(f"handoff 扫描器内部错误: {msg}", file=sys.stderr)
    sys.exit(3)


# 枚举面：显式 glob（见函数头注释），仅 handoff/*-input.md
files = sorted(glob.glob(os.path.join(root, "RT", rt_id, "handoff", "*-input.md")))
K = len(files)

H2 = re.compile(r"^##\s")  # 二级标题（### 不匹配：## 后须空白）


def split_pack(text):
    """→ (body, sec3)。body=[(1 起文件行号, 行)] §1/§2 正文=「## 1」标题后至
    「## 3」标题前（无 ## 1 则自文件头）；sec3 同构为 §3 节（至下一 ## 标题
    前），无 §3 节则 None。"""
    lines = text.splitlines()
    h1 = h3 = None
    for i, ln in enumerate(lines):
        if H2.match(ln):
            t = ln[2:].lstrip()
            if h1 is None and re.match(r"^1\b", t):
                h1 = i
            if h3 is None and re.match(r"^3\b", t):
                h3 = i
    b0 = h1 + 1 if h1 is not None else 0
    b1 = h3 if h3 is not None else len(lines)
    body = list(enumerate(lines[b0:b1], start=b0 + 1))
    if h3 is None:
        return body, None
    e = len(lines)
    for j in range(h3 + 1, len(lines)):
        if H2.match(lines[j]):
            e = j
            break
    return body, list(enumerate(lines[h3 + 1:e], start=h3 + 2))


def read_pack(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return split_pack(f.read())
    except OSError as e:
        die(f"读取 {p} 失败: {e}")


def stats_line(n_exec, n_skip):
    return f"扫描 {K} 份 / 执行 {n_exec} 条 / skip {n_skip} 条"


def g112():
    zero = re.compile(r"→\s*\*\*0\*\*")  # 零期望标记（spike 实证形态）
    grepcmd = re.compile(r'grep\s+-[A-Za-z]+\s+"([^"]+)"')  # 带引号模式的 grep
    prov = re.compile(r"溯源")  # 溯源声明行归 G113 管，不算判据
    item = re.compile(r"^\s*\d+[.、)]")  # 编号判据项
    n_exec = n_skip = 0
    conflicts, notes = [], []
    for p in files:
        base = os.path.basename(p)
        body, sec3 = read_pack(p)
        if sec3 is None:
            notes.append(f"{base} 无 §3 节")
            continue
        pack_n = 0  # 本包入账行数（执行+skip）；0 与「无 §3」须可区分（裁定 001）
        for no, ln in sec3:
            if prov.search(ln):
                continue
            # 判据行入账口径：编号项 / 含「→」/ 含 grep 三者其一；纯散文行
            # 机器不可识别为判据，不入 skip 账（口径见 rt-gates.yaml G112 注释）
            if not (item.match(ln) or "→" in ln or "grep" in ln):
                continue
            pack_n += 1
            mz, mg = zero.search(ln), grepcmd.search(ln)
            if not (mz and mg):
                n_skip += 1  # 非 grep 类 / 期望非零 / 模式不可提取 → 显式 skip
                continue
            try:
                rx = re.compile(mg.group(1))
            except re.error as e:
                n_skip += 1
                notes.append(f"{base}:{no} 模式不可编译 skip（{e}）")
                continue
            n_exec += 1
            for bno, bl in body:
                if rx.search(bl):
                    conflicts.append(
                        f'{base}:{no} 零命中判据 grep "{mg.group(1)}" '
                        f"命中本包正文 :{bno}（照抄执行恒不通过）")
                    break
        if pack_n == 0:
            notes.append(f"{base} §3 无可识别判据行")
    if conflicts:
        shown = "；".join(conflicts[:3])
        more = f"……等 {len(conflicts)} 条" if len(conflicts) > 3 else ""
        print(f"自冲突 {len(conflicts)} 条：{shown}{more}——{stats_line(n_exec, n_skip)}")
        return 1
    tail = f"（{'；'.join(notes)}）" if notes else ""
    print(f"零命中判据自检通过，无自冲突{tail}——{stats_line(n_exec, n_skip)}")
    return 0


DECL = re.compile(
    r"^\s*>\s*溯源[：:]\s*rt-plan\.md\s+v\S+\s+\S+\s+判据\s*(\d+)\s*条"
    r"\s*→\s*本包\s*(\d+)\s*条"
    r"\s*[（(]\s*新增\s*(\d+)\s*[、,]\s*合并\s*(\d+)\s*[、,]\s*删除\s*(\d+)"
    r"\s*(?:[：:]\s*(?P<reason>.*?))?[）)]\s*[。.；;]?\s*$")


def g113():
    n_exec = n_skip = 0
    bad, ok = [], ""
    for p in files:
        base = os.path.basename(p)
        _, sec3 = read_pack(p)
        if sec3 is None:
            n_skip += 1  # 无 §3 节，声明无从校验
            continue
        n_exec += 1
        hit = None
        has_prefix = False   # DI-026：区分「格式不合」与「压根没有」
        for no, ln in sec3:
            m = DECL.match(ln)
            if m:
                hit = (no, m)
            elif re.match(r"^\s*>\s*溯源[：:]", ln):
                has_prefix = (no, ln.strip())
                break
        if hit is None:
            if has_prefix:
                # DI-026：起头对但整行不匹配——报「格式不合」并回显原行，
                # 不得报成「缺声明行」（后者会让作者去补一条已存在的声明，越补越乱）
                bad.append(f"{base}:{has_prefix[0]} 溯源声明格式不合（起头正确、整行不匹配）："
                           f"{has_prefix[1][:60]}")
            else:
                bad.append(f"{base} §3 缺溯源声明行")
            continue
        no, m = hit
        M, Kp, a, b, c = (int(m.group(i)) for i in range(1, 6))
        expect = M + a - b - c  # 加减账：新增是加项，合并/删除是减项
        if expect != Kp:
            bad.append(f"{base}:{no} 加减账不自洽：{M}+新增{a}-合并{b}-删除{c}"
                       f"={expect} ≠ 本包 {Kp} 条")
        elif c > 0 and not (m.group("reason") or "").strip():
            bad.append(f"{base}:{no} 删除 {c} 条但未给非空理由")
        elif not ok:
            ok = f"{base} 判据 {M} 条 → 本包 {Kp} 条"
    if bad:
        shown = "；".join(bad[:3])
        more = f"……等 {len(bad)} 份" if len(bad) > 3 else ""
        print(f"溯源声明违规 {len(bad)} 份：{shown}{more}——{stats_line(n_exec, n_skip)}")
        return 1
    head = f"（{ok}）" if ok else ""
    print(f"溯源声明齐全且加减账自洽{head}——{stats_line(n_exec, n_skip)}")
    return 0


if mode == "g112":
    sys.exit(g112())
if mode == "g113":
    sys.exit(g113())
die(f"未知 mode: {mode}")
PY
}

rtg_check_handoff_pack_self_consistent() { # G112：零命中判据 vs 本包 §1/§2 正文（error）
  local out rc=0
  out="$(rtg_handoff_scan g112 "$1" "$2")" || rc=$?
  (( rc != 3 )) || rtg_die "handoff 扫描器内部错误（g112，见上方 stderr）"
  printf '%s' "$out"
  (( rc == 0 )) && return 0
  # ── status=done 的作用域豁免（2026-08-22）──────────────────────────────────
  # 扫描照跑、结论照实打印（上一行），仅不判失败。理由：本判据的时机是「冻结
  # 任务包**前**」（本函数对应 rt-gates.yaml G112 的 title 与 task-pack-projection.md
  # §8 判据表），任务包随 RT 关闭即成历史归档——改它就是篡改记录，而红着也无从
  # 修复。RT-125 的 3 条自冲突恰是该判据的事故原型，已由其 retrospective 与
  # task-pack-projection.md §7 元规则 4 长期留档，门禁无需再红一遍。
  # 边界：**只认 status == done**。in-progress/created 照常硬失败；meta 缺
  # status 字段亦不豁免（fixture root-u1/RT-501 即此形态，g112-u5-selfconflict
  # 用例继续断言 FAIL）。绕过成本：把 status 改成 done 要同时过 G111 复盘 Gate
  # 与 G109 index 条目，且改动在 git diff 里可见。
  local st; st="$(rtg_meta_field "$1/RT/$2/meta.yaml" status)"
  if [[ "$st" == "done" ]]; then
    printf '｜status=done，任务包已归档、冻结前自检时机已过，不追溯（判据仍实跑，结论如上）'
    return 0
  fi
  return 1
}

rtg_check_handoff_criteria_declared() { # G113：§3 溯源声明自洽（warn）
  local out rc=0
  out="$(rtg_handoff_scan g113 "$1" "$2")" || rc=$?
  (( rc != 3 )) || rtg_die "handoff 扫描器内部错误（g113，见上方 stderr）"
  printf '%s' "$out"
  (( rc == 0 )) && return 0
  return 1
}

# ── U6 辅助：分支名 → RT-ID（门禁只管 RT 车道 feature/RT-NNN-*）─────────────
rtg_branch_rt_id() { # $1=分支名；stdout=RT-ID（提不出则空串）
  if [[ "$1" =~ ^feature/(RT-[0-9]+) ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  fi
}

# ── U6 检查函数：pre-commit hook 安装自检（G001，告警级）────────────────────
# 出口判据（unit-U6-input.md §3-4）：自检
# $(git rev-parse --path-format=absolute --git-common-dir)/hooks/pre-commit。
# worktree 内 .git 是文件不是目录，故必须走 --git-common-dir（rt-plan v5 U6 ⚠ 行）。
# --root 非 git 顶层（fixture 子目录场景）时自检不适用，恒 PASS——保证 fixture
# 断言不随宿主机 hook 安装状态漂移（确定性要求）。
rtg_check_repo_pre_commit_hook_installed() {
  local root="$1" toplevel common hook
  toplevel="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null)" || toplevel=""
  if [[ -z "$toplevel" ]]; then
    printf 'root 不在 git 仓库内，pre-commit 安装自检不适用'
    return 0
  fi
  if [[ "$(cd "$toplevel" && pwd -P)" != "$(cd "$root" && pwd -P)" ]]; then
    printf 'root 非 git 仓库顶层（fixture/子目录场景），pre-commit 安装自检不适用'
    return 0
  fi
  common="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || { printf 'git-common-dir 解析失败，自检不适用'; return 0; }
  hook="$common/hooks/pre-commit"
  if [[ -f "$hook" ]]; then
    printf 'pre-commit hook 已安装（%s）——防绕底线层生效中' "$hook"
    return 0
  fi
  printf 'pre-commit hook 未安装（%s 不存在）——防绕底线层未生效（观察期启用前属预期状态）' "$hook"
  return 1
}

# ── U2b 交叉引用扫描器（判据 #10 / G110）────────────────────────────────────
# 检查内容：被检 RT 目录内 .md 的 file:line（:NN）与同文档 §N 交叉引用存在性。
# 判据口径（任务包 unit-U2b-input.md §1，源自 U1 回执 ③-7 实测）：
#   · 短名引用（如 rt-lite.md:426）走 basename→全路径索引；歧义时任一候选
#     行号命中即 OK，全部不命中才报 DANGLING（宽侧，服务 U3 误报率）
#   · 仓库外引用豁免（两层）：绝对路径 / ../ 越界在抽取层即不匹配；仓库内
#     查无候选 → UNRESOLVED（外部/已失效不可判），skip 不报
#   · dot 前缀路径（.aodw-next/... 等）在抽取正则里显式允许，防截断假判
#   · file:line 判定 = 文件存在且行号 ≤ 总行数——行号存在性是弱判据，检不出
#     行内容漂移（已知局限，本单元不解决）
#   · §N 只查同文档内引用；疑似跨文档 §（窗口内有文件名/文档指称词）skip
# 退出码（python 侧）：0=无悬空 10=有悬空 3=内部错误（与判据结果可区分）
rtg_xref_scan() { # $1=root $2=RT-ID $3=mode(summary|detail) $4=scan_rules(0|1) $5=扩展名枚举
  "$RTG_PYTHON" - "$1" "$2" "$3" "$4" "${5-}" <<'PY'
import os, re, sys


def main():
    root = os.path.abspath(sys.argv[1])
    rt_id = sys.argv[2]
    mode = sys.argv[3]                     # summary | detail
    scan_rules = sys.argv[4] == "1"
    exts = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else \
        "md|py|sh|json|yaml|yml|toml|txt"
    ext_set = set(exts.split("|"))

    # 抽取正则：首段显式允许一个前导 dot（.aodw-next/...，U1 实测截断坑）；
    # 左边界排除 [\w/.]——绝对路径与 ../ 越界引用在抽取层即不匹配（仓库外豁免）
    ref_pat = re.compile(
        r'(?<![\w/.])'
        r'(\.?[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)*'
        r'\.(?:' + exts + r')):(\d+)(?:-(\d+))?')
    sec_pat = re.compile(r'§\s*(\d+(?:\.\d+)*)')
    head_pat = re.compile(r'^\s{0,3}#{1,6}\s*(.*)$')
    headnum_pat = re.compile(r'^[§\s]*(\d+(?:\.\d+)*)')
    circled = dict(zip('①②③④⑤⑥⑦⑧⑨⑩', [str(i) for i in range(1, 11)]))
    # 跨文档 § 启发式：§ 前 30 字符窗口内出现文件名扩展或文档指称词 → skip
    xdoc_file = re.compile(r'\.(?:' + exts + r')\b')
    xdoc_tail = re.compile(
        r'(任务包|回执|计划|规划|宪章|模板|手册|附录|规则|文档|清单'
        r'|spike|input|receipt|plan|template|probe)'
        r'\s*(?:的|之|中|里)?\s*[（(]?\s*$', re.IGNORECASE)

    prune = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
             '.pytest_cache'}

    def md_files_under(d):
        out = []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = sorted(x for x in dirnames if x not in prune)
            out += [os.path.join(dirpath, f) for f in filenames
                    if f.endswith('.md')]
        return sorted(out)

    rt_dir = os.path.join(root, 'RT', rt_id)
    docs = md_files_under(rt_dir)
    if scan_rules:
        docs += md_files_under(os.path.join(root, '.aodw-next'))

    # basename → 全路径索引（os.walk 默认不跟符号链接，cases→NAS 不会被走进；
    # 另剪掉 .claude/worktrees，避免 worktree 副本把候选翻倍）
    index = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in prune and not (rel == '.claude' and d == 'worktrees'))
        for fn in filenames:
            if '.' in fn and fn.rsplit('.', 1)[1] in ext_set:
                index.setdefault(fn, []).append(os.path.join(dirpath, fn))

    lc_cache = {}

    def line_count(p):
        if p not in lc_cache:
            try:
                with open(p, 'rb') as f:
                    data = f.read()
                n = data.count(b'\n')
                if data and not data.endswith(b'\n'):
                    n += 1
            except OSError:
                n = -1
            lc_cache[p] = n
        return lc_cache[p]

    rows = []  # (verdict, doc_rel, lineno, ref_text, note)
    st = {'OK': 0, 'OK-AMBIG': 0, 'DANGLING': 0, 'UNRESOLVED': 0,
          'SEC-OK': 0, 'SEC-DANGLING': 0, 'XDOC-SKIP': 0}

    for doc in docs:
        doc_rel = os.path.relpath(doc, root)
        with open(doc, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()

        # 标题树：编号集合（## 3. / ### 3.2 / ## §7 / ## ③ 等变体）
        sections = set()
        for ln in lines:
            hm = head_pat.match(ln)
            if not hm:
                continue
            txt = hm.group(1).strip()
            if txt[:1] in circled:
                sections.add(circled[txt[0]])
                continue
            nm = headnum_pat.match(txt)
            if nm:
                sections.add(nm.group(1))

        def sec_exists(num):
            return num in sections or \
                any(s.startswith(num + '.') for s in sections)

        doc_dir = os.path.dirname(doc)
        bases = []
        for b in (root, doc_dir, rt_dir, os.path.join(root, '.aodw-next')):
            if b not in bases:
                bases.append(b)

        for i, line in enumerate(lines, 1):
            # ── file:NN 引用 ──
            for m in ref_pat.finditer(line):
                rel_ref, start = m.group(1), int(m.group(2))
                ref_text = '%s:%s%s' % (
                    rel_ref, m.group(2),
                    '-' + m.group(3) if m.group(3) else '')
                cands = []
                for b in bases:
                    c = os.path.normpath(os.path.join(b, rel_ref))
                    if c != root and not c.startswith(root + os.sep):
                        continue  # 越出仓库的基准组合不取（仓库外豁免）
                    if os.path.isfile(c):
                        cands.append(os.path.realpath(c))
                cands = list(dict.fromkeys(cands))
                via = '直连'
                if not cands:
                    bcands = index.get(os.path.basename(rel_ref), [])
                    tail = '/' + rel_ref.lstrip('./')
                    suffixed = [p for p in bcands
                                if p.replace(os.sep, '/').endswith(tail)]
                    cands = list(dict.fromkeys(
                        os.path.realpath(p) for p in (suffixed or bcands)))
                    via = 'basename 索引'
                if not cands:
                    rows.append(('UNRESOLVED', doc_rel, i, ref_text,
                                 '仓库内无此文件（外部/已失效不可判，宽侧跳过不报）'))
                    st['UNRESOLVED'] += 1
                    continue
                hits = [p for p in cands if line_count(p) >= start]
                if hits and len(cands) == 1:
                    rows.append(('OK', doc_rel, i, ref_text,
                                 '' if via == '直连' else '短名经 basename 索引解析'))
                    st['OK'] += 1
                elif hits:
                    rows.append(('OK-AMBIG', doc_rel, i, ref_text,
                                 '候选 %d 个、行号命中 %d 个（任一命中即 OK，宽侧）'
                                 % (len(cands), len(hits))))
                    st['OK-AMBIG'] += 1
                else:
                    mx = max(line_count(p) for p in cands)
                    rows.append(('DANGLING', doc_rel, i, ref_text,
                                 '候选 %d 个（%s）全部不足 %d 行（最大 %d 行）'
                                 % (len(cands), via, start, mx)))
                    st['DANGLING'] += 1

            # ── §N 引用（标题行是定义不是引用；跨文档 § skip）──
            if head_pat.match(line):
                continue
            line_xdoc = False
            for m in sec_pat.finditer(line):
                num = m.group(1)
                window = line[max(0, m.start() - 30):m.start()]
                if line_xdoc or xdoc_file.search(window) \
                        or xdoc_tail.search(window):
                    rows.append(('XDOC-SKIP', doc_rel, i, '§' + num,
                                 '疑似跨文档 § 引用（太歧义，skip 不判）'))
                    st['XDOC-SKIP'] += 1
                    line_xdoc = True
                    continue
                if sec_exists(num):
                    rows.append(('SEC-OK', doc_rel, i, '§' + num, ''))
                    st['SEC-OK'] += 1
                else:
                    rows.append(('SEC-DANGLING', doc_rel, i, '§' + num,
                                 '同文档内无编号 %s 的标题' % num))
                    st['SEC-DANGLING'] += 1

    n_file = st['OK'] + st['OK-AMBIG'] + st['DANGLING'] + st['UNRESOLVED']
    n_sec = st['SEC-OK'] + st['SEC-DANGLING'] + st['XDOC-SKIP']
    dl, ds = st['DANGLING'], st['SEC-DANGLING']
    summary = ('文档 %d 篇：file:line %d 处、§ %d 处；悬空 file:line %d、悬空 § %d；'
               'OK %d（歧义命中 %d）、§OK %d、未解析跳过 %d、跨文档 § 跳过 %d'
               % (len(docs), n_file, n_sec, dl, ds,
                  st['OK'] + st['OK-AMBIG'], st['OK-AMBIG'], st['SEC-OK'],
                  st['UNRESOLVED'], st['XDOC-SKIP']))
    if dl + ds > 0 and mode == 'summary':
        summary += '——详见 --scan-refs'

    if mode == 'detail':
        for verdict, drel, lineno, ref_text, note in rows:
            loc = '%s:%d' % (drel, lineno)
            extra = '（%s）' % note if note else ''
            print('%-13s%-40s %s%s' % (verdict, loc, ref_text, extra))
        print()
        print('== 统计 ==')
        for k in ('OK', 'OK-AMBIG', 'DANGLING', 'UNRESOLVED',
                  'SEC-OK', 'SEC-DANGLING', 'XDOC-SKIP'):
            print('  %-13s%d' % (k, st[k]))
        print('  %-13s%d' % ('TOTAL', sum(st.values())))
        print(summary)
    else:
        print(summary)

    sys.exit(10 if dl + ds > 0 else 0)


try:
    main()
except SystemExit:
    raise
except Exception as e:
    print('xref 扫描内部错误: %s: %s' % (type(e).__name__, e), file=sys.stderr)
    sys.exit(3)
PY
}

rtg_check_xref_doc_refs_resolve() { # 判据 10（G110）：RT 目录内 .md 交叉引用存在性
  local root="$1" rt_id="$2" exts="${3-}" out rc=0
  out="$(rtg_xref_scan "$root" "$rt_id" summary 0 "$exts")" || rc=$?
  if (( rc != 0 && rc != 10 )); then
    # 扫描器崩溃不是判据结果——与 rt-gates.yaml 不同步同类，fail-closed
    rtg_die "交叉引用扫描器内部错误（rc=$rc，见上方 stderr）"
  fi
  printf '%s' "$out"
  (( rc == 0 )) && return 0
  return 1
}

rtg_check_deferred_refs_resolve() { # G114：RT 声称转出的 DI 编号必须在台账里真实存在
  # 缘由（2026-08-22，RT-143 实事故）：收口时用「if DI-024 not in 台账 then 追加」
  # 做去重，而该编号早被占用 ⇒ 条件为假 ⇒ 整段静默跳过 ⇒ 台账一个字没写；
  # 而 meta.yaml 与 commit 均已宣称「遗留项已转出 DI-024/025」。若非人工追问，
  # 两笔账随 RT 关闭彻底消失，现场还留着「已转出」的假记录。
  # 这与 RT-125 建立本台账时的原始缘由同构（把「转出」在进度里勾 [x] 却全仓无落点）。
  local root="$1" rt_id="$2" rt_dir="$1/RT/$2" ledger="$1/RT/_deferred-items.md"
  [[ -d "$rt_dir" ]] || { printf 'RT 目录不存在，跳过'; return 0; }

  # 只扫 RT 自己的「声明面」——meta.yaml 与 index.yaml 的本 RT 条目。
  # 不扫 rt-lite/rt-plan/audit/handoff：那些地方提到 DI 编号多为「查重时扫过、
  # 主题不同、无需认领」的论证，不是转出声明，全扫会把论证误判成声明。
  # 只认**结构化字段** `deferred_items_raised:` 的列表值——不扫自由注释。
  # 理由：meta 的注释里合法地会出现别的 DI 编号（查重论证、订正说明、
  # 「主题不同无需认领」的记录）。把注释也当声明会误报，且会逼人不敢在注释里
  # 提编号——那反而损失信息。转出是有合同意义的动作，就该写进结构化字段。
  local refs=""
  if [[ -f "$rt_dir/meta.yaml" ]]; then
    refs="$(awk '
      /^deferred_items_raised:/ { inblk=1; next }
      inblk && /^[a-zA-Z_]+:/ { inblk=0 }
      inblk && /^[[:space:]]*-[[:space:]]*DI-[0-9]{3}/ {
        match($0, /DI-[0-9]{3}/); print substr($0, RSTART, RLENGTH)
      }
    ' "$rt_dir/meta.yaml" | sort -u || true)"
  fi

  if [[ -z "$refs" ]]; then
    printf '本 RT 的 meta.yaml 无 deferred_items_raised 字段，无转出声明可校验'
    return 0
  fi
  if [[ ! -f "$ledger" ]]; then
    printf '声明了 DI 转出（%s）但 RT/_deferred-items.md 不存在' "$(printf '%s' "$refs" | tr '\n' ' ')"
    return 1
  fi

  local missing="" found=0 total=0 di
  while IFS= read -r di; do
    [[ -n "$di" ]] || continue
    total=$((total+1))
    # 必须锚定台账的**条目标题行**（`### DI-0NN — 标题`），不能全文 grep：
    # 台账正文里会互相引用兄弟条目编号，全文搜会把「被别人提到过」误判成
    # 「自己有条目」——与 G109 index.entry_present 同一个坑。
    local blk
    blk="$(awk -v di="$di" '
      $0 ~ "^#{2,4}[[:space:]]+" di "([[:space:]]|—|-|$)" { inblk=1; print; next }
      inblk && /^#{2,4}[[:space:]]+DI-/ { inblk=0 }
      inblk { print }
    ' "$ledger")"
    if [[ -z "$blk" ]]; then
      missing+="${di}(无此条目) "
    elif ! grep -qE "\*\*发现于\*\*[^|]*\|[^|]*${rt_id}([^0-9]|$)" <<< "$blk"; then
      # 编号存在但「发现于」不是本 RT ⇒ 该编号被别人占用，你的内容根本没落盘。
      # 这正是 RT-143 踩的坑：DI-024 存在（内容是别人的孤儿测试问题），
      # 只验存在性会 PASS，把「宣称转出、实际一字未写」放行。
      missing+="${di}(编号被占,发现于非${rt_id}) "
    else
      found=$((found+1))
    fi
  done <<< "$refs"

  if [[ -n "$missing" ]]; then
    printf '声明转出的 DI 与台账不符：%s（扫描 %d 个 / 命中 %d 个）——编号不存在，或该编号被别的 RT 占用（你的内容没落盘）' \
      "${missing% }" "$total" "$found"
    return 1
  fi
  printf 'DI 转出声明与台账一致——扫描 %d 个 / 全部命中' "$total"
  return 0
}

rtg_check_deferred_claims_resolve() { # G115：认领声明与台账状态一致（G114 的对偶）
  # G114 管「转出方向」：声称 raised 的编号必须在台账有真实属于自己的条目。
  # 本判据管「认领方向」：声称 claimed 的编号必须存在，且台账状态列已标注本 RT。
  # 防的是「声称认领、台账还写着未认领」——那样别人扫台账会重复认领同一条，
  # 正是 rt-manager §3.4b-2 第 3 步「立即改状态、不要等收口」要防的。
  local root="$1" rt_id="$2" rt_dir="$1/RT/$2" ledger="$1/RT/_deferred-items.md"
  [[ -d "$rt_dir" ]] || { printf 'RT 目录不存在，跳过'; return 0; }

  local refs=""
  if [[ -f "$rt_dir/meta.yaml" ]]; then
    refs="$(awk '
      /^deferred_items_claimed:/ { inblk=1; next }
      inblk && /^[a-zA-Z_]+:/ { inblk=0 }
      inblk && /^[[:space:]]*-[[:space:]]*DI-[0-9]{3}/ {
        match($0, /DI-[0-9]{3}/); print substr($0, RSTART, RLENGTH)
      }
    ' "$rt_dir/meta.yaml" | sort -u || true)"
  fi

  if [[ -z "$refs" ]]; then
    printf '本 RT 的 meta.yaml 无 deferred_items_claimed 字段，无认领声明可校验'
    return 0
  fi
  if [[ ! -f "$ledger" ]]; then
    printf '声明了 DI 认领（%s）但 RT/_deferred-items.md 不存在' "$(printf '%s' "$refs" | tr '\n' ' ')"
    return 1
  fi

  local bad="" ok=0 total=0 di blk stat
  while IFS= read -r di; do
    [[ -n "$di" ]] || continue
    total=$((total+1))
    blk="$(awk -v di="$di" '
      $0 ~ "^#{2,4}[[:space:]]+" di "([[:space:]]|—|-|$)" { inblk=1; print; next }
      inblk && /^#{2,4}[[:space:]]+DI-/ { inblk=0 }
      inblk { print }
    ' "$ledger")"
    if [[ -z "$blk" ]]; then
      bad+="${di}(无此条目) "
      continue
    fi
    # 只取状态行的**值单元格**（第 3 个 | 分隔字段）来判。
    # 不能在整行任意位置 grep RT-ID：状态列常带长描述，描述里提到本 RT 是常态
    # ——那样即使状态还写着「未认领」，也会因描述提了一句而误判成 PASS（实测踩过）。
    stat="$(grep -E '^\|[[:space:]]*\*\*状态\*\*' <<< "$blk" | head -1 | awk -F'|' '{print $3}')"
    if [[ -z "$stat" ]]; then
      bad+="${di}(无状态行) "
    elif grep -q '未认领' <<< "$stat"; then
      bad+="${di}(台账仍标未认领) "
    elif ! grep -qE "${rt_id}([^0-9]|$)" <<< "$stat"; then
      bad+="${di}(状态未标注${rt_id}) "
    else
      ok=$((ok+1))
    fi
  done <<< "$refs"

  if [[ -n "$bad" ]]; then
    printf '认领声明与台账状态不符：%s（扫描 %d 个 / 一致 %d 个）——台账状态列须写明认领它的 RT' \
      "${bad% }" "$total" "$ok"
    return 1
  fi
  printf 'DI 认领声明与台账状态一致——扫描 %d 个 / 全部命中' "$total"
  return 0
}

rtg_dispatch_check() { # $1=impl $2=root $3=RT-ID $4=param；未知 impl → die（fail-closed）
  local impl="$1"
  case "$impl" in
    placeholder.rt_dir_exists) rtg_check_placeholder_rt_dir_exists "$2" "$3" ;;
    meta.type_valid)           rtg_check_meta_type_valid "$2" "$3" "${4-}" ;;
    meta.type_present)         rtg_check_meta_type_present "$2" "$3" ;;
    meta.profile_present)      rtg_check_meta_profile_present "$2" "$3" ;;
    text.changelog_present)    rtg_check_text_changelog_present "$2" "$3" "${4-}" ;;
    text.csf_review_present)   rtg_check_text_csf_review_present "$2" "$3" ;;
    index.entry_present)       rtg_check_index_entry_present "$2" "$3" ;;
    meta.retrospective_present) rtg_check_meta_retrospective_present "$2" "$3" ;;
    xref.doc_refs_resolve)     rtg_check_xref_doc_refs_resolve "$2" "$3" "${4-}" ;;
    repo.pre_commit_hook_installed) rtg_check_repo_pre_commit_hook_installed "$2" ;;
    handoff.pack_self_consistent) rtg_check_handoff_pack_self_consistent "$2" "$3" ;;
    deferred.refs_resolve)     rtg_check_deferred_refs_resolve "$2" "$3" ;;
    deferred.claims_resolve)   rtg_check_deferred_claims_resolve "$2" "$3" ;;
    handoff.criteria_declared)  rtg_check_handoff_criteria_declared "$2" "$3" ;;
    *) rtg_die "判据 impl 未实现: $impl（rt-gates.yaml 与脚本不同步，fail-closed）" ;;
  esac
}

# ── CLI 形态主流程 ───────────────────────────────────────────────────────────
rtg_run_cli() { # 使用全局：RTG_ROOT RTG_RT RTG_FORMAT RTG_GATES_FILE RTG_LIST_ONLY
  [[ -n "$RTG_ROOT" ]] || rtg_die "CLI 形态必须提供 --root <dir>"
  [[ -d "$RTG_ROOT" ]] || rtg_die "--root 目录不存在: $RTG_ROOT"
  [[ -f "$RTG_GATES_FILE" ]] || rtg_die "判据文件不存在: $RTG_GATES_FILE"

  local gates_tsv
  gates_tsv="$(rtg_load_gates "$RTG_GATES_FILE")" \
    || rtg_die "判据加载失败（见上方 stderr）: $RTG_GATES_FILE"
  local n_gates
  n_gates="$(printf '%s\n' "$gates_tsv" | grep -c .)" || true

  if [[ "$RTG_LIST_ONLY" == "1" ]]; then
    printf 'rt-guard %s — 已加载判据 %s 条（%s）\n' \
      "$RT_GUARD_VERSION" "$n_gates" "$RTG_GATES_FILE"
    local id name sev scope enabled impl title param
    while IFS=$'\t' read -r id name sev scope enabled impl title param; do
      [[ -n "$id" ]] || continue
      printf '  [%s] %-24s severity=%-5s scope=%-4s enabled=%-5s impl=%s%s\n' \
        "$id" "$name" "$sev" "$scope" "$enabled" "$impl" \
        "${param:+ param=${param}}"
    done <<< "$gates_tsv"
    rtg_finish 0
  fi

  # 被检 RT 集合
  local -a rt_ids=()
  if [[ -n "$RTG_RT" ]]; then
    rt_ids=("$RTG_RT")
  else
    [[ -d "$RTG_ROOT/RT" ]] || rtg_die "被检仓库缺少 RT/ 目录: $RTG_ROOT/RT"
    local d
    for d in "$RTG_ROOT"/RT/RT-*/; do
      [[ -d "$d" ]] || continue
      d="${d%/}"
      rt_ids+=("${d##*/}")
    done
  fi

  # 逐 RT × 逐判据执行；结果行式收集: rt_id \t gate_id \t severity \t status \t detail
  local results="" rt_id id name sev scope enabled impl title param detail status
  local n_pass=0 n_error=0 n_warn=0
  for rt_id in ${rt_ids[@]+"${rt_ids[@]}"}; do
    while IFS=$'\t' read -r id name sev scope enabled impl title param; do
      [[ -n "$id" ]] || continue
      if [[ "$enabled" != "true" ]]; then
        results+="$rt_id"$'\t'"$id"$'\t'"$sev"$'\t'"SKIP"$'\t'"判据未启用"$'\n'
        continue
      fi
      if detail="$(rtg_dispatch_check "$impl" "$RTG_ROOT" "$rt_id" "${param-}")"; then
        status="PASS"; n_pass=$((n_pass + 1))
      else
        if [[ "$sev" == "error" ]]; then
          status="FAIL"; n_error=$((n_error + 1))
        else
          status="WARN"; n_warn=$((n_warn + 1))
        fi
      fi
      results+="$rt_id"$'\t'"$id"$'\t'"$sev"$'\t'"$status"$'\t'"$detail"$'\n'
    done <<< "$gates_tsv"
  done

  local exit_code=0
  (( n_error > 0 )) && exit_code=1

  if [[ "$RTG_FORMAT" == "json" ]]; then
    printf '%s' "$results" | "$RTG_PYTHON" -c '
import json, sys
rows = []
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    rt_id, gate_id, sev, status, detail = line.split("\t", 4)
    rows.append({"rt": rt_id, "gate": gate_id, "severity": sev,
                 "status": status, "detail": detail})
summary = {"version": sys.argv[1], "checked_rts": sys.argv[2].split(",") if sys.argv[2] else [],
           "pass": int(sys.argv[3]), "error": int(sys.argv[4]), "warn": int(sys.argv[5]),
           "verdict": "fail" if int(sys.argv[4]) > 0 else "pass", "results": rows}
print(json.dumps(summary, ensure_ascii=False, indent=2))
' "$RT_GUARD_VERSION" "$(IFS=,; printf '%s' "${rt_ids[*]-}")" "$n_pass" "$n_error" "$n_warn" \
      || rtg_die "json 输出渲染失败"
  else
    printf 'rt-guard %s — root=%s 判据 %s 条，被检 RT %s 个\n' \
      "$RT_GUARD_VERSION" "$RTG_ROOT" "$n_gates" "${#rt_ids[@]}"
    local line
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      IFS=$'\t' read -r rt_id id sev status detail <<< "$line"
      printf '  %-4s %-8s %-6s %-5s %s\n' "$status" "$rt_id" "$id" "$sev" "$detail"
    done <<< "$results"
    printf 'result: %s (pass=%s error=%s warn=%s)\n' \
      "$([[ $exit_code -eq 0 ]] && printf 'pass' || printf 'fail')" \
      "$n_pass" "$n_error" "$n_warn"
  fi
  # 出口判据 3（U2a）：仅告警 → exit 0，但 stderr 给显著提示（json 模式亦然，
  # stdout 纯 JSON 通道不受污染）
  if (( exit_code == 0 && n_warn > 0 )); then
    printf 'rt-guard: ⚠ 告警 %s 条（warn 级不阻断，exit 0）——详见结果中 WARN 行\n' \
      "$n_warn" >&2
  fi
  rtg_finish "$exit_code"
}

# ── --scan-refs 明细扫描主流程（G110 的报告形态，告警级恒 exit 0）────────────
rtg_run_scan_refs() {
  [[ -n "$RTG_ROOT" ]] || rtg_die "--scan-refs 需要 --root <dir>"
  [[ -d "$RTG_ROOT" ]] || rtg_die "--root 目录不存在: $RTG_ROOT"
  [[ -n "$RTG_RT" ]] || rtg_die "--scan-refs 需要 --rt <RT-ID>（扫描范围默认限被检 RT 目录内）"
  [[ -f "$RTG_GATES_FILE" ]] || rtg_die "判据文件不存在: $RTG_GATES_FILE"
  # 扩展名枚举取 G110 条目的 param（单一事实来源）；取不到用扫描器内置缺省
  local gates_tsv exts
  gates_tsv="$(rtg_load_gates "$RTG_GATES_FILE")" \
    || rtg_die "判据加载失败（见上方 stderr）: $RTG_GATES_FILE"
  exts="$(printf '%s\n' "$gates_tsv" \
    | awk -F'\t' '$6 == "xref.doc_refs_resolve" { print $8; exit }')" || exts=""
  local rc=0
  rtg_xref_scan "$RTG_ROOT" "$RTG_RT" detail "$RTG_SCAN_RULES" "$exts" || rc=$?
  if (( rc != 0 && rc != 10 )); then
    rtg_die "交叉引用扫描器内部错误（rc=$rc，见上方 stderr）"
  fi
  if (( rc == 10 )); then
    printf 'rt-guard: ⚠ 交叉引用存在悬空（G110 告警级不阻断，exit 0）\n' >&2
  fi
  rtg_finish 0
}

# ── hook 形态主流程 ──────────────────────────────────────────────────────────
# 骨架职责：正确分派 + 防死循环 + 命令解析；判定动作（拦什么）留给 U2a/U2b。
# 纪律：stdout 保留给结构化裁决 JSON（exit 0 时以 { 开头的 stdout 会被宿主解析，
#       见 spike §5.4），诊断一律走 stderr。
rtg_run_hook() {
  local input event
  input="$(cat)" || rtg_die "hook 形态读取 stdin 失败"
  [[ -n "$input" ]] || rtg_die "hook 形态 stdin 为空"
  event="$(rtg_json_field "$input" "hook_event_name")" \
    || rtg_die "hook stdin 不是合法 JSON"
  [[ -n "$event" ]] || rtg_die "hook stdin 缺少 hook_event_name"

  case "$event" in
    Stop|SubagentStop)
      local active
      active="$(rtg_json_field "$input" "stop_hook_active")" || active=""
      if [[ "$active" == "true" ]]; then
        # 防死循环（spike §4.4）：第二轮一律放行，不得再次阻断
        printf 'rt-guard[hook]: %s stop_hook_active=true -> allow (loop guard)\n' \
          "$event" >&2
        rtg_finish 0
      fi
      # U1 骨架：Stop 判据尚未接入（U2a/U2b 落位后在此评估产出完整性判据）
      printf 'rt-guard[hook]: %s stop_hook_active=%s -> allow (skeleton, no stop gates)\n' \
        "$event" "${active:-false}" >&2
      rtg_finish 0
      ;;
    PreToolUse)
      local tool cmd action
      tool="$(rtg_json_field "$input" "tool_name")" || tool=""
      if [[ "$tool" != "Bash" ]]; then
        printf 'rt-guard[hook]: PreToolUse tool=%s -> allow (non-Bash)\n' \
          "${tool:-unknown}" >&2
        rtg_finish 0
      fi
      cmd="$(rtg_json_field "$input" "tool_input.command")" || cmd=""
      action="$(rtg_classify_command "$cmd")"
      if [[ "$action" != "git-commit" ]]; then
        printf 'rt-guard[hook]: PreToolUse detected action=%s -> allow (非 commit 动作不设门)\n' \
          "$action" >&2
        rtg_finish 0
      fi
      # ── U6 判定策略（unit-U6-input.md §2.1）：git commit → 对目标仓库当前
      # 分支提取 RT-ID（feature/RT-NNN-*）跑 CLI 判据；error → deny；仅 warn →
      # 放行；提不出 RT-ID → 放行（门禁只管 RT 车道）。
      local hook_cwd base target branch rt_id repo_root
      hook_cwd="$(rtg_json_field "$input" "cwd")" || hook_cwd=""
      base="${hook_cwd:-${RTG_ROOT:-$PWD}}"
      target="$(rtg_resolve_commit_target "$cmd" "$base")"
      branch="$(git -C "$target" branch --show-current 2>/dev/null)" || branch=""
      rt_id="$(rtg_branch_rt_id "$branch")"
      if [[ -z "$rt_id" ]]; then
        printf 'rt-guard[hook]: PreToolUse detected action=git-commit branch=%s -> allow (提不出 RT-ID，非 RT 车道不设门)\n' \
          "${branch:-<none>}" >&2
        rtg_finish 0
      fi
      repo_root="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || repo_root=""
      if [[ -z "$repo_root" ]]; then
        printf 'rt-guard[hook]: PreToolUse detected action=git-commit -> allow (目标 %s 非 git 仓库)\n' \
          "$target" >&2
        rtg_finish 0
      fi
      local cli_out cli_rc=0 fails reason
      cli_out="$(bash "$RTG_SCRIPT_DIR/rt-guard.sh" --root "$repo_root" \
        --rt "$rt_id" --gates "$RTG_GATES_FILE" 2>&1)" || cli_rc=$?
      if (( cli_rc == 0 )); then
        printf 'rt-guard[hook]: PreToolUse detected action=git-commit rt=%s -> allow (error 级判据全过)\n' \
          "$rt_id" >&2
        rtg_finish 0
      fi
      if (( cli_rc == 1 )); then
        fails="$(printf '%s\n' "$cli_out" | grep -E '^  FAIL' || true)"
        reason="AODW RT 门禁（rt-guard --hook-mode）：分支 ${branch} 归属 ${rt_id}，该 RT 有 error 级判据未过，本次 git commit 已被拒绝。
未过判据：
${fails}
请先修复 RT/${rt_id}/ 下的元数据/文档（或改用非 RT 车道分支），再重试提交。
判据明细：.aodw-next/tools/rt-guard.sh --root ${repo_root} --rt ${rt_id}"
      else
        reason="AODW RT 门禁（rt-guard --hook-mode）：判据评估内部错误（rc=${cli_rc}），按 fail-closed 拒绝本次 git commit。诊断尾部：
$(printf '%s\n' "$cli_out" | tail -n 5)"
      fi
      printf 'rt-guard[hook]: PreToolUse detected action=git-commit rt=%s -> deny (cli_rc=%s)\n' \
        "$rt_id" "$cli_rc" >&2
      rtg_emit_deny "PreToolUse" "$reason" || rtg_die "deny JSON 渲染失败"
      rtg_finish 0
      ;;
    *)
      printf 'rt-guard[hook]: 未处理事件 %s -> allow\n' "$event" >&2
      rtg_finish 0
      ;;
  esac
}

# ── pre-commit 形态主流程（U6：git 层不可绕过底线）───────────────────────────
# pre-commit 框架保证以仓库根为 cwd 调用（always_run + pass_filenames: false）。
# 判定策略与 hook 形态同源：分支名提取 RT-ID，提不出快速通过（不拦非 RT 车道）。
rtg_run_precommit() {
  local root branch rt_id
  root="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || rtg_die "--pre-commit 须在 git 仓库内运行"
  branch="$(git branch --show-current 2>/dev/null)" || branch=""
  rt_id="$(rtg_branch_rt_id "$branch")"
  if [[ -z "$rt_id" ]]; then
    printf 'rt-guard[pre-commit]: 分支 %s 提取不出 RT-ID（非 RT 车道），快速通过\n' \
      "${branch:-<detached>}" >&2
    rtg_finish 0
  fi
  printf 'rt-guard[pre-commit]: 分支 %s 归属 %s，执行 RT 门禁判据\n' \
    "$branch" "$rt_id" >&2
  RTG_ROOT="$root"
  RTG_RT="$rt_id"
  rtg_run_cli
}

# ── argv 解析与分派 ──────────────────────────────────────────────────────────
RTG_MODE="cli"
RTG_ROOT=""
RTG_RT=""
RTG_FORMAT="text"
RTG_GATES_FILE="$RTG_DEFAULT_GATES"
RTG_LIST_ONLY=0
RTG_SCAN_REFS=0
RTG_SCAN_RULES=0

if [[ $# -eq 0 ]]; then
  rtg_usage >&2
  rtg_die "缺少参数（--help 查看用法）"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      rtg_usage
      rtg_finish 0
      ;;
    --version)
      printf 'rt-guard %s\n' "$RT_GUARD_VERSION"
      rtg_finish 0
      ;;
    --hook-mode)
      RTG_MODE="hook"
      shift
      ;;
    --pre-commit)
      RTG_MODE="precommit"
      shift
      ;;
    --root)
      [[ $# -ge 2 ]] || rtg_die "--root 需要参数"
      RTG_ROOT="$2"
      shift 2
      ;;
    --rt)
      [[ $# -ge 2 ]] || rtg_die "--rt 需要参数"
      RTG_RT="$2"
      shift 2
      ;;
    --format)
      [[ $# -ge 2 ]] || rtg_die "--format 需要参数"
      case "$2" in
        text|json) RTG_FORMAT="$2" ;;
        *) rtg_die "--format 只接受 text|json，收到: $2" ;;
      esac
      shift 2
      ;;
    --gates)
      [[ $# -ge 2 ]] || rtg_die "--gates 需要参数"
      RTG_GATES_FILE="$2"
      shift 2
      ;;
    --self-test)
      # 自包含自检入口：判据与其 fixture 同在 .aodw-next/ 下，复制该目录到任何项目即可用
      _st_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures"
      [ -x "$_st_dir/run-fixtures.sh" ] || [ -f "$_st_dir/run-fixtures.sh" ] || {
        echo "rt-guard: 未找到 $_st_dir/run-fixtures.sh" >&2; rtg_finish 2; }
      bash "$_st_dir/run-fixtures.sh"; rtg_finish $?
      ;;
    --list-gates)
      RTG_LIST_ONLY=1
      shift
      ;;
    --scan-refs)
      RTG_SCAN_REFS=1
      shift
      ;;
    --scan-rules)
      RTG_SCAN_RULES=1
      shift
      ;;
    --parse-command)
      [[ $# -ge 2 ]] || rtg_die "--parse-command 需要参数"
      printf 'action=%s\n' "$(rtg_classify_command "$2")"
      rtg_finish 0
      ;;
    --emit-deny)
      [[ $# -ge 3 ]] || rtg_die "--emit-deny 需要 <event> <reason> 两个参数"
      rtg_emit_deny "$2" "$3" || rtg_die "deny JSON 渲染失败"
      rtg_finish 0
      ;;
    *)
      rtg_die "未知参数: $1（--help 查看用法）"
      ;;
  esac
done

if [[ "$RTG_SCAN_RULES" == "1" && "$RTG_SCAN_REFS" != "1" ]]; then
  rtg_die "--scan-rules 只能与 --scan-refs 连用（.aodw-next 全库扫描的独立开关，默认关）"
fi

if [[ "$RTG_MODE" == "hook" ]]; then
  [[ "$RTG_SCAN_REFS" == "1" ]] && rtg_die "--scan-refs 与 --hook-mode 互斥"
  rtg_run_hook
elif [[ "$RTG_MODE" == "precommit" ]]; then
  [[ "$RTG_SCAN_REFS" == "1" ]] && rtg_die "--scan-refs 与 --pre-commit 互斥"
  rtg_run_precommit
elif [[ "$RTG_SCAN_REFS" == "1" ]]; then
  rtg_run_scan_refs
else
  rtg_run_cli
fi

# 不可达：两个主流程都以 rtg_finish 显式收口；万一流到这里，EXIT trap 会兜成 2
