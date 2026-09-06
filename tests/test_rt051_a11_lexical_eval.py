#!/usr/bin/env python3
"""RT-051 P4 · A11: 正文词法与同一 reader 的 48 题评测（脱敏合成语料）.

Spec (rt-lite.md 词法行): 48 题 / 2 合成库, 每类 6 题——正文-only、中文短词、
编号、无答案、版本、隔离撤权、索引故障、metadata 兼容; macro doc Recall@10
≥ 0.90, 正文-only / 短词各 ≥ 5/6, gold 关键 span Recall@10 ≥ 0.85; 隔离泄露 0、
verified 错版 / 错 span 为 0; 同集 metadata / body / fusion 消融。

Gold 说明（不自签）: 每题附支持理由（rationale）; reviewer / reviewed_at
留空, 正式签收需独立复核人（Evan 或被指派者）逐题确认后填写——本套件
验证的是引擎行为达到阈值, 不是 gold 的人工签收本身。
运行 ``CWK_RT051_A11_DUMP=1 python3 -m tests.test_rt051_a11_lexical_eval``
会把逐题结果写到 RT/RT-051/a11-results-latest.json 供复核。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "tests"))

import kb_gateway as gateway  # noqa: E402
import kb_ingest as ingest  # noqa: E402
import kb_lexical_builder as builder  # noqa: E402
import kb_token  # noqa: E402
from kb_ledger import dumps, read_json  # noqa: E402
from kb_storage import LocalFSBackend  # noqa: E402
from test_kb_gateway import FIXED_NOW, TOKEN, issue_binding_token  # noqa: E402
from test_kb_ingest import make_kb  # noqa: E402
from test_rt051_acceptance import ROOT, run_refresh  # noqa: E402

FILLER = "背景说明填充行，用于拉开正文字数。\n"
V2_SUFFIX = "（二〇二六修订版：口径已更新。）\n"


def _body(marker_sentence: str, before: int, after: int) -> bytes:
    return "".join(
        ["# 合成评估文档\n"]
        + [FILLER] * before
        + [marker_sentence + "\n"]
        + [FILLER] * after
    ).encode("utf-8")


def _skill_env(base: Path) -> dict:
    skill = base / "cms-docdb"
    (skill / "scripts" / "browse").mkdir(parents=True, exist_ok=True)
    (skill / "scripts" / "query").mkdir(parents=True, exist_ok=True)
    return {
        ingest.ENV_DOCDB_SKILL_DIR: str(skill),
        "XG_BIZ_API_KEY": "fake-key-not-a-real-secret",
    }


def _ascii_body() -> bytes:
    """元数据兼容件的正文：纯 ASCII。

    中文 1/2/3-gram 分词下，任何含中文的正文都可能被元数据词的单字 gram
    命中（那是 BM25 的诚实行为）——要让「body 路=零、metadata 路=命中」
    这条消融可证伪，元数据件正文必须不含任何中文字符。
    """
    return "".join(
        ["# synthetic evaluation document\n"]
        + ["filler ascii baseline 0001\n"] * 18
        + ["plain filler body line two.\n"]
        + ["filler ascii baseline 0002\n"] * 18
    ).encode("utf-8")


# ── 语料：两库各 14 件，marker 全部受控 ─────────────────────────────────────
# 注意：两库的 classify 路径都含「玄关/合同」段，marker 一律避开 玄/关/合/同。

LIB_A_DOCS = {
    "601": ("601-节点说明.md", _body("关键配置项：天枢采用双通道冗余。", 20, 20)),
    "602": ("602-保护机制.md", _body("异常时触发熔断并自动回切。", 25, 25)),
    "603": ("603-设计总览.md", _body("整体蓝图已在评审会上定稿。", 120, 40)),
    "604": ("604-发布策略.md", _body("本月起按灰度批次逐段放量。", 150, 60)),
    "605": ("605-运维手册.md", _body("每日例行巡检覆盖全部链路。", 20, 20)),
    "606": ("606-容量评估.md", _body("上周完成一轮全链路压测。", 25, 25)),
    "607": ("607-编号对照.md", _body("编号对照：AB-017 对应闸门模块。", 30, 30)),
    "608": ("608-工单记录.md", _body("工单 XY-2026-004 已关闭并复核。", 30, 30)),
    "609": ("609-资料汇编.md", _body("本篇仅作语料体积填充。", 18, 18)),
    "610": ("610-基础概念.md", _body("所有变更以锚点数据为准。", 20, 20)),
    "611": ("611-背景资料.md", _body("本篇仅作语料体积填充。", 18, 18)),
    "612": ("612-里程碑.md", _ascii_body()),
    "613": ("613-审计底稿.md", _ascii_body()),
    "614": ("614-验收清单.md", _ascii_body()),
}
LIB_B_DOCS = {
    "701": ("701-基准参考.md", _body("度量口径以最新基线为准。", 20, 20)),
    "702": ("702-流程规范.md", _body("各环节产出须与规范对齐。", 120, 40)),
    "703": ("703-隐私保护.md", _body("外发前一律完成脱敏处理。", 25, 25)),
    "704": ("704-会议摘要.md", _body("本篇仅作语料体积填充。", 18, 18)),
    "705": ("705-周报存档.md", _body("本篇仅作语料体积填充。", 18, 18)),
    "706": ("706-培训材料.md", _body("本篇仅作语料体积填充。", 18, 18)),
    "707": ("707-资产台账.md", _body("资产编号 BB-204 属于第二批交付。", 30, 30)),
    "708": ("708-工单索引.md", _body("工单 QW-77 状态为已归档。", 30, 30)),
    "709": ("709-回执登记.md", _body("回执 RT-33 已完成核销。", 30, 30)),
    "710": ("710-监测方案.md", _body("按五分钟间隔持续采样。", 20, 20)),
    "711": ("711-环境说明.md", _body("测试环境与生产严格隔离。", 25, 25)),
    "712": ("712-决策纪要.md", _ascii_body()),
    "713": ("713-移交档案.md", _ascii_body()),
    "714": ("714-答疑记录.md", _ascii_body()),
}
UPGRADED_AT = "2026-08-20 10:00:00"


@dataclass
class Gold:
    qid: str
    category: str
    lib: str
    query: str
    expect: str                 # lineage，或 "__zero__"
    marker: str = ""            # gold 关键 span 文本
    rationale: str = ""
    meta: dict = field(default_factory=dict)


def _build_gold() -> list:
    g: list = []
    for qid, lib, q, lin, why in [
        ("Q01", "liba", "天枢", "docdb:601", "『天枢』只在 601 正文；标题/路径均无"),
        ("Q02", "liba", "蓝图", "docdb:603", "marker 在约 2400 字之后（正文中后部），标题无"),
        ("Q03", "liba", "灰度", "docdb:604", "约 3600 字长文跨块召回，标题无"),
        ("Q04", "libb", "基线", "docdb:701", "『基线』只在 701 正文；A 库全库无"),
        ("Q05", "libb", "对齐", "docdb:702", "marker 在长文后半部，标题无"),
        ("Q06", "libb", "脱敏", "docdb:703", "『脱敏』只在 703 正文"),
    ]:
        g.append(Gold(qid, "body_only", lib, q, lin, q, why))
    for qid, lib, q, lin, why in [
        ("Q07", "liba", "熔断", "docdb:602", "2 字词，只出现在 602 正文"),
        ("Q08", "liba", "巡检", "docdb:605", "2 字词，只出现在 605 正文"),
        ("Q09", "liba", "锚点", "docdb:610", "2 字词，只出现在 610 正文"),
        ("Q10", "libb", "采样", "docdb:710", "2 字词，只出现在 710 正文"),
        ("Q11", "libb", "隔离", "docdb:711", "2 字词，只出现在 711 正文"),
        ("Q12", "liba", "压测", "docdb:606", "2 字词，只出现在 606 正文"),
    ]:
        g.append(Gold(qid, "short_term", lib, q, lin, q, why))
    for qid, lib, q, lin, why in [
        ("Q13", "liba", "AB-017", "docdb:607", "编号只在正文；C07：AB-017 是单一 ASCII 词项"),
        ("Q14", "liba", "XY-2026-004", "docdb:608", "复合编号整词匹配"),
        ("Q15", "libb", "BB-204", "docdb:707", "B 库正文编号"),
        ("Q16", "libb", "QW-77", "docdb:708", "B 库正文编号"),
        ("Q17", "libb", "RT-33", "docdb:709", "B 库正文编号"),
        ("Q18", "liba", "017", "__zero__", "编号语义：017 不得命中 AB-017（矩阵失败样例）"),
    ]:
        g.append(Gold(qid, "code", lib, q, lin, q if q != "017" else "", why))
    for qid, lib, q in [
        ("Q19", "liba", "麒麟凤凰"), ("Q20", "liba", "甲乙丙丁"),
        ("Q21", "liba", "恐龙化石"), ("Q22", "libb", "琥珀陨石"),
        ("Q23", "libb", "戊己庚辛"), ("Q24", "libb", "壬癸乾坤"),
    ]:
        g.append(Gold(qid, "no_answer", lib, q, "__zero__", "",
                      "查询词逐字不在该库全部正文/标题/路径中（Phase 0 自证）"))
    for qid, lib, fid, marker in [
        ("Q25", "liba", "601", "天枢"), ("Q26", "liba", "607", "AB-017"),
        ("Q27", "liba", "610", "锚点"), ("Q28", "libb", "701", "基线"),
        ("Q29", "libb", "707", "BB-204"), ("Q30", "libb", "710", "采样"),
    ]:
        g.append(Gold(qid, "version", lib, marker, f"docdb:{fid}", marker,
                      "源覆写升 v2：旧 ref 409、新 read 含修订标记、钩子重建后融合仍召回"))
    for qid, why in [
        ("Q31", "liba-token 访问 ?kb=libb → 403，不泄露 B 库 title/计数"),
        ("Q32", "?kb=liba 查 B 库 marker『基线』→ 融合与 metadata 双零命中"),
        ("Q33", "admin 多库挂载查 ?kb=libb『基线』→ 200 命中 701（隔离不是藏死）"),
        ("Q34", "撤权后同 token 再查 liba → 401（即刻生效）"),
        ("Q35", "ref 过期 → 410；重新 resolve 恢复 200"),
        ("Q36", "篡改句柄尾部两字符 → 400"),
    ]:
        g.append(Gold(qid, "isolation", "liba", "", "", "", why))
    for qid, why in [
        ("Q37", "词法代缺失 → 融合 503；同 ref read 仍 200"),
        ("Q38", "词法代损坏 → 融合 503；read 仍 200"),
        ("Q39", "词法代 generation 与资格域不符 → 503（不冒充）"),
        ("Q40", "raw-index 不可读 → list 503 且无正文内容"),
        ("Q41", "词法坏期间 metadata 模式不受影响 → 200"),
        ("Q42", "refresh 钩子自动重建 → 融合恢复 200"),
    ]:
        g.append(Gold(qid, "index_fault", "libb", "", "", "", why))
    for qid, lib, q, lin, why in [
        ("Q43", "liba", "里程碑", "docdb:612", "只在标题；融合响应 body_rank 须为 None（路线分离）"),
        ("Q44", "liba", "审计底稿", "docdb:613", "只在标题"),
        ("Q45", "liba", "验收清单", "docdb:614", "只在标题"),
        ("Q46", "libb", "决策纪要", "docdb:712", "只在标题"),
        ("Q47", "libb", "移交档案", "docdb:713", "只在标题"),
        ("Q48", "libb", "答疑记录", "docdb:714", "只在标题"),
    ]:
        g.append(Gold(qid, "metadata_compat", lib, q, lin, "", why))
    return g


GOLD = _build_gold()
GOLD_HEADER = {
    "spec": "RT/RT-051/rt-lite.md 词法行（48 题 / 2 合成库 / 8 类 × 6）",
    "reviewer": None,   # 不自签：待独立复核人签署
    "reviewed_at": None,
}


class A11EvalTests(unittest.TestCase):
    LIBS = {"liba": LIB_A_DOCS, "libb": LIB_B_DOCS}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.env = _skill_env(self.base)
        self.apps, self.backends, self.codes, self.roots = {}, {}, {}, {}
        for lib, docs in self.LIBS.items():
            root = self.base / lib
            code = make_kb(root, sources=("docdb",))
            backend = LocalFSBackend(root)
            rep = run_refresh(backend, code, root, {ROOT: self._rows(docs)},
                              self._blobs(docs), self.env)
            self.assertTrue(rep["ok"], rep)
            self._publish(backend, code)
            self.apps[lib] = gateway.GatewayApp(
                backend, TOKEN, backend_kind="local",
                clock=lambda: FIXED_NOW, kb_id=lib)
            self.backends[lib], self.codes[lib], self.roots[lib] = backend, code, root
        self.H = {gateway.TOKEN_HEADER: TOKEN}
        self.rows_report: list = []
        self.faults: list = []

    # -- 固定装置 --------------------------------------------------------

    @staticmethod
    def _rows(docs, upgraded=()):
        return [
            {"fileId": fid, "name": name, "type": "2",
             "updateTime": UPGRADED_AT if fid in upgraded else "2026-08-14 09:30:00"}
            for fid, (name, _b) in sorted(docs.items())
        ]

    @staticmethod
    def _blobs(docs, suffixes=None):
        suffixes = suffixes or {}
        return {
            fid: docs[fid][1] + suffixes.get(fid, b"") for fid in docs
        }

    def _publish(self, backend, code):
        report = builder.build_lexical_index(backend, kb_code=code)
        builder.publish(backend, kb_code=code, report=report)

    def get(self, lib, path, app=None):
        return (app or self.apps[lib]).dispatch("GET", path, self.H)

    def fusion(self, lib, q, app=None):
        r = self.get(lib, f"/v2/kb/search?kb={lib}&q={quote(q)}"
                        "&retrieval_mode=lexical_fusion_v1&page_size=10", app=app)
        return r

    def meta_search(self, lib, q, app=None):
        return self.get(lib, f"/v2/kb/search?kb={lib}&q={quote(q)}&page_size=10",
                        app=app)

    def resolve(self, lib, lineage, app=None):
        r = self.get(lib, f"/v2/kb/resolve?kb={lib}&lineage={lineage}", app=app)
        self.assertEqual(r.status, 200, r.payload)
        return r.payload["document_ref"]

    def read(self, lib, ref, extra=""):
        return self.get(lib, f"/v2/kb/read?kb={lib}&document_ref={ref}{extra}")

    def docs_index(self, lib):
        """{lineage: (title, path, body_text)} 的当前实况。"""
        idx = read_json(self.backends[lib], "_system/raw-index.json")
        out = {}
        for lin, e in idx["entries"].items():
            raw = self.backends[lib].read(e["path"])
            out[lin] = (e.get("title") or "", e.get("path") or "",
                        raw.decode("utf-8", "replace"))
        return out

    def record(self, gold: Gold, **facts) -> None:
        gold.meta.update(facts)
        self.rows_report.append({
            "qid": gold.qid, "category": gold.category, "lib": gold.lib,
            "query": gold.query, "expect": gold.expect,
            "rationale": gold.rationale, **facts,
        })

    def fail_note(self, qid, msg):
        self.faults.append(f"{qid}: {msg}")

    # -- Phase 0: gold 语料自证 ------------------------------------------

    def phase0_self_certify(self):
        for g in GOLD:
            if g.category in ("body_only", "short_term") or (
                g.category == "code" and g.expect != "__zero__"
            ):
                docs = self.docs_index(g.lib)
                body_hits = sorted(l for l, (_t, _p, b) in docs.items()
                                   if g.marker in b)
                self.assertEqual(body_hits, [g.expect],
                                 f"{g.qid} marker 正文泄漏: {body_hits}")
                leaked = [l for l, (t, p, _b) in docs.items()
                          if g.marker in (t + p)]
                self.assertEqual(leaked, [], f"{g.qid} marker 出现在标题/路径")
            elif g.category == "no_answer":
                docs = self.docs_index(g.lib)
                corpus = "".join(t + p + b for t, p, b in docs.values())
                bad = [ch for ch in g.query if ch in corpus]
                self.assertEqual(bad, [], f"{g.qid} 查询字出现在语料: {bad}")
            elif g.category == "metadata_compat":
                docs = self.docs_index(g.lib)
                bodies = [b for _t, _p, b in docs.values()]
                self.assertFalse(any(g.query in b for b in bodies),
                                 f"{g.qid} 查询词泄入正文")
                # 真 ingest 链 docdb 件 title 为空，元数据面 = title+path
                # （文件名携词）；逐件认证只有期望件的元数据面含查询词
                meta_hits = sorted(
                    l for l, (t, p, _b) in docs.items() if g.query in (t + p))
                self.assertEqual(meta_hits, [g.expect],
                                 f"{g.qid} 元数据面命中: {meta_hits}")

    # -- Phase 1: 检索矩阵（含消融） --------------------------------------

    def _fusion_top10(self, r):
        if r.status != 200:
            return None, r
        return [i["lineage_id"] for i in r.payload["items"]], r

    def _check_hit(self, gold: Gold):
        r = self.fusion(gold.lib, gold.query)
        if r.status != 200:
            self.fail_note(gold.qid, f"融合 {r.status} {r.payload['error']['code']}")
            self.record(gold, fusion_status=r.status, recall=False)
            return
        top, _ = self._fusion_top10(r)
        hit = gold.expect in top
        rank = top.index(gold.expect) + 1 if hit else None
        # 消融：metadata 单路
        mr = self.meta_search(gold.lib, gold.query)
        m_top = ([i["lineage_id"] for i in mr.payload["items"]]
                 if mr.status == 200 else None)
        meta_hit = bool(m_top and gold.expect in m_top)
        body_rank = meta_only = None
        if hit:
            row = next(i for i in r.payload["items"]
                       if i["lineage_id"] == gold.expect)
            body_rank, meta_only = row.get("body_rank"), row.get("metadata_rank")
        span_ok = self._check_span(gold) if (hit and gold.marker) else None
        self.record(gold, fusion_status=200, recall=hit, rank=rank,
                    metadata_solo_hit=meta_hit, body_rank=body_rank,
                    metadata_rank=meta_only, total=r.payload["total"],
                    span_ok=span_ok)
        if not hit:
            self.fail_note(gold.qid, f"未进 top10（total={r.payload['total']}）")

    def _check_zero(self, gold: Gold):
        fr = self.fusion(gold.lib, gold.query)
        mr = self.meta_search(gold.lib, gold.query)
        ok = (fr.status == 200 and fr.payload["total"] == 0
              and mr.status == 200 and mr.payload["total"] == 0)
        self.record(gold, fusion_status=fr.status,
                    fusion_total=fr.payload.get("total"),
                    metadata_total=mr.payload.get("total"), honest_zero=ok)
        if not ok:
            self.fail_note(gold.qid,
                           f"零命中不诚实 fusion={fr.payload.get('total')}"
                           f" meta={mr.payload.get('total')}")

    def _check_span(self, gold: Gold) -> bool:
        """gold marker 必须能从候选 span 走同一 reader 读回。"""
        r = self.fusion(gold.lib, gold.query)
        if r.status != 200:
            return False
        row = next((i for i in r.payload["items"]
                    if i["lineage_id"] == gold.expect), None)
        if row is None:
            return False
        for span in row["candidate_spans"]:
            rr = self.read(
                gold.lib, row["document_ref"],
                f"&start_byte={span['start_byte']}&end_byte={span['end_byte']}")
            if (rr.status == 200 and gold.marker in rr.payload["text"]
                    and rr.payload["full_sha_verified"]):
                return True
        return False

    def phase1_retrieval(self):
        for g in GOLD:
            if g.expect == "__zero__":
                self._check_zero(g)   # 含 Q18：017 不得命中 AB-017
            elif g.category in ("body_only", "short_term", "code",
                                "metadata_compat"):
                self._check_hit(g)

    # -- Phase 2: 版本（升 v2 → 钩子重建 → 旧 ref 拒 + 再召回） ------------

    def phase2_version(self):
        upgraded: dict = {}
        for g in [q for q in GOLD if q.category == "version"]:
            lib, fid = g.lib, g.expect.split(":")[1]
            docs = self.LIBS[lib]
            upgraded.setdefault(lib, set()).add(fid)
            old_ref = self.resolve(lib, g.expect)
            blobs = self._blobs(docs, {fid: V2_SUFFIX.encode("utf-8")
                                       for fid in upgraded[lib]})
            rep = run_refresh(
                self.backends[lib], self.codes[lib], self.roots[lib],
                {ROOT: self._rows(docs, upgraded[lib])}, blobs, self.env)
            lex_status = (rep.get("lexical") or {}).get("status")
            facts = dict(rebuilt=lex_status == "rebuilt")
            r_old = self.read(lib, old_ref)
            facts["old_ref_status"] = r_old.status
            facts["old_ref_code"] = r_old.payload.get("error", {}).get("code")
            r_new = self.get(lib, f"/v2/kb/resolve?kb={lib}&lineage={g.expect}")
            facts["new_version"] = r_new.payload.get("identity", {}).get(
                "source_version")
            rr = self.read(lib, r_new.payload["document_ref"])
            facts["v2_text"] = "修订版" in rr.payload.get("text", "")
            top, _ = self._fusion_top10(self.fusion(lib, g.query))
            facts["recall"] = bool(top and g.expect in top)
            facts["span_ok"] = (self._check_span(g) if facts["recall"] else False)
            self.record(g, **facts)
            for key, want in (("rebuilt", True), ("old_ref_status", 409),
                              ("old_ref_code", "stale_reference"),
                              ("new_version", 2), ("v2_text", True),
                              ("recall", True), ("span_ok", True)):
                if facts[key] != want:
                    self.fail_note(g.qid, f"{key}={facts[key]!r} 期望 {want!r}")

    # -- Phase 3: 隔离 / 撤权 / 句柄 -------------------------------------

    def phase3_isolation(self):
        by_qid = {g.qid: g for g in GOLD}
        registry = self.base / "tokens.json"
        record, bearer = issue_binding_token(registry, kb_ids=("liba",))
        now = {"t": FIXED_NOW}
        app = gateway.GatewayApp(
            self.backends["liba"], TOKEN, backend_kind="local",
            clock=lambda: now["t"], kb_id="liba",
            kb_mounts={"libb": self.backends["libb"]},
            tokens=kb_token.TokenFile(registry))

        # Q31: liba-token 越库 → 403 且不泄 B 库
        r = app.dispatch("GET", "/v2/kb/search?kb=libb&q="
                         + quote("基线") + "&page_size=10",
                         {gateway.TOKEN_HEADER: bearer})
        blob = json.dumps(r.payload, ensure_ascii=False)
        leak = "基准参考" in blob or '"total"' in blob and "701" in blob
        ok31 = r.status == 403 and not leak
        self.record(by_qid["Q31"], status=r.status, payload_clean=not leak, ok=ok31)
        if not ok31:
            self.fail_note("Q31", f"status={r.status} leak={leak}")

        # Q32: liba 内查 B 库 marker → 双零
        fr, mr = self.fusion("liba", "基线"), self.meta_search("liba", "基线")
        ok32 = (fr.status == 200 and fr.payload["total"] == 0
                and mr.status == 200 and mr.payload["total"] == 0)
        self.record(by_qid["Q32"], fusion_total=fr.payload.get("total"),
                    metadata_total=mr.payload.get("total"), ok=ok32)
        if not ok32:
            self.fail_note("Q32", "跨库泄漏")

        # Q33: admin 多挂载查 libb → 200 命中
        r = self.fusion("libb", "基线", app=app)
        top, _ = self._fusion_top10(r)
        ok33 = bool(top and "docdb:701" in top)
        self.record(by_qid["Q33"], status=r.status, hit=ok33, ok=ok33)
        if not ok33:
            self.fail_note("Q33", f"status={r.status}")

        # Q34: 撤权即刻生效
        data = kb_token.load_registry(registry)
        kb_token.revoke_token(data, token_id=record["token_id"],
                              actor="a11", reason="Q34")
        kb_token.save_registry(registry, data)
        r = app.dispatch("GET", "/v2/kb/list?kb=liba&page_size=1",
                         {gateway.TOKEN_HEADER: bearer})
        ok34 = r.status == 401
        self.record(by_qid["Q34"], status=r.status, ok=ok34)
        if not ok34:
            self.fail_note("Q34", f"status={r.status}")

        # Q35: ref 过期 410 → 重 resolve 恢复
        ref = self.resolve("liba", "docdb:601", app=app)
        now["t"] = FIXED_NOW + timedelta(seconds=901)
        r = self.get("liba", f"/v2/kb/inspect?kb=liba&document_ref={ref}", app=app)
        expired = r.status == 410
        now["t"] = FIXED_NOW
        ref2 = self.resolve("liba", "docdb:601", app=app)
        r2 = self.get("liba", f"/v2/kb/inspect?kb=liba&document_ref={ref2}", app=app)
        ok35 = expired and r2.status == 200
        self.record(by_qid["Q35"], expired_410=expired, resolved=r2.status, ok=ok35)
        if not ok35:
            self.fail_note("Q35", f"expired={expired} resolved={r2.status}")

        # Q36: 篡改句柄尾部 → 400
        flipped = ref2[:-2] + ("AA" if not ref2.endswith("AA") else "BB")
        r = self.get("liba", f"/v2/kb/read?kb=liba&document_ref={flipped}", app=app)
        ok36 = r.status == 400
        self.record(by_qid["Q36"], status=r.status, ok=ok36)
        if not ok36:
            self.fail_note("Q36", f"status={r.status}")

    # -- Phase 4: 索引故障 ≠ 源坏 + 自愈 ----------------------------------

    def phase4_index_fault(self):
        by_qid = {g.qid: g for g in GOLD}
        lib, backend = "libb", self.backends["libb"]
        lex_path = "_system/lexical-index.json"
        ref = self.resolve(lib, "docdb:710")

        # Q37: 词法缺失 → 融合 503，read 200
        (self.roots[lib] / "_system" / "lexical-index.json").unlink()
        f37 = self.fusion(lib, "采样")
        r37 = self.read(lib, ref)
        ok37 = (f37.status == 503
                and f37.payload["error"]["code"] == "lexical_unavailable"
                and r37.status == 200)
        self.record(by_qid["Q37"], fusion=f37.status, read=r37.status, ok=ok37)
        if not ok37:
            self.fail_note("Q37", f"fusion={f37.status} read={r37.status}")

        # Q38 + Q41: 词法损坏 → 503；metadata 200；read 200
        backend.write(lex_path, b"{corrupt")
        f38 = self.fusion(lib, "采样")
        m41 = self.meta_search(lib, "采样")
        r38 = self.read(lib, ref)
        ok38 = f38.status == 503 and r38.status == 200
        ok41 = m41.status == 200
        self.record(by_qid["Q38"], fusion=f38.status, read=r38.status, ok=ok38)
        self.record(by_qid["Q41"], metadata=m41.status, ok=ok41)
        if not ok38:
            self.fail_note("Q38", f"fusion={f38.status} read={r38.status}")
        if not ok41:
            self.fail_note("Q41", f"metadata={m41.status}")

        # Q39: 陈旧代 → 503（先恢复合法词法，再让资格域前进）
        self._publish(backend, self.codes[lib])
        f_ok = self.fusion(lib, "采样")
        keep_idx = (self.roots[lib] / "_system" / "raw-index.json").read_bytes()
        idx = read_json(backend, "_system/raw-index.json")
        idx["entries"]["docdb:710"]["version"] = 99
        backend.write("_system/raw-index.json", dumps(idx))
        f39 = self.fusion(lib, "采样")
        ok39 = f_ok.status == 200 and f39.status == 503
        (self.roots[lib] / "_system" / "raw-index.json").write_bytes(keep_idx)
        self.record(by_qid["Q39"], before=f_ok.status, stale=f39.status, ok=ok39)
        if not ok39:
            self.fail_note("Q39", f"before={f_ok.status} stale={f39.status}")

        # Q40: raw-index 不可读 → list 503 且无内容
        keep = (self.roots[lib] / "_system" / "raw-index.json").read_bytes()
        backend.write("_system/raw-index.json", b"{broken")
        r40 = self.get(lib, "/v2/kb/list?kb=libb")
        clean = "items" not in r40.payload and "text" not in r40.payload
        (self.roots[lib] / "_system" / "raw-index.json").write_bytes(keep)
        ok40 = r40.status == 503 and clean
        self.record(by_qid["Q40"], status=r40.status, clean=clean, ok=ok40)
        if not ok40:
            self.fail_note("Q40", f"status={r40.status} clean={clean}")

        # Q42: refresh 钩子自愈——升级 705（phase2 未动过）触发真实 rebuilt
        upgraded4 = ("701", "707", "710", "705")
        rep = run_refresh(self.backends[lib], self.codes[lib], self.roots[lib],
                          {ROOT: self._rows(self.LIBS[lib], upgraded=upgraded4)},
                          self._blobs(self.LIBS[lib],
                                      {fid: V2_SUFFIX.encode("utf-8")
                                       for fid in upgraded4}),
                          self.env)
        f42 = self.fusion(lib, "采样")
        ok42 = f42.status == 200
        self.record(by_qid["Q42"], heal_status=f42.status,
                    lexical=(rep.get("lexical") or {}).get("status"), ok=ok42)
        if not ok42:
            self.fail_note("Q42", f"heal={f42.status}")

    # -- 收口 -------------------------------------------------------------

    def test_a11_full_matrix(self):
        self.phase0_self_certify()
        self.phase1_retrieval()
        self.phase2_version()
        self.phase3_isolation()
        self.phase4_index_fault()
        self._dump_artifact()

        recs = {r["qid"]: r for r in self.rows_report}
        retrieval = [r for r in self.rows_report
                     if r["category"] in ("body_only", "short_term", "code",
                                          "no_answer", "version",
                                          "metadata_compat")]
        recall_hits = [
            r for r in retrieval
            if (r.get("recall") is True) or (r.get("honest_zero") is True)
        ]
        macro = len(recall_hits) / len(retrieval)
        body = [r for r in self.rows_report if r["category"] == "body_only"]
        body_recall = sum(1 for r in body if r.get("recall")) / len(body)
        short = [r for r in self.rows_report if r["category"] == "short_term"]
        short_recall = sum(1 for r in short if r.get("recall")) / len(short)
        span_qs = [r for r in retrieval if r.get("recall") and r["category"] in
                   ("body_only", "short_term", "code", "version")
                   and r.get("span_ok") is not None]
        span_ok = sum(1 for r in span_qs if r.get("span_ok"))
        span_recall = (span_ok / len(span_qs)) if span_qs else 1.0
        # 消融：正文-only 题在 metadata 单路必须全灭（证明正文路贡献）
        ablation_body_clean = all(not r.get("metadata_solo_hit")
                                  for r in body)
        ablation_meta_clean = all(
            r.get("body_rank") is None
            for r in self.rows_report if r["category"] == "metadata_compat"
            and r.get("recall"))
        wrong_version = [r["qid"] for r in self.rows_report
                         if r.get("old_ref_status") not in (None, 409)]
        iso_leaks = [r["qid"] for r in self.rows_report
                     if r["category"] in ("isolation", "index_fault") and not r.get("ok")]

        summary = (f"macro={macro:.3f} body={body_recall:.2f} "
                   f"short={short_recall:.2f} span={span_recall:.3f} "
                   f"ablation_body_clean={ablation_body_clean} "
                   f"ablation_meta_clean={ablation_meta_clean} "
                   f"wrong_version={wrong_version} iso_fault={iso_leaks} "
                   f"faults={self.faults}")
        self.assertGreaterEqual(macro, 0.90, summary)
        self.assertGreaterEqual(body_recall, 5 / 6, summary)
        self.assertGreaterEqual(short_recall, 5 / 6, summary)
        self.assertGreaterEqual(span_recall, 0.85, summary)
        self.assertTrue(ablation_body_clean, summary)
        self.assertTrue(ablation_meta_clean, summary)
        self.assertEqual(wrong_version, [], summary)
        self.assertEqual(iso_leaks, [], summary)
        self.assertEqual(self.faults, [], summary)

    def _dump_artifact(self):
        if os.environ.get("CWK_RT051_A11_DUMP") != "1":
            return
        out = PROJECT / "RT" / "RT-051" / "a11-results-latest.json"
        out.write_text(json.dumps(
            {"header": GOLD_HEADER, "results": self.rows_report,
             "faults": self.faults},
            ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
