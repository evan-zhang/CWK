#!/usr/bin/env bash
# =============================================================================
# run-fixtures.sh — rt-guard.sh fixture 测试驱动
# =============================================================================
# 用法: bash .aodw-next/tools/fixtures/run-fixtures.sh
#   或: bash .aodw-next/tools/rt-guard.sh --self-test
# 退出码: 0 = 全部 PASS；1 = 存在 FAIL
#
# 本驱动守全局 shell 约束 set -euo pipefail（判据④的 -e 豁免仅针对 rt-guard.sh
# 单一文件——它要防 hook fail-open；测试驱动没有该语境）。
#
# 用例登记方式（U2a/U2b 增量接入点）：在「用例清单」区追加 run_case 调用即可，
# 断言原语：期望退出码 / stdout 或 stderr 的 grep 断言 / stdout 合法 JSON 断言。
# =============================================================================

set -euo pipefail

FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$FIXTURES_DIR/../../.." && pwd)"
GUARD="$REPO_ROOT/.aodw-next/tools/rt-guard.sh"
DATA="$FIXTURES_DIR/data"
ROOT_MINI="$FIXTURES_DIR/root-mini"
ROOT_U2A="$FIXTURES_DIR/root-u2a"          # U2a：各类缺陷 RT；整树无 RT/index.yaml
ROOT_U2A_OK="$FIXTURES_DIR/root-u2a-ok"    # U2a：全合规正例；含 RT/index.yaml
ROOT_U2B="$FIXTURES_DIR/root-u2b"          # U2b：交叉引用各类形态（G110）

[[ -f "$GUARD" ]] || { echo "找不到被测脚本: $GUARD" >&2; exit 1; }

N_PASS=0
N_FAIL=0
FAILED_NAMES=()

# run_case <name> <expected_exit> [--stdin <file>] [--grep-out <re>]
#          [--grep-err <re>] [--json-stdout] -- <argv...>
run_case() {
  local name="$1" expect="$2"
  shift 2
  local stdin_file="" grep_out="" grep_err="" json_stdout=0
  while [[ "$1" != "--" ]]; do
    case "$1" in
      --stdin)       stdin_file="$2"; shift 2 ;;
      --grep-out)    grep_out="$2";   shift 2 ;;
      --grep-err)    grep_err="$2";   shift 2 ;;
      --json-stdout) json_stdout=1;   shift   ;;
      *) echo "run_case 参数错误: $1" >&2; exit 1 ;;
    esac
  done
  shift # 去掉 --

  local out_f err_f rc=0
  out_f="$(mktemp)"; err_f="$(mktemp)"
  if [[ -n "$stdin_file" ]]; then
    "$@" > "$out_f" 2> "$err_f" < "$stdin_file" || rc=$?
  else
    "$@" > "$out_f" 2> "$err_f" < /dev/null || rc=$?
  fi

  local verdict="PASS" why=""
  if [[ "$rc" -ne "$expect" ]]; then
    verdict="FAIL"; why="期望 exit=$expect 实得 exit=$rc"
  elif [[ -n "$grep_out" ]] && ! grep -Eq "$grep_out" "$out_f"; then
    verdict="FAIL"; why="stdout 未命中 /$grep_out/"
  elif [[ -n "$grep_err" ]] && ! grep -Eq "$grep_err" "$err_f"; then
    verdict="FAIL"; why="stderr 未命中 /$grep_err/"
  elif [[ "$json_stdout" -eq 1 ]] && ! python3 -c 'import json,sys; json.load(sys.stdin)' < "$out_f" 2>/dev/null; then
    verdict="FAIL"; why="stdout 不是合法 JSON"
  fi

  if [[ "$verdict" == "PASS" ]]; then
    N_PASS=$((N_PASS + 1))
    printf 'PASS  %-28s (exit=%s)\n' "$name" "$rc"
  else
    N_FAIL=$((N_FAIL + 1))
    FAILED_NAMES+=("$name")
    printf 'FAIL  %-28s %s\n' "$name" "$why"
    printf '      cmd: %s\n' "$*"
    sed 's/^/      out| /' "$out_f"
    sed 's/^/      err| /' "$err_f"
  fi
  rm -f "$out_f" "$err_f"
}

echo "== rt-guard.sh fixtures（被测: ${GUARD}）=="

# ── 用例清单 ─────────────────────────────────────────────────────────────────

# [出口判据 1] --help 可解释
run_case cli-help 0 --grep-out '用法' \
  -- bash "$GUARD" --help

# [出口判据 2] 读入 rt-gates.yaml 并列出已加载判据
run_case cli-list-gates 0 --grep-out 'G000.*rt-dir-exists' \
  -- bash "$GUARD" --root "$ROOT_MINI" --list-gates

# [出口判据 2 延伸] 无 PyYAML 环境（python3 -S wrapper）走受限子集解析器兜底
run_case cli-list-gates-fallback 0 --grep-out 'G000.*rt-dir-exists' \
  -- env RT_GUARD_PYTHON="$DATA/python3-no-yaml.sh" \
     bash "$GUARD" --root "$ROOT_MINI" --list-gates

# [出口判据 3+5] 占位判据跑通，退出码可断言（--root 生效于假仓库）
run_case cli-placeholder-pass 0 --grep-out 'PASS.*RT-101.*G000' \
  -- bash "$GUARD" --root "$ROOT_MINI" --rt RT-101
run_case cli-placeholder-fail 1 --grep-out 'FAIL.*RT-999.*G000' \
  -- bash "$GUARD" --root "$ROOT_MINI" --rt RT-999

# [出口判据 4] fail-open 防护：不存在的 --root → exit 2（而非 1）
run_case cli-failopen-root 2 --grep-err 'rt-guard' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/no-such-root-xyz" --rt RT-101

# [骨架 I/O] --format json 输出合法 JSON
run_case cli-format-json 0 --json-stdout --grep-out '"verdict": "pass"' \
  -- bash "$GUARD" --root "$ROOT_MINI" --rt RT-101 --format json

# [出口判据 6] Stop + stop_hook_active=true → 直接放行（loop guard 路径）
run_case hook-stop-active 0 --stdin "$DATA/hook-stop-active.json" \
  --grep-err 'loop guard' \
  -- bash "$GUARD" --hook-mode
# 对照：首轮 Stop（active=false）走判据评估路径（骨架为空判据，放行）
run_case hook-stop-first 0 --stdin "$DATA/hook-stop-first.json" \
  --grep-err 'stop_hook_active=false' \
  -- bash "$GUARD" --hook-mode

# [hook I/O（U6 起为真判定）] benign 命令 → 非 commit 动作放行；commit 变体
# （-C 指向非 git 仓库目标）→ 提不出 RT-ID 放行。解析结果须正确标注在 stderr
run_case hook-pretooluse-benign 0 --stdin "$DATA/hook-pretooluse-benign.json" \
  --grep-err 'action=other' \
  -- bash "$GUARD" --hook-mode
run_case hook-pretooluse-commit 0 --stdin "$DATA/hook-pretooluse-commit.json" \
  --grep-err 'action=git-commit' \
  -- bash "$GUARD" --hook-mode

# [出口判据 4 延伸] hook 形态喂非法 JSON → exit 2（fail-closed，不得静默放行）
run_case hook-bad-stdin 2 --stdin "$DATA/hook-bad-stdin.txt" \
  --grep-err 'rt-guard' \
  -- bash "$GUARD" --hook-mode

# [出口判据 7] 命令变体解析：三种写法都识别为 commit 动作
run_case parse-plain-commit 0 --grep-out '^action=git-commit$' \
  -- bash "$GUARD" --parse-command 'git commit -m "x"'
run_case parse-dash-c-commit 0 --grep-out '^action=git-commit$' \
  -- bash "$GUARD" --parse-command 'git -C /tmp/x commit --allow-empty -m "y"'
run_case parse-cd-chain-commit 0 --grep-out '^action=git-commit$' \
  -- bash "$GUARD" --parse-command 'cd /tmp/x && git commit -m "z"'
# 加强：绝对路径 git（U0 §3.4 点名的同类绕过形式）
run_case parse-abs-path-commit 0 --grep-out '^action=git-commit$' \
  -- bash "$GUARD" --parse-command '/usr/bin/git commit -m "w"'
# 负例：git 非 commit 子命令 / git 不在命令头
run_case parse-negative-status 0 --grep-out '^action=other$' \
  -- bash "$GUARD" --parse-command 'git status'
run_case parse-negative-echo 0 --grep-out '^action=other$' \
  -- bash "$GUARD" --parse-command 'echo git commit'

# [骨架 I/O] deny 裁决 JSON 形状（spike §5.5：两事件字段名不同）
run_case emit-deny-pretooluse 0 --json-stdout \
  --grep-out '"permissionDecision": "deny"' \
  -- bash "$GUARD" --emit-deny PreToolUse "fixture reason"
run_case emit-deny-stop 0 --json-stdout \
  --grep-out '"decision": "deny"' \
  -- bash "$GUARD" --emit-deny Stop "fixture reason"

# ═══ U2a 用例：9 类元数据与文本判据（G101/G103/G104/G106/G108/G109）═══════════
# gate id 对齐任务包判据编号：G101=判据1/2、G103=判据3、G104=判据4/5、
# G106=判据6/7、G108=判据8、G109=判据9。

# [判据 1] type 非法取值 → G101 硬失败（exit 1）
run_case u2a-type-invalid-fix 1 --grep-out 'FAIL RT-201 +G101 +error' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-201
run_case u2a-type-invalid-combo 1 --grep-out 'FAIL RT-202 +G101 +error' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-202

# [判据 2] type 合法但带行内注释 → 剥 # 后比对，通过
run_case u2a-type-inline-comment 0 --grep-out 'PASS RT-203 +G101' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-203

# [判据 3] type 字段缺失 → G103 告警（exit 0），且 G101 不误报非法
run_case u2a-type-missing-warn 0 --grep-out 'WARN RT-204 +G103 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-204
run_case u2a-type-missing-no-g101 0 --grep-out 'PASS RT-204 +G101' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-204

# [判据 4] 五种同义词各一（决策记录在 §7、变更记录在 §8）→ G104 均不报缺失
run_case u2a-chlog-syn-bgjl-s8 0 --grep-out 'PASS RT-205 +G104' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-205        # 变更记录 @ §8
run_case u2a-chlog-syn-changelog 0 --grep-out 'PASS RT-206 +G104' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-206        # Changelog
run_case u2a-chlog-syn-wcjl 0 --grep-out 'PASS RT-207 +G104' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-207        # 完成记录
run_case u2a-chlog-syn-completion 0 --grep-out 'PASS RT-208 +G104' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-208        # Completion
run_case u2a-chlog-syn-jcjl-s7 0 --grep-out 'PASS RT-209 +G104' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-209        # 决策记录 @ §7

# [判据 5] 全文无同义词（§6 为「进度」）→ G104 报缺（告警级）
run_case u2a-chlog-missing 0 --grep-out 'WARN RT-210 +G104 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-210

# [判据 6] Spec-Full 缺 csf-review.md → G106 报缺 CSF
run_case u2a-specfull-no-csf 0 --grep-out 'WARN RT-211 +G106 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-211
# [判据 7] Spec-Lite 缺 csf-review.md → 不报（规则明写可选）
run_case u2a-speclite-no-csf-ok 0 --grep-out 'PASS RT-212 +G106' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-212

# [判据 8] profile 缺失 → G108 单独告警，且 G106 走 Spec-Lite 宽判据不报 CSF
run_case u2a-profile-missing-warn 0 --grep-out 'WARN RT-213 +G108 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-213
run_case u2a-profile-missing-lenient 0 --grep-out 'PASS RT-213 +G106' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-213

# [判据 9] RT/index.yaml 不存在 → 按「全部条目缺失」告警；expect 0 同时证明
# 未因文件缺失走 trap 的 exit 2 意外错误路径
run_case u2a-index-missing-warn 0 --grep-out 'WARN RT-203 +G109 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-203
run_case u2a-index-missing-note 0 --grep-out 'index\.yaml 不存在' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-203
# 正对照：index.yaml 存在且含条目 → PASS
run_case u2a-index-entry-ok 0 --grep-out 'PASS RT-301 +G109' \
  -- bash "$GUARD" --root "$ROOT_U2A_OK" --rt RT-301
# 回归（2026-08-16）：判据原用全文 grep RT-ID，导致「本 RT 只出现在他人条目的
# related 列表里」被误判成「自己有条目」——任何被关联过的 RT 都永远 PASS，
# 这道闸形同虚设。fixture 里 RT-303 仅被 RT-301 的 related 引用、自身无条目。
run_case u2a-index-related-only-warn 0 --grep-out 'WARN RT-303 +G109 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2A_OK" --rt RT-303
run_case u2a-index-related-only-note 0 --grep-out '无本 RT（RT-303）条目' \
  -- bash "$GUARD" --root "$ROOT_U2A_OK" --rt RT-303

# [出口判据 2] text / json 两种 format 都含判据结果与严重度
run_case u2a-json-severity-warn 0 --json-stdout --grep-out '"severity": "warn"' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-204 --format json
run_case u2a-json-verdict-fail 1 --json-stdout --grep-out '"verdict": "fail"' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-201 --format json

# [出口判据 3] 仅告警 → exit 0 且 stderr 显著提示
run_case u2a-warn-stderr-note 0 --grep-err '告警' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-204

# [零误报正例] 全合规 RT（Spec-Full / Spec-Lite 各一）→ error=0 warn=0
run_case u2a-all-green-specfull 0 --grep-out 'error=0 warn=0' \
  -- bash "$GUARD" --root "$ROOT_U2A_OK" --rt RT-301
run_case u2a-all-green-speclite 0 --grep-out 'error=0 warn=0' \
  -- bash "$GUARD" --root "$ROOT_U2A_OK" --rt RT-302

# [解析器兼容] 无 PyYAML 受限子集解析器能加载含 param 字段的新增条目
run_case u2a-list-gates-fallback 0 --grep-out 'G109.*index-entry' \
  -- env RT_GUARD_PYTHON="$DATA/python3-no-yaml.sh" \
     bash "$GUARD" --root "$ROOT_U2A" --list-gates

# ═══ U2b 用例：交叉引用检查器（G110 = 任务包判据 #10，告警级）═══════════════
# root-u2b 各 RT 无 meta.yaml / index.yaml，因此每次运行都伴随 G103/G104/G108/
# G109 的 WARN——属预期噪音，断言只锚定 G110 自己的行与统计。

# [出口判据 1-a] 悬空 :NN（文件存在但行号超总行数）→ G110 告警（warn 不阻断，exit 0）
run_case u2b-xref-dangling-line 0 --grep-out 'WARN RT-401 +G110 +warn +.*悬空 file:line 1' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-401
# [1-b] 有效 :NN（直连 + dot 前缀路径）与有效同文档 §N → 不报
run_case u2b-xref-valid-ok 0 --grep-out 'PASS RT-402 +G110' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-402
# [1-c] 短名可解析（文件在子目录，须经 basename 索引）→ 不报
run_case u2b-xref-shortname-ok 0 --grep-out 'PASS RT-403 +G110' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-403
# [1-d] 短名歧义、任一候选行号命中 → 不报（宽侧）
run_case u2b-xref-ambig-anyhit-ok 0 --grep-out 'PASS RT-404 +G110' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-404
# [1-e] 仓库外路径（第三方包文件 + 绝对路径）→ skip 不报
run_case u2b-xref-external-skip 0 --grep-out 'PASS RT-405 +G110' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-405
# [1-f] 悬空 §N → 报；同文档同时存在的跨文档 § 引用被 skip、不产生第二条悬空
run_case u2b-xref-dangling-section 0 --grep-out 'WARN RT-406 +G110 +warn +.*悬空 § 1' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-406
run_case u2b-xref-xdoc-skip 0 --grep-out '跨文档 § 跳过 1' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-406
# [加强] 短名歧义、全部候选不命中 → 报（「全部不存在才报」的报出侧）
run_case u2b-xref-ambig-allmiss-warn 0 --grep-out 'WARN RT-407 +G110 +warn' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-407

# [--scan-refs 明细] dot 前缀路径原样保留（抽取正则未截断前导 .，U1 实测坑）
run_case u2b-scan-refs-dot-prefix 0 \
  --grep-out 'OK +RT/RT-402/rt-lite\.md:[0-9]+ +\.aodw-next/guide\.md:2' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-402 --scan-refs
# [--scan-refs 明细] 输出统计块；默认 scope 只扫 RT 目录内（文档 2 篇）
run_case u2b-scan-refs-stats 0 --grep-out '== 统计 ==' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-402 --scan-refs
run_case u2b-scan-default-scope 0 --grep-out '文档 2 篇' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-402 --scan-refs
# [--scan-rules 开关] 打开后 .aodw-next/ 的 .md 并入扫描（guide.md 成为被扫文档）
run_case u2b-scan-rules-scope 0 --grep-out '^OK +\.aodw-next/guide\.md:' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-402 --scan-refs --scan-rules
# [--scan-refs 报出侧] 有悬空仍 exit 0（告警级报告模式），stderr 有显著提示
run_case u2b-scan-refs-warn-exit0 0 --grep-err '交叉引用存在悬空' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-401 --scan-refs

# [解析器兼容] 无 PyYAML 受限子集解析器加载 G110（param 单行标量装得下）
run_case u2b-list-gates-fallback 0 --grep-out 'G110.*xref-refs-resolve' \
  -- env RT_GUARD_PYTHON="$DATA/python3-no-yaml.sh" \
     bash "$GUARD" --root "$ROOT_U2B" --list-gates
# [json] G110 结果进 json 输出
run_case u2b-json-g110-warn 0 --json-stdout --grep-out '"gate": "G110"' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-401 --format json
# [用法防呆] --scan-rules 不与 --scan-refs 连用 → exit 2
run_case u2b-scan-rules-alone 2 --grep-err 'scan-rules' \
  -- bash "$GUARD" --root "$ROOT_U2B" --rt RT-402 --scan-rules

# ═══ U6 用例：载体接入判定策略（hook deny 路径 / --pre-commit / G001 自检）═══
# 临时 git 仓库置于 mktemp（仓库外，用毕即删）；分支即车道，全程无真实提交。
# 判定策略（unit-U6-input.md §2.1/2.2）：分支 feature/RT-NNN-* 提取 RT-ID，
# error 级判据未过 → hook 形态 deny / pre-commit 形态 exit 1；提不出则放行。
U6_TMP="$(mktemp -d)"
U6_REPO="$U6_TMP/repo-rt-lane"
mkdir -p "$U6_REPO/RT"
git init -q -b feature/RT-201-fixture "$U6_REPO"
cp -R "$ROOT_U2A/RT/RT-201" "$U6_REPO/RT/RT-201"   # 缺陷 RT：type: Fix → G101 error
python3 - "$U6_REPO" > "$U6_TMP/hook-commit-rt-lane.json" <<'PY'
import json, sys
print(json.dumps({
    "session_id": "fixture-session-u6", "transcript_path": "/tmp/fixture.jsonl",
    "cwd": sys.argv[1], "permission_mode": "auto",
    "hook_event_name": "PreToolUse", "tool_name": "Bash",
    "tool_input": {"command": "git commit -m \"fixture violation\"",
                   "description": "fixture: commit on RT lane"},
    "tool_use_id": "toolu_fixture_u6_1"}))
PY

# [U6 hook deny] RT 车道 + 缺陷 RT → 结构化 deny 裁决（exit 0 正路），理由含 G101
run_case u6-hook-deny-rt-lane 0 --stdin "$U6_TMP/hook-commit-rt-lane.json" \
  --json-stdout --grep-out '"permissionDecision": "deny"' \
  -- bash "$GUARD" --hook-mode
run_case u6-hook-deny-reason-g101 0 --stdin "$U6_TMP/hook-commit-rt-lane.json" \
  --grep-out 'G101' \
  -- bash "$GUARD" --hook-mode

# [U6 hook allow] 非 RT 车道（main）→ 放行（门禁只管 RT 车道）
git -C "$U6_REPO" symbolic-ref HEAD refs/heads/main
run_case u6-hook-allow-non-rt-lane 0 --stdin "$U6_TMP/hook-commit-rt-lane.json" \
  --grep-err '非 RT 车道' \
  -- bash "$GUARD" --hook-mode
git -C "$U6_REPO" symbolic-ref HEAD refs/heads/feature/RT-201-fixture

# [U6 --pre-commit] RT 车道 + 缺陷 RT → exit 1（阻断提交）
run_case u6-precommit-fail-rt-lane 1 --grep-out 'FAIL RT-201 +G101' \
  -- bash -c 'cd "$1" && bash "$2" --pre-commit' _ "$U6_REPO" "$GUARD"
# [U6 --pre-commit] 非 RT 车道 → 快速通过（exit 0）
git -C "$U6_REPO" symbolic-ref HEAD refs/heads/main
run_case u6-precommit-pass-non-rt 0 --grep-err '快速通过' \
  -- bash -c 'cd "$1" && bash "$2" --pre-commit' _ "$U6_REPO" "$GUARD"
git -C "$U6_REPO" symbolic-ref HEAD refs/heads/feature/RT-201-fixture

# [U6 G001 自检] 临时仓库顶层 + hook 未安装 → WARN（确定性：临时仓库永无 hook；
# exit 1 来自同一 RT 的 G101 error，不来自 G001）
run_case u6-gate-selfcheck-warn 1 --grep-out 'WARN RT-201 +G001 +warn' \
  -- bash "$GUARD" --root "$U6_REPO" --rt RT-201
# [U6 G001 自检] fixture 子目录（非 git 顶层）→ 不适用恒 PASS（断言确定性）
run_case u6-gate-selfcheck-na 0 --grep-out 'PASS RT-301 +G001' \
  -- bash "$GUARD" --root "$ROOT_U2A_OK" --rt RT-301

rm -rf "$U6_TMP"


# ── G111 收口复盘 Gate（批次二）────────────────────────────────────────────
run_case g111-done-no-retro 1 --grep-out 'FAIL RT-214 +G111' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-214
run_case g111-done-with-retro 0 --grep-out 'PASS RT-215 +G111' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-215
run_case g111-not-done-skip 0 --grep-out 'PASS RT-204 +G111' \
  -- bash "$GUARD" --root "$ROOT_U2A" --rt RT-204

# ── 任务包自检用例（G112/G113）────────────────────────────────────────────────
# 事故原型：RT-125 unit-U5——§1 目标文案含 §3 零命中判据要 grep 的子串「plan 批准前
# 必须执行」，照抄执行恒不通过（当时靠执行 Agent 识破，unit-U5-receipt.md:89-93）。
run_case g112-u5-selfconflict 1 \
  --grep-out 'FAIL RT-501 +G112 +error 自冲突 1 条.*扫描 1 份 / 执行 2 条 / skip 0 条' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-u1" --rt RT-501
run_case g112-no-conflict-pass 0 \
  --grep-out 'PASS RT-502 +G112.*扫描 1 份 / 执行 1 条 / skip 1 条' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-u1" --rt RT-502
# status=done 作用域豁免（2026-08-22）：RT-503 的任务包与 RT-501 逐字相同，唯一差别
# 是 meta.yaml 有 status: done。断言两件事同时成立——判据仍实跑并如实报出自冲突条数
# （不是跳过扫描），且结论不判失败。反向锚点即上面的 g112-u5-selfconflict：RT-501
# 无 status 字段，必须继续 FAIL。
run_case g112-done-scope-exempt 0 \
  --grep-out 'PASS RT-503 +G112 +error 自冲突 1 条.*status=done，任务包已归档' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-u1" --rt RT-503
# 事故原型：RT-125 unit-U6——判据③整条消失且无溯源声明，最终由执行 Agent 回补
# （unit-U6-receipt.md:59）。
run_case g113-missing-provenance 0 \
  --grep-out 'WARN RT-502 +G113 +warn +溯源声明违规 1 份.*缺溯源声明行.*扫描 1 份 / 执行 1 条 / skip 0 条' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-u1" --rt RT-502
run_case g113-declared-pass 1 --grep-out 'PASS RT-501 +G113' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-u1" --rt RT-501

# ── RT-135 U2 用例：G109 收紧反例 + G113 合并计数口径（fixture root 落
# DI-004 / rt-lite AC-3：收紧后「仅出现在他人 related 列表」必须被拦（锚定实现
# 已随 fc6fcc6 入库，本用例钉住该行为不回退）。
run_case g109-related-only-fail 0 \
  --grep-out 'WARN RT-622 +G109 +warn +.*无本 RT（RT-622）条目' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-g109" --rt RT-622
# 裁定 001 措辞 A：合并 b 记净减少条数——2 并 1 记 1，10 + 0 − 1 − 0 = 9 自洽。
run_case g113-merge-accounting 0 \
  --grep-out 'PASS RT-631 +G113 +.*判据 10 条 → 本包 9 条.*扫描 1 份 / 执行 1 条 / skip 0 条' \
  -- bash "$GUARD" --root "$FIXTURES_DIR/root-g113" --rt RT-631

# ── 汇总 ─────────────────────────────────────────────────────────────────────
echo "== 汇总: PASS=$N_PASS FAIL=$N_FAIL =="
if [[ "$N_FAIL" -gt 0 ]]; then
  printf '失败用例: %s\n' "${FAILED_NAMES[*]}"
  exit 1
fi
exit 0
