#!/usr/bin/env python3
"""交接包格式校验器。

用法：
    validate.py <交接包路径>          校验，全过 exit 0
    validate.py <路径> --quiet        只输出失败项

退出码：0 通过 / 1 有必须修的问题 / 2 用法错误。

设计原则（来自 2026-08 的实测教训）：
- 只判**能机械判定**的事。判不了的（下一步能不能动手、会不会误导）明确交回人工。
- 判到真问题，不判近似物。例：不只查「有没有 rt 字段」，还查它的值是否合法；
  不只查「有没有引用 commit」，还查那些 commit 是否真的存在。
- 每条判据都要有判别力——写完用「本该失败的样本」验一遍。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REQUIRED_FIELDS = [
    "title",
    "status",
    "kind",
    "created",
    "last_verified",
    "owners",
    "rt",
    "related_items",
    "scope",
    "related",
]
STATUS_VALUES = {"active", "superseded", "archived"}
# 交接包分两类，正文章节要求不同（见 check_sections）：
#   session    工作过程交接——「我做了什么、下一个人接着做什么」，八节缺一不可
#   operations 运维手册交接——「这个系统怎么运维」，结构由内容决定，不套用八节
KIND_VALUES = {"session", "operations"}
RELATION_TYPES = {"发现", "认领", "结清", "受阻于", "同族"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RT_RE = re.compile(r"^(RT-\d+|none|\[.*\])$")
# 正文必需章节：关键词任一命中即算该节存在
REQUIRED_SECTIONS = [
    ("一句话", ["一句话", "TL;DR", "摘要"]),
    ("做了什么", ["做了什么", "已完成", "工作内容"]),
    ("怎么做的", ["怎么做", "怎么查", "方法"]),
    ("要解决什么", ["要解决", "希望解决", "为什么做", "问题背景"]),
    ("发现了什么", ["发现了什么", "发现的问题", "实证结论"]),
    ("下一步", ["下一步", "后续", "计划做什么"]),
    ("现状变化", ["现状变化", "现状核实", "落笔前", "接手时的现状"]),
    ("接手清单", ["接手清单", "接手步骤", "照着做"]),
]

problems: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def split_frontmatter(text: str) -> tuple[str, str]:
    """返回 (frontmatter 原文, 正文)。无 frontmatter 时前者为空串。"""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    return text[3:end], text[end + 4 :]


def check_frontmatter(fm: str, path: Path) -> None:
    if not fm.strip():
        fail("缺 frontmatter——必须以 `---` 开头（见 references/format-contract.md §一）")
        return

    # 用 yaml 解析；不可用时降级为逐行提取，保证校验器本身不因环境挂掉
    data: dict = {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(fm) or {}
        if not isinstance(data, dict):
            fail("frontmatter 不是 YAML 映射")
            return
    except ImportError:
        for line in fm.splitlines():
            m = re.match(r"^([a-z_]+):\s*(.*)$", line)
            if m:
                data[m.group(1)] = m.group(2).strip()
        warn("未安装 pyyaml，已降级为逐行提取——列表类字段的校验会放宽")

    for f in REQUIRED_FIELDS:
        if f not in data or data[f] in (None, "", []):
            if f == "related_items" and f in data:
                continue  # 空列表合法，但正文要说明；见下方检查
            fail(f"frontmatter 缺必需字段 `{f}`")

    if (kd := data.get("kind")) and str(kd) not in KIND_VALUES:
        fail(f"`kind` 取值非法：{kd}（合法：{'/'.join(sorted(KIND_VALUES))}）")

    if (st := data.get("status")) and str(st) not in STATUS_VALUES:
        fail(f"`status` 取值非法：{st}（合法：{'/'.join(sorted(STATUS_VALUES))}）")

    for f in ("created", "last_verified"):
        v = data.get(f)
        if v and not DATE_RE.match(str(v)):
            fail(f"`{f}` 必须是 YYYY-MM-DD 格式，当前：{v}")

    # rt：字段必须存在，但 none 是完全正当的取值——不挂 RT 的会话是常态，
    # 硬凑一个「看起来相关」的编号比留空更浪费接手人的时间。
    rt = data.get("rt")
    if rt is not None:
        rt_s = str(rt) if not isinstance(rt, list) else "[" + ",".join(str(x) for x in rt) + "]"
        if not RT_RE.match(rt_s.replace(" ", "")):
            fail(f"`rt` 取值非法：{rt}（应为 RT-XXX、列表、或 none）")
        if rt_s == "none":
            scope = str(data.get("scope", ""))
            if len(scope.strip()) < 20:
                fail(
                    "`rt: none` 完全正当，但请在 `scope` 里一句话说清这是什么性质的工作（当前 scope 过短）"
                )

    # related_items 的关系类型必须在封闭枚举内
    items = data.get("related_items")
    if isinstance(items, list) and items:
        for it in items:
            if isinstance(it, dict):
                for k, v in it.items():
                    if str(v).strip() not in RELATION_TYPES:
                        fail(
                            f"`related_items` 中 {k} 的关系类型非法：{v}（合法：{'/'.join(sorted(RELATION_TYPES))}）"
                        )
            elif isinstance(it, str) and ":" in it:
                rel = it.split(":", 1)[1].strip()
                if rel not in RELATION_TYPES:
                    fail(f"`related_items` 中的关系类型非法：{rel}")
    elif isinstance(items, list) and not items:
        warn("`related_items` 为空——若本次确实无台账/RT 关联，这是正常的，正文一句话说明即可")


def check_sections(body: str, kind: str) -> None:
    """章节要求按 kind 分流。

    - session（工作过程交接）：讲「我做了什么、下一个人接着做什么」，八节缺一不可。
    - operations（操作手册交接）：讲「这个系统怎么运维」，结构由内容决定
      （怎么部署 / 怎么健康检查 / 出事怎么办），**不套用八节**——强行套会把
      一份好用的手册改坏。只要求它有「接手清单」性质的可操作内容。
    """
    heads = "\n".join(re.findall(r"^#{2,3}\s+.*$", body, re.M))
    if kind == "operations":
        if len(heads.strip().splitlines()) < 3:
            fail("operations 类交接包正文至少要有 3 个二级/三级小节，便于按场景查阅")
        return
    for label, keys in REQUIRED_SECTIONS:
        if not any(k in heads for k in keys):
            fail(f"正文缺必需章节：{label}（标题里应含 {' / '.join(keys[:2])} 之一）")


def check_commits(text: str, repo: Path | None) -> None:
    """引用的 commit hash 必须真实存在——防「引用一个不存在的提交」。

    ⚠️ repo 为 None（文档不在 git 仓库内）时**整体跳过并 WARN**，不逐条报 FAIL。
    自检实测教训：把文档复制到 /tmp 校验时，原实现把每个 commit 都报成「不存在」
    ——一次环境不满足产生 11 条假红，掩盖了真正被测的那一条。
    判据在前提不成立时必须明确 skip，不能产生假红。
    """
    hashes = set(re.findall(r"`([0-9a-f]{7,40})`", text))
    if not hashes:
        warn("正文未引用任何 commit hash——交接包通常应给出可回溯的提交")
        return
    if repo is None:
        warn(f"文档不在 git 仓库内，跳过 {len(hashes)} 个 commit 的存在性校验")
        return
    for h in sorted(hashes):
        r = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-t", h],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 or r.stdout.strip() != "commit":
            fail(f"引用的 commit `{h}` 在仓库中不存在（或不是 commit）")


def check_links(text: str, doc: Path, repo: Path) -> None:
    """文档内的相对链接必须指向真实存在的文件。"""
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        t = target.split("#", 1)[0]
        if not t:
            continue
        cand = (doc.parent / t), (repo / t)
        if not any(p.exists() for p in cand):
            fail(f"链接指向的文件不存在：[{label}]({target})")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    quiet = "--quiet" in argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        print("用法：validate.py <交接包路径> [--quiet]", file=sys.stderr)
        return 2

    doc = Path(args[0]).resolve()
    if not doc.is_file():
        print(f"文件不存在：{doc}", file=sys.stderr)
        return 2

    r = subprocess.run(
        ["git", "-C", str(doc.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    # 定位不到仓库根就传 None——让 commit 校验明确跳过，而不是逐条误报
    repo = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None

    text = doc.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)

    kind = "session"
    m = re.search(r"^kind:\s*(\w+)", fm, re.M)
    if m:
        kind = m.group(1)

    check_frontmatter(fm, doc)
    check_sections(body, kind)
    check_commits(body, repo)
    check_links(body, doc, repo or doc.parent)

    if not quiet:
        print(f"交接包校验 — {doc.name}\n")
    for w in warnings:
        print(f"  \033[33mWARN\033[0m  {w}")
    for p in problems:
        print(f"  \033[31mFAIL\033[0m  {p}")

    if problems:
        print(f"\n  合计 FAIL={len(problems)} WARN={len(warnings)}")
        print("  ⚠️ 校验不过就不算写完。规则见 references/format-contract.md")
        return 1

    if not quiet:
        print(f"  \033[32mPASS\033[0m  格式校验全过（WARN={len(warnings)}）\n")
        print("  ⚠️ 脚本查不了这两件事，请人工过一遍：")
        print("     1. §下一步 的每一条，接手的人照着能不能直接动手？")
        print("     2. §现状变化 读完会不会被误导？")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
