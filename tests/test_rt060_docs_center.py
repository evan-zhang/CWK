"""RT-060: 首页回归产品介绍，深度内容进文档中心。

两条判据线：

1. **首页是给没用过的人看的**——它不该再背着 API 字段表、命令清单和架构图。
   这条容易在后续迭代里被慢慢侵蚀，所以用「首页不得出现这些东西」正面挡住。
2. **文档中心自身是完整的**——侧栏上列出的每一页都真的存在、能打开、
   互相之间链得通，并且未注册的路径不会被当成文件去找。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_portal


def fetch(route: str, **env):
    return kb_portal.PortalApp(env).handle("GET", route)


def page(route: str, **env) -> str:
    status, _, body, _ = fetch(route, **env)
    assert status == 200, f"{route} 返回 {status}"
    return body.decode("utf-8")


class RoutingTests(unittest.TestCase):
    def test_every_sidebar_entry_resolves_to_a_real_page(self):
        """侧栏上写着的每一页都必须能打开——死链是文档中心最廉价的失信方式。"""
        for slug, title, _ in kb_portal._DOC_NAV:
            status, content_type, body, _ = fetch(f"/docs/{slug}")
            self.assertEqual(status, 200, f"/docs/{slug} 打不开")
            self.assertIn("html", content_type)
            self.assertIn(title, body.decode("utf-8"))

    def test_docs_index_links_every_page(self):
        index = page("/docs")
        for slug, title, desc in kb_portal._DOC_NAV:
            self.assertIn(f"/docs/{slug}", index)
            self.assertIn(title, index)
            self.assertIn(desc, index)

    def test_unknown_doc_slug_is_a_plain_404(self):
        """路径里的东西永远不会被当成文件名去找：只认注册过的 slug。"""
        for bad in ("/docs/nope", "/docs/../etc/passwd", "/docs/%2e%2e", "/docs/api/extra"):
            status, _, body, _ = fetch(bad)
            self.assertEqual(status, 404, bad)
            self.assertIn(b"not_found", body)

    def test_trailing_slash_and_index_html_reach_the_same_pages(self):
        self.assertEqual(page("/docs/api/"), page("/docs/api"))
        self.assertEqual(page("/index.html"), page("/"))

    def test_management_api_paths_are_still_absent_here(self):
        for route in ("/api/overview", "/api/audit", "/console"):
            self.assertEqual(fetch(route)[0], 404, route)

    def test_write_methods_are_refused_on_every_route(self):
        for route in ("/", "/docs", "/docs/api"):
            status, _, _, headers = kb_portal.PortalApp({}).handle("POST", route)
            self.assertEqual(status, 405)
            self.assertEqual(headers.get("Allow"), "GET, HEAD")


class HomeStaysIntroductoryTests(unittest.TestCase):
    """首页给小白看：讲清楚是什么、能干什么，不铺技术细节。"""

    def setUp(self) -> None:
        self.home = page("/")

    def test_home_does_not_carry_the_command_runbook(self):
        for detail in ("git clone", "openclaw skills info", "chmod 600", "curl -s"):
            self.assertNotIn(detail, self.home, f"首页又出现了命令：{detail}")

    def test_home_does_not_carry_api_field_tables(self):
        for detail in ("top_k", "doc_id", "X-KB-Token", "took_ms", "/query", "/answer"):
            self.assertNotIn(detail, self.home, f"首页又出现了接口细节：{detail}")

    def test_home_does_not_carry_the_diagrams(self):
        self.assertNotIn("<svg", self.home)

    def test_home_still_answers_what_is_this_and_why(self):
        for promise in ("每句结论都能追回原文", "四项能力", "当前可接入的知识库"):
            self.assertIn(promise, self.home)

    def test_home_offers_both_next_steps(self):
        self.assertIn("/docs/quickstart", self.home)
        self.assertIn("/docs", self.home)


class DocsContentTests(unittest.TestCase):
    def test_api_reference_matches_the_real_contract(self):
        """字段写错的 API 文档比没有文档更糟：照着它写的调用会被服务端拒绝。"""
        api = page("/docs/api")
        for field in ("bank", "query", "top_k", "hits", "no_answer", "took_ms",
                      "doc_id", "score", "channel", "citations", "offset", "eof", "total_chars"):
            self.assertIn(field, api, f"API 参考缺字段：{field}")
        for code in ("400", "401", "403", "404", "416", "503"):
            self.assertIn(code, api, f"API 参考缺状态码：{code}")

    def test_api_reference_states_the_limits_the_service_actually_enforces(self):
        """限制值直接读服务端契约：将来服务端改了而文档没跟上，这里会红。"""
        sys.path.insert(0, str(PROJECT))
        from adapters.opensearch_retrieval.service import contract

        api = page("/docs/api")
        self.assertIn(str(contract.MAX_QUERY_CHARS), api,
                      "API 参考写的查询长度上限与服务端实际执行的不一致")
        self.assertIn(str(contract.MAX_TOP_K), api,
                      "API 参考写的 top_k 上限与服务端实际执行的不一致")

    def test_extend_page_describes_the_four_adapter_operations(self):
        extend = page("/docs/extend")
        for op in ("discover", "fetch", "dedupe_key", "watch"):
            self.assertIn(op, extend, f"扩展开发缺适配器操作：{op}")
        self.assertIn("NormalizedDoc", extend)

    def test_extend_page_states_that_writes_are_not_exposed(self):
        extend = page("/docs/extend")
        self.assertIn("没有公网入口", extend)
        self.assertIn("写操作", extend)

    def test_architecture_page_carries_both_diagrams_and_the_sources(self):
        arch = page("/docs/architecture")
        self.assertEqual(arch.count("<svg"), 2)
        self.assertIn("工作协同系统", arch)
        self.assertIn("云端文件库", arch)

    def test_quickstart_is_reachable_from_the_home_call_to_action(self):
        self.assertIn("/docs/quickstart", page("/"))


if __name__ == "__main__":
    unittest.main()
