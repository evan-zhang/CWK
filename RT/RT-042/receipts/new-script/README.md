# RT-042 新脚本回执（from=none）

本目录放 RT-042 五个**全新**脚本的登记回执。每份回执记 `target_path`、
`from_sha256: "none"`（无前序链尖）、`to_sha256`（落盘内容指纹）、认领它的
所有权规则、以及模块索引条目。

## 为什么不放 `receipts/script-evolution-v2/`

rt-lite 原文写的是 `receipts/script-evolution-v2/`。按仓库现行门禁，那条路
**走不通**，不是风格取舍：

- `.aodw-next/06-project/governance-audit.py` 的 GA-V2-RECEIPT 会对每份
  `RT/*/receipts/script-evolution-v2/*.json` 断言 target 必须落在
  `continuation_slots` 或 `legacy_frozen_files` 里，否则硬失败
  （「既不是续演槽位、也不是 legacy 成员」）。
- 想给这五个新文件开续演槽位同样不行：GA-V2-SLOT 要求槽位目标先存在于
  v1 `policy_v1.json` 的 `evolvable_paths`，且 v1 槽位**已用尽**才允许开 v2。
  全新文件两条都不满足。v2 叠加层按设计是「给用尽者的续命通道」（DI-001/DI-002），
  不是新文件的入口。

先例：RT-040 新增 `scripts/reply_refresh.py` 时做了同样判定，
见 code-ownership-manifest.json 的 `R-runtime-rt040-reply-refresh` rationale。

## 这五个脚本真正的受管点

1. **所有权**：`R-runtime-rt042-kb-platform`（kind=exact_set，五条路径逐条列出）。
   `scripts/` 是 exact-only 区，没有宽前缀能吸收它们——漏登记会直接变孤儿并让
   `make governance-audit` 变红。这是比回执更硬的约束。
2. **模块索引**：`.aodw-next/06-project/modules-index.yaml` 的 `kb-platform-storage`。
3. **测试**：`tests/test_kb_{storage,create,ledger,migrate}.py`，由 `R-test-suite` 认领。

## 复验

```bash
# 回执指纹与磁盘一致
python3 - <<'PY'
import hashlib, json, pathlib
for p in sorted(pathlib.Path("RT/RT-042/receipts/new-script").glob("*.json")):
    r = json.loads(p.read_text(encoding="utf-8"))
    disk = hashlib.sha256(pathlib.Path(r["target_path"]).read_bytes()).hexdigest()
    print("OK " if disk == r["to_sha256"] else "DRIFT", r["target_path"])
PY

make governance-audit
```

脚本内容改了就要同步更新这里的 `to_sha256`，否则上面的复验会显示 DRIFT。
