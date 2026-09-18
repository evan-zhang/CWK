"""门户文档页在手机宽度下不得把整页撑出横向滚动。

根因是 CSS Grid 的 `1fr` 默认等于 `minmax(auto, 1fr)`：正文里一条长 URL
或一段代码会把列的最小宽度抬到视口之外。本文件锁住修法本身——列宽必须允许
缩到 0，长端点地址必须允许断行，所有数据表都必须包在可横向滚动的容器里。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_portal  # noqa: E402


def page(route: str, **env) -> str:
    status, _, body, _ = kb_portal.PortalApp(env).handle("GET", route)
    assert status == 200, f"{route} 返回 {status}"
    return body.decode("utf-8")


class DocLayoutDoesNotForcePageScrollTests(unittest.TestCase):
    def test_doc_grid_columns_may_shrink_below_content_min_size(self):
        css = kb_portal._CSS
        self.assertIn(
            "grid-template-columns:minmax(0,220px) minmax(0,1fr)",
            css,
            "宽屏两栏也必须允许列缩到 0，否则长内容照样撑出视口",
        )
        narrow = css[css.index("@media (max-width:820px)"):]
        narrow = narrow[: narrow.index("\n}") + 2]
        self.assertIn("grid-template-columns:minmax(0,1fr)", narrow)

    def test_long_endpoints_and_doc_text_are_allowed_to_wrap(self):
        css = kb_portal._CSS
        self.assertIn(".doc{min-width:0;overflow-wrap:anywhere", css)
        self.assertIn(".endpoint{", css)
        endpoint = css[css.index(".endpoint{"): css.index(".endpoint{") + 200]
        self.assertIn("overflow-wrap:anywhere", endpoint)
        self.assertIn("word-break:break-all", endpoint)

    def test_every_doc_table_sits_inside_a_horizontal_scroll_shell(self):
        """裸表会按内容撑宽；统一进 .scroll，横向溢出只发生在表自己内部。"""
        for slug, _, _ in kb_portal._DOC_NAV:
            html = page(f"/docs/{slug}")
            tables = re.findall(r"<table[\s>]", html)
            wrapped = re.findall(r"<div class='scroll'><table", html)
            self.assertEqual(
                len(tables),
                len(wrapped),
                f"/docs/{slug} 有 {len(tables)} 张表，但只有 {len(wrapped)} 张包在 .scroll 里",
            )


if __name__ == "__main__":
    unittest.main()
