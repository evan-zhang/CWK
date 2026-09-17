"""RT-059: the product site is a complete runbook that shows no library data.

Two promises carry this page:

1. a colleague who has never used the service can finish their own install
   and configuration from it — so the steps, the addresses and the register
   template must actually be on the page, not summarized away;
2. it displays nothing from the real corpus — no document, no hit, no count.
   The site process cannot read any of that (asserted by the import test in
   ``test_rt058_kb_console.py``); these tests guard the other direction, that
   nobody later hard-codes a number onto the page.
"""
from __future__ import annotations

import html
import re
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_portal


class RunbookCompletenessTests(unittest.TestCase):
    """Every step a newcomer must perform has to be present and executable."""

    def setUp(self) -> None:
        self.page = kb_portal.PortalApp({}).page()

    def test_all_five_steps_are_present(self):
        for heading in ("安装查询 Skill", "加进 Agent 白名单", "预检并收集登记信息",
                        "申请令牌", "落地令牌并验证"):
            self.assertIn(heading, self.page, f"接入步骤缺了「{heading}」")

    def test_the_commands_a_newcomer_must_run_are_there(self):
        for command in ("git clone --depth 1", "openclaw skills info cwk-kb-query",
                        "/healthz", "chmod 600 ~/.openclaw/cwk/kb.env", "/query"):
            self.assertIn(command, self.page, f"缺少命令：{command}")

    def test_register_template_carries_every_bank_as_a_checkbox(self):
        for bank_id, _ in kb_portal.DEFAULT_BANKS:
            self.assertIn(f"[ ] {bank_id}", self.page)

    def test_service_addresses_come_from_configuration(self):
        page = kb_portal.PortalApp({
            "KB_PORTAL_RETRIEVAL_BASE": "http://10.1.2.3:9999",
            "KB_PORTAL_ANSWER_BASE": "http://10.1.2.3:9998",
            "KB_PORTAL_REPO_URL": "https://git.example.internal/cwk.git",
        }).page()
        self.assertIn("http://10.1.2.3:9999/healthz", page)
        self.assertIn("https://git.example.internal/cwk.git", page)
        self.assertNotIn(kb_portal.DEFAULT_RETRIEVAL_BASE, page)

    def test_error_semantics_are_explained_not_just_listed(self):
        """401 and 403 are the two things newcomers misread as an outage."""
        self.assertIn("403", self.page)
        self.assertIn("401", self.page)
        self.assertIn("隔离", self.page)

    def test_copy_uses_the_non_secure_context_fallback(self):
        """The site is served over plain HTTP, where navigator.clipboard is absent."""
        self.assertIn("execCommand", self.page)
        self.assertNotIn("navigator.clipboard", self.page)


class NoCorpusDataTests(unittest.TestCase):
    """A promotional page, not a dashboard: nothing from the real corpus."""

    def setUp(self) -> None:
        self.page = kb_portal.PortalApp({}).page()

    def test_page_reports_no_counts_or_status_of_real_libraries(self):
        """接口词汇（hits/took_ms）是手册必须讲的；这里禁的是「实际统计值」的呈现面。"""
        for leaked in ("文档数", "可读数", "readable_total", "registry_status",
                       "令牌总数", "有效令牌", "lexical_status"):
            self.assertNotIn(leaked, self.page, f"官网出现了库内/管理数据字段：{leaked}")

    def test_bank_cards_carry_only_the_configured_name_and_description(self):
        """库名本就写在接入手册里；统计数字不是。

        判据不是「卡片里不许有数字」——库名自己就带 2027。判据是卡片的文字
        必须**恰好**等于配置里的那两段，多一个字都说明有人往上加了东西。
        """
        cards = re.findall(r"<article class='bank'><h3>(.*?)</h3><p>(.*?)</p></article>", self.page, re.S)
        self.assertEqual(len(cards), len(kb_portal.DEFAULT_BANKS))
        for (name, description), (want_name, want_desc) in zip(cards, kb_portal.DEFAULT_BANKS):
            self.assertEqual(name, want_name)
            self.assertEqual(description, html.escape(want_desc))

    def test_the_site_never_fetches_from_the_management_api(self):
        """No client-side call may turn the static page into a data surface."""
        for call in ("fetch(", "XMLHttpRequest", "/api/overview", "/api/audit", "X-KB-Admin-Key"):
            self.assertNotIn(call, self.page, f"页面出现了取数调用：{call}")


class SitePresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = kb_portal.PortalApp({}).page()

    def test_sections_are_anchored_for_the_nav(self):
        for anchor in ("value", "features", "setup", "limits", "faq", "admin"):
            self.assertIn(f"id='{anchor}'", self.page)
            self.assertIn(f"href='#{anchor}'", self.page)

    def test_contact_is_configurable_and_escaped(self):
        page = kb_portal.PortalApp({"KB_PORTAL_CONTACT": "<b>老王</b>"}).page()
        self.assertNotIn("<b>老王</b>", page)
        self.assertIn("&lt;b&gt;老王&lt;/b&gt;", page)

    def test_boundaries_section_states_what_it_will_not_do(self):
        for promise in ("只读", "找不到就说找不到", "快照"):
            self.assertIn(promise, self.page)

    def test_console_entry_appears_in_both_hero_and_admin_section(self):
        console = kb_portal.DEFAULT_CONSOLE_URL
        self.assertEqual(self.page.count(console), 2)

    def test_console_entry_still_rejects_a_non_http_scheme(self):
        page = kb_portal.PortalApp({"KB_PORTAL_CONSOLE_URL": "javascript:alert(1)"}).page()
        self.assertNotIn("javascript:", page)
        self.assertIn("管理控制台未配置", page)


if __name__ == "__main__":
    unittest.main()


class DiagramTests(unittest.TestCase):
    """部署图与流转图：内容要对得上现实，且不能把图变成数据面。"""

    def setUp(self) -> None:
        self.page = kb_portal.PortalApp({}).page()

    def test_both_diagrams_are_inline_svg(self):
        """内联而非外链：图要跟着主题变色，也不该再发一次网络请求。"""
        self.assertEqual(self.page.count("<svg"), 2)
        self.assertNotIn("<img", self.page)

    def test_diagrams_use_theme_variables_not_hard_coded_colors(self):
        for svg in (kb_portal._DEPLOY_SVG, kb_portal._FLOW_SVG):
            self.assertNotRegex(svg, r"(fill|stroke)='#[0-9a-fA-F]{3,6}'",
                                "图里写死了颜色，深色主题下会看不清")

    def test_deployment_diagram_names_every_port_a_user_meets(self):
        for port in (":8787", ":8790", ":8791", ":8792"):
            self.assertIn(port, kb_portal._DEPLOY_SVG)

    def test_deployment_diagram_marks_what_is_loopback_only(self):
        """索引和模型只在服务器本机可达——这是图要传达的主要事实之一。"""
        self.assertIn("仅本机", kb_portal._DEPLOY_SVG)
        self.assertIn("局域网", kb_portal._DEPLOY_SVG)

    def test_flow_diagram_covers_the_whole_chain_in_order(self):
        marks = [kb_portal._FLOW_SVG.index(n) for n in "①②③④⑤⑥⑦⑧⑨"]
        self.assertEqual(marks, sorted(marks), "步骤编号在源码里的顺序不是递增的")

    def test_flow_diagram_states_where_the_token_is_checked(self):
        self.assertIn("401", kb_portal._FLOW_SVG)
        self.assertIn("403", kb_portal._FLOW_SVG)

    def test_diagrams_are_accessible_and_scrollable(self):
        for svg in (kb_portal._DEPLOY_SVG, kb_portal._FLOW_SVG):
            self.assertIn("role='img'", svg)
            self.assertIn("aria-label=", svg)
        self.assertIn("class='figure'", self.page)


class DataSourceHonestyTests(unittest.TestCase):
    """承接能力必须与摄取管道的实际支持一致，否则同事照着试会撞墙。"""

    def test_the_two_working_sources_are_described(self):
        page = kb_portal.PortalApp({}).page()
        self.assertIn("工作协同系统", page)
        self.assertIn("云端文件库", page)

    def test_the_unsupported_source_is_marked_as_unavailable(self):
        """kb_ingest 的 --source 只认 cwork-mirror 与 docdb，第三类今天不存在。"""
        page = kb_portal.PortalApp({}).page()
        index = page.index("你自己上传的文件")
        nearby = page[index - 400:index + 400]
        self.assertIn("尚未开通", nearby)

    def test_the_claim_matches_the_ingest_adapters_actually_registered(self):
        """判据直接读 kb_ingest：将来加了适配器而官网没跟上，这里会红。"""
        import kb_ingest
        self.assertEqual(sorted(kb_ingest.ADAPTERS), ["cwork-mirror", "docdb"],
                         "摄取适配器变了——官网的『能承接哪些数据』必须同步更新")
