"""RT-063: 管理控制台入口放到每一页的顶部导航里。

原先入口只在首页最底部，用户常常滚不到那里就以为没有控制台。判据锁三件事：

1. 每一页（首页、文档中心、每个文档页）的顶部导航里都有入口，而且它出现在正文之前；
2. 入口地址沿用同一道安全闸——非 http(s) 的配置不会变成可点击的脚本，
   未配置时导航里干脆不画按钮；
3. 窄屏下导航会横向滚动，要让出次要链接，保证入口不被挤到首屏之外。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_portal  # noqa: E402

CONSOLE = "http://192.168.91.72:8791/console"


def page(route: str, **env) -> str:
    status, _, body, _ = kb_portal.PortalApp(env).handle("GET", route)
    assert status == 200, f"{route} 返回 {status}"
    return body.decode("utf-8")


def nav_of(html_text: str) -> str:
    match = re.search(r"<nav>.*?</nav>", html_text, re.S)
    assert match, "页面没有顶部导航"
    return match.group(0)


def every_route():
    yield "/"
    yield "/docs"
    for slug, _, _ in kb_portal._DOC_NAV:
        yield f"/docs/{slug}"


class ConsoleInHeaderTests(unittest.TestCase):
    def test_every_page_has_the_console_entry_in_its_top_nav(self):
        for route in every_route():
            with self.subTest(route):
                text = page(route, KB_PORTAL_CONSOLE_URL=CONSOLE)
                nav = nav_of(text)
                self.assertIn(f"href='{CONSOLE}'", nav)
                self.assertIn("管理控制台", nav)
                self.assertLess(text.index(f"href='{CONSOLE}'"), text.index("</nav>"),
                                "入口必须先于正文出现，而不是只在页面底部")

    def test_entry_is_the_last_item_so_it_sits_at_the_right_edge(self):
        nav = nav_of(page("/", KB_PORTAL_CONSOLE_URL=CONSOLE))
        links = re.findall(r"<a [^>]*>", nav)
        self.assertIn("navcta", links[-1])
        self.assertLess(nav.index("spacer"), nav.index("navcta"))

    def test_the_bottom_admin_section_is_kept_as_a_second_way_in(self):
        home = page("/", KB_PORTAL_CONSOLE_URL=CONSOLE)
        self.assertEqual(home.count(f"href='{CONSOLE}'"), 2)
        self.assertIn("管理员入口", home)


class ConsoleUrlSafetyTests(unittest.TestCase):
    def test_unsafe_or_missing_url_draws_no_button_in_the_nav(self):
        for value in ("javascript:alert(1)", "data:text/html,x", "not a url", "  "):
            for route in every_route():
                with self.subTest(value=value, route=route):
                    text = page(route, KB_PORTAL_CONSOLE_URL=value)
                    self.assertNotIn("navcta", nav_of(text))
                    self.assertNotIn("javascript:", text)

    def test_quotes_in_a_configured_url_cannot_break_out_of_the_attribute(self):
        text = page("/docs", KB_PORTAL_CONSOLE_URL="https://kb.internal/console'onmouseover='alert(1)")
        nav = nav_of(text)
        self.assertNotIn("'onmouseover='", nav)
        self.assertIn("&#x27;onmouseover=&#x27;", nav)


class NarrowScreenTests(unittest.TestCase):
    def test_secondary_links_give_way_on_narrow_screens_but_the_entry_does_not(self):
        css = kb_portal._CSS
        narrow = css[css.index("@media (max-width:640px)"):]
        narrow = narrow[:narrow.index("\n}") + 2]
        self.assertIn("nav a.secondary{display:none}", narrow)
        self.assertNotIn("navcta", narrow)
        nav = nav_of(page("/", KB_PORTAL_CONSOLE_URL=CONSOLE))
        self.assertIn("class='secondary' href='/docs/quickstart'", nav)


if __name__ == "__main__":
    unittest.main()
