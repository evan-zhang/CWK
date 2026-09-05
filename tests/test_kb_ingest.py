"""RT-043 摄取管道判据：lineage 寻址、格式工厂、源适配器、计划与确认卡。

判据编号对应 RT/RT-043/rt-lite.md：

- J1 originals write-once：同源同件重复摄取幂等（零写入、零账变）
- J2 覆盖率对账：手工删掉一个 raw 产物 → reconcile 必红
- J3 raw-index 原子写：写中断后旧 index 完整可用
- J4 状态账与断点续跑：failed 件重跑只处理未完成
- J5 源故障必红：源目录消失 / 适配器 5xx → 批次红且已完成件保留
- J6 lineage 寻址：同 ID 第二版 → 同条目 versions+1、supersedes 链正确

全部离线：源用临时目录 fake，DocDB 用 fake subprocess，后端用 LocalFSBackend /
MemoryBackend。没有任何一条测试会连真 NAS 或真 DocDB。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

import kb_create as create  # noqa: E402
import kb_ingest as ingest  # noqa: E402
from kb_storage import LocalFSBackend  # noqa: E402

FIXED_NOW = datetime(2026, 9, 5, 3, 0, 0, tzinfo=timezone.utc)

# Points the docx converter at nothing, so "本机没有转换器" is a property of
# the test rather than of whoever's laptop is running it.
NO_CONVERTER = {ingest.ENV_DOCX_CONVERTER: "/nonexistent/md2md"}


# ── shared fixtures ─────────────────────────────────────────────────────────


def make_kb(root: Path, sources=("cwork",)) -> str:
    """Build a real RT-042 library so the tests exercise the actual floor."""
    spec = create.KbSpec(
        display_name="摄取测试库",
        kb_code="b" * 32,
        owner_ref="owner-43",
        created_at=FIXED_NOW,
        sources=tuple(
            create.SourceSpec(source_type=name,
                              docdb_root="/玄关/合同" if name == "docdb" else None)
            for name in sources
        ),
    )
    create.create_kb(LocalFSBackend(root), spec)
    return spec.kb_code


def write_mirror(root: Path, rel: str, body: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def run_cli(argv) -> tuple:
    """Run the CLI and return ``(exit_code, parsed_stdout)``."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = ingest.main(argv)
    text = buffer.getvalue()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic aid
        raise AssertionError(f"CLI 没有输出单个 JSON 对象：{text[:400]}（{exc}）")
    return code, payload


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeDocdb:
    """A stand-in for ``scripts/browse/browse.py`` and friends.

    ``folders`` maps a folder id to the rows browse.py would print.  Setting
    ``fail_with`` makes every call answer with that resultMsg, which is how
    the 5xx criterion is driven without a network.

    ``full_content`` maps a fileId to what ``get-full-content.py`` answers
    with: a string is the document's text, ``None`` means the endpoint failed
    permanently, and an absent id means it answered with an empty ``data``.
    """

    def __init__(
        self,
        folders: dict,
        fail_with: str = "",
        blobs=None,
        fail_ids=(),
        full_content=None,
    ) -> None:
        self.folders = folders
        self.fail_with = fail_with
        self.blobs = blobs or {}
        self.fail_ids = set(fail_ids)
        self.full_content = full_content or {}
        self.calls: list = []

    def __call__(self, cmd, cwd=None, env=None, text=None, capture_output=None):
        self.calls.append(list(cmd))
        script = Path(cmd[1]).name
        if self.fail_with:
            return FakeProc(
                0,
                json.dumps({"resultCode": 0, "resultMsg": self.fail_with, "data": None}),
            )
        if script == "browse.py":
            rows = self.folders.get(str(cmd[2]), [])
            return FakeProc(0, json.dumps({"resultCode": 1, "resultMsg": "ok", "data": rows}))
        if script == "download-file.py":
            file_id = str(cmd[2])
            if file_id in self.fail_ids:
                return FakeProc(0, json.dumps(
                    {"resultCode": 0, "resultMsg": "503 Service Unavailable", "data": None}
                ))
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(self.blobs.get(file_id, b"# docdb\n"))
            return FakeProc(0, json.dumps({"resultCode": 1, "resultMsg": "ok", "data": {}}))
        if script == "get-full-content.py":
            file_id = str(cmd[cmd.index("--file-id") + 1])
            body = self.full_content.get(file_id, "")
            if body is None:
                return FakeProc(0, json.dumps(
                    {"resultCode": 0, "resultMsg": "403 Forbidden", "data": None}
                ))
            return FakeProc(0, json.dumps(
                {"resultCode": 1, "resultMsg": "ok", "data": body}, ensure_ascii=False
            ))
        raise AssertionError(f"fake 没有实现 {script}")


# ── synthetic binaries (RT-046 夹具) ─────────────────────────────────────────
#
# 夹具全部现生成，进 git 的只有生成它们的代码。一个 checked-in 的 .pdf/.xlsx
# 二进制在 review 里读不出任何东西——没人能说清它到底钉住了哪条分支，也没人
# 敢改它。下面这几十行反过来把「这份 PDF 长什么样」写成了判据的一部分，而且
# 体积是零。


def _pdf_stream_object(payload: bytes, *, compress: bool = True) -> bytes:
    body = zlib.compress(payload) if compress else payload
    filt = b"/Filter /FlateDecode " if compress else b""
    return (b"<< " + filt + b"/Length " + str(len(body)).encode() + b" >>\nstream\n"
            + body + b"\nendstream")


def _pdf_assemble(objects) -> bytes:
    """Header, numbered objects, xref, trailer — a real PDF, not a stub."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start_xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
            b"startxref\n" + str(start_xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def latin_pdf(lines, *, compress: bool = True) -> bytes:
    """A Helvetica page: literal strings, the easy half of the parser."""
    ops = [b"BT", b"/F1 12 Tf", b"72 720 Td"]
    for line in lines:
        escaped = (line.encode("latin-1").replace(b"\\", b"\\\\")
                   .replace(b"(", b"\\(").replace(b")", b"\\)"))
        ops += [b"(" + escaped + b") Tj", b"0 -16 Td"]
    ops.append(b"ET")
    return _pdf_assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        _pdf_stream_object(b"\n".join(ops), compress=compress),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def cjk_pdf(lines, *, ranges: bool = False, tounicode: bool = True) -> bytes:
    """An Identity-H subset font — the shape every Word-exported 中文 PDF has.

    页面字节是 CID（子集里的第几个字形），意思全在 ToUnicode CMap 里。
    ``tounicode=False`` 把 CMap 摘掉，得到的就是「抽出来是乱码」的那一份；
    ``ranges=True`` 换成 bfrange 数组写法，Word 最常写的正是这一种。
    """
    chars: list = []
    for line in lines:
        for char in line:
            if char not in chars:
                chars.append(char)
    cid = {char: index + 1 for index, char in enumerate(chars)}
    ops = [b"BT", b"/F1 12 Tf", b"72 720 Td"]
    for line in lines:
        hexed = "".join("%04X" % cid[char] for char in line).encode()
        ops += [b"<" + hexed + b"> Tj", b"0 -16 Td"]
    ops.append(b"ET")
    if ranges:
        block = (b"1 beginbfrange\n<0001> <%04X> [" % len(cid)
                 + b"".join(b"<%04X>" % ord(char) for char in chars)
                 + b"]\nendbfrange\n")
    else:
        block = (str(len(cid)).encode() + b" beginbfchar\n"
                 + b"".join(b"<%04X> <%04X>\n" % (code, ord(char))
                            for char, code in cid.items())
                 + b"endbfchar\n")
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + block
            + b"endcmap CMapName currentdict /CMap defineresource pop end end")
    to_unicode = b"/ToUnicode 6 0 R " if tounicode else b""
    return _pdf_assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        _pdf_stream_object(b"\n".join(ops)),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+SimSun /Encoding /Identity-H "
        b"/DescendantFonts [7 0 R] " + to_unicode + b">>",
        _pdf_stream_object(cmap),
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+SimSun "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor 8 0 R /DW 1000 >>",
        b"<< /Type /FontDescriptor /FontName /ABCDEF+SimSun /Flags 4 "
        b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 900 /Descent -200 "
        b"/CapHeight 700 /StemV 80 >>",
    ])


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 8
GIF_BYTES = b"GIF89a" + b"\x00" * 16

OOXML_MARKER = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}


def ooxml_zip(kind: str) -> bytes:
    """The smallest thing that still *is* an OOXML container of that kind."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{OOXML_MARKER[kind]}/document.xml", "<x/>")
    return buffer.getvalue()


def plain_zip(names=("合同/正本.pdf", "合同/附件.xlsx")) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"x")
    return buffer.getvalue()


def xlsx_with_cached_formula() -> bytes:
    """A workbook whose one interesting cell is ``=SUM(A2:A3)``, cached as 82.

    RT-108 实测：anydoc 丢掉公式的**缓存值**，markitdown 保留。缓存值恰恰是
    结论（总额 / CAGR / 峰值），这也是本 RT 把 xlsx 排成
    openpyxl(data_only=True) → markitdown 而不是反过来的理由。
    """
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package'
            '.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd'
            '.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd'
            '.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships"><Relationship Id="rId1" Type="{rel}/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'
            '<sheet name="市场测算" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships"><Relationship Id="rId1" Type="{rel}/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{ns}"><dimension ref="A1:A4"/><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>金额</t></is></c></row>'
            '<row r="2"><c r="A2"><v>40</v></c></row>'
            '<row r="3"><c r="A3"><v>42</v></c></row>'
            '<row r="4"><c r="A4"><f>SUM(A2:A3)</f><v>82</v></c></row>'
            '</sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


@contextlib.contextmanager
def stub_module(name: str, **attributes):
    """Put a fake optional converter in ``sys.modules`` for the duration.

    缺失分支在裸 CI 上天然覆盖（anydoc / markitdown 都不在），可**装上了会
    怎样**恰恰是这次改动的主张——不这样做，成功分支就只能靠"在我机器上试过"
    背书。``module_available`` 先看 sys.modules 正是为了让这条路可测。
    """
    previous = sys.modules.get(name)
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    try:
        yield module
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@contextlib.contextmanager
def without_modules(*names: str):
    """Hide the named optional packages from the whole converter table.

    ``stub_module`` 的反面，同样是为了让判据与机器无关：一条格式行是一串
    转换器（xlsx 是 openpyxl → markitdown），只挡掉第一个，装了第二个的机器
    会正确地降到 module-text 而不是 placeholder——断言 placeholder 的测试就
    在开发机上红、在裸 CI 上绿。要钉「整条链都没有时会怎样」，就得把整条链
    一起挡掉。
    """
    hidden = set(names)
    original = ingest.module_available
    ingest.module_available = lambda module: (
        False if module in hidden else original(module)
    )
    try:
        yield
    finally:
        ingest.module_available = original


class FakeOptionalConverter:
    """anydoc / markitdown 的最小替身，记下它被交到手里的那个路径。

    路径要记，是因为两个转换器都按后缀选解析器：把嗅探出来的 docx 用一个没有
    后缀的临时文件名递过去，它会安静地当纯文本处理，产物看不出错。
    """

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.paths: list = []

    def to_markdown(self, path: str) -> str:  # anydoc 的形状
        self.paths.append(path)
        return self.text

    def MarkItDown(self):  # noqa: N802 - markitdown 的形状，照抄上游命名
        outer = self

        class _MarkItDown:
            def convert(self, path):
                outer.paths.append(path)
                return types.SimpleNamespace(text_content=outer.text)

        return _MarkItDown()


# ── lineage identity ────────────────────────────────────────────────────────


class LineageAddressingTests(unittest.TestCase):
    """J6 前置：键的形状。键错了，版本链根本不会形成。"""

    def test_j6_lineage_key_is_source_colon_stable_id(self) -> None:
        self.assertEqual(ingest.lineage_id("cwork", "2095046023776104449"),
                         "cwork:2095046023776104449")
        self.assertEqual(ingest.lineage_id("docdb", "2087519593823322113"),
                         "docdb:2087519593823322113")

    def test_j6_lineage_key_refuses_rev_and_seq_suffixes(self) -> None:
        # 负例：把快照号写进键。这些串在第一次摄取时看起来完全正常，
        # 第二版到达时才暴露——那时版本链已经错过了。
        for bad in ("docdb:2087@7", "cwork:2095~2", "docdb:2087.v2", "cwork:a/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ingest.IngestError):
                    ingest.assert_lineage_key(bad)

    def test_j6_split_item_anchor_is_still_an_identity(self) -> None:
        # DOCDB-INGEST-DESIGN §II：拆分件 <fileId>#<内容锚点slug> 是身份的一部分，
        # 不是版本，必须放行——否则 docdb 拆分件永远进不了索引。
        self.assertEqual(
            ingest.assert_lineage_key("docdb:2087519593823322113#第三章-验收"),
            "docdb:2087519593823322113#第三章-验收",
        )

    def test_lineage_key_refuses_unknown_source_and_empty_id(self) -> None:
        for bad in ("mystery:123", "cwork:", "no-colon", "a:b:c"):
            with self.subTest(bad=bad):
                with self.assertRaises(ingest.IngestError):
                    ingest.assert_lineage_key(bad)


class StableIdTests(unittest.TestCase):
    def test_report_id_is_the_leading_digit_run(self) -> None:
        self.assertEqual(
            ingest.stable_id_from_name("2095046023776104449-周报.md"), "2095046023776104449"
        )

    def test_a_date_prefixed_name_is_not_mistaken_for_an_id(self) -> None:
        # 坏情形：把 2026-08-14-周报.md 认成 report 2026。那样同一年的
        # 每一件都会挤进同一个 lineage，第二件直接覆盖第一件。
        self.assertIsNone(ingest.stable_id_from_name("2026-08-14-周报.md"))
        self.assertIsNone(ingest.stable_id_from_name("README.md"))


class DateDerivationTests(unittest.TestCase):
    def test_written_date_beats_file_mtime(self) -> None:
        # mtime 指向 2020 年，路径写着 2026-08-14。复制一份镜像会重写
        # 每个 mtime；照 mtime 落位会把整个档案重新归到复制那天。
        mtime = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            ingest.derive_date("2026-08/2026-08-14/2095046023776104449-周报.md", mtime),
            "2026-08-14",
        )

    def test_month_directory_falls_back_to_the_first_of_the_month(self) -> None:
        self.assertEqual(
            ingest.derive_date("2026-08/2095046023776104449-周报.md", None), "2026-08-01"
        )

    def test_mtime_is_the_last_resort(self) -> None:
        mtime = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(ingest.derive_date("flat/2095046023776104449-周报.md", mtime),
                         "2026-07-09")

    def test_no_date_anywhere_yields_none(self) -> None:
        self.assertIsNone(ingest.derive_date("flat/2095046023776104449-周报.md", None))


class SlugTests(unittest.TestCase):
    def test_slug_keeps_cjk_and_drops_path_characters(self) -> None:
        self.assertEqual(ingest.slugify("八月/周报 v2"), "八月-周报-v2")
        self.assertEqual(ingest.slugify("../../etc/passwd"), "etc-passwd")

    def test_slug_never_returns_empty(self) -> None:
        self.assertEqual(ingest.slugify("///"), "untitled")
        self.assertEqual(ingest.slugify(""), "untitled")


# ── format factory (decision half) ──────────────────────────────────────────


class FormatFactoryDecisionTests(unittest.TestCase):
    """DOCDB-INGEST-DESIGN §V 的 v1 范围表，逐行。"""

    def test_markdown_and_text_pass_through(self) -> None:
        for name in ("a.md", "b.markdown", "c.txt"):
            decision = ingest.decide_format(name, env={})
            self.assertEqual((decision.handling, decision.expected_status),
                             ("passthrough", "converted"), name)

    def test_docx_converts_when_the_host_binary_exists_and_degrades_when_it_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            converter = Path(tmp) / "md2md"
            converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            converter.chmod(0o755)
            good = ingest.decide_format("x.docx", env={ingest.ENV_DOCX_CONVERTER: str(converter)})
            self.assertEqual((good.handling, good.expected_status), ("docx-convert", "converted"))
        # docx 链是 docx-host → anydoc：只挡本机二进制，装了 anydoc 的机器会
        # 正确地降到 module-text。「本机没有 docx 转换器」得由测试规定。
        with without_modules("anydoc"):
            missing = ingest.decide_format(
                "x.docx", env={ingest.ENV_DOCX_CONVERTER: str(Path(tmp) / "gone")}
            )
        self.assertEqual((missing.handling, missing.expected_status),
                         ("placeholder", "placeholder"))

    def test_xlsx_follows_openpyxl_availability(self) -> None:
        with_lib = ingest.decide_format("s.xlsx", env={}, has_openpyxl=True)
        self.assertEqual(with_lib.handling, "xlsx-csv")
        # 整条链一起挡：只关掉 openpyxl 就断言 placeholder，等于假设本机没装
        # markitdown——那是机器的属性，不是这条判据的内容。
        with without_modules("openpyxl", "markitdown"):
            without = ingest.decide_format("s.xlsx", env={}, has_openpyxl=False)
        self.assertEqual(without.handling, "placeholder")
        self.assertEqual(without.code, "converter-missing:xlsx")

    def test_xlsx_falls_through_to_the_next_converter_in_the_chain(self) -> None:
        """openpyxl 缺席不等于降级：表里 xlsx 的第二顺位是 markitdown。"""
        with without_modules("openpyxl"), stub_module("markitdown"):
            decision = ingest.decide_format("s.xlsx", env={}, has_openpyxl=False)
        self.assertEqual((decision.handling, decision.converter),
                         ("module-text", "markitdown"))

    def test_pptx_images_and_zip_are_placeholders(self) -> None:
        for name in ("deck.pptx", "photo.png", "scan.JPG", "bundle.zip"):
            decision = ingest.decide_format(name, env={})
            self.assertEqual(decision.expected_status, "placeholder", name)

    def test_rar_and_7z_are_skipped_not_placeheld(self) -> None:
        for name in ("archive.rar", "archive.7z"):
            decision = ingest.decide_format(name, env={})
            self.assertEqual((decision.handling, decision.expected_status), ("skip", "skipped"),
                             name)

    def test_unknown_extension_takes_the_generic_placeholder_path(self) -> None:
        decision = ingest.decide_format("mystery.qqq", env={})
        self.assertEqual((decision.format, decision.expected_status), ("unknown", "placeholder"))


# ── format factory v1.1: the converter table ────────────────────────────────


class ConverterTableTests(unittest.TestCase):
    """RT-046 转换器表：谁认领哪个后缀、链怎么排、缺了怎么记账。

    表的价值全在「可读、可审、可反驳」：判决必须只由表决定，不由行序、不由
    if 链的书写顺序、更不由某台机器上碰巧装了什么决定。
    """

    def test_every_suffix_is_claimed_by_exactly_one_row(self) -> None:
        # 两行都认领 .docx 时，判决就取决于行序——读代码的人看不见的东西。
        # 表在 import 期自检；这里把自检本身钉住，否则它明天可以被删掉。
        self.assertEqual(ingest.FORMAT_BY_SUFFIX[".docx"].format, "docx")
        self.assertEqual(ingest.FORMAT_BY_SUFFIX[".xlsm"].format, "xlsx")
        with self.assertRaises(ingest.IngestError):
            ingest.index_format_table(
                ingest.FORMAT_TABLE + (ingest.FormatRow("shadow", (".docx",), ()),)
            )

    def test_the_table_is_the_one_the_rt_signed_off(self) -> None:
        chains = {row.format: row.chain for row in ingest.FORMAT_TABLE}
        self.assertEqual(chains["markdown"], ("passthrough",))
        self.assertEqual(chains["docx"], ("docx-host", "anydoc"))
        self.assertEqual(chains["xlsx"], ("openpyxl", "markitdown"))
        self.assertEqual(chains["pdf"], ("pdf-text",))
        # 没有转换器的格式要显式写成空链，而不是从表里消失：缺行会退回
        # 「未知扩展名」，占位理由也就说不清是"不支持"还是"没装"。
        self.assertEqual((chains["pptx"], chains["image"], chains["zip"]), ((), (), ()))

    def test_every_chain_names_a_converter_the_table_defines(self) -> None:
        known = set(ingest.TEXT_HANDLINGS) | {"passthrough", "xlsx-csv"}
        for row in ingest.FORMAT_TABLE:
            for name in row.chain:
                with self.subTest(format=row.format, converter=name):
                    self.assertIn(name, ingest.CONVERTERS)
                    # materialise() 按 handling 分发；表里写一个它不认识的
                    # handling，件会静静掉进末尾的通用占位分支。
                    self.assertIn(ingest.CONVERTERS[name].handling, known)

    def test_an_unknown_converter_name_is_an_error_not_a_quiet_false(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.converter_available("no-such-converter")

    def test_docx_falls_through_to_anydoc_when_the_host_binary_is_absent(self) -> None:
        with stub_module("anydoc", to_markdown=lambda path: ""):
            decision = ingest.decide_format("x.docx", env=NO_CONVERTER)
        self.assertEqual(
            (decision.converter, decision.handling, decision.expected_status),
            ("anydoc", "module-text", "converted"),
        )

    def test_the_host_binary_still_wins_when_both_are_installed(self) -> None:
        # 装上 anydoc 不该无声改变既有产物：本机通路是这台机器上已验证的那条，
        # 顺序换了等于把全库正文的来源换掉，而账上看不出发生过什么。
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "md2md"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            with stub_module("anydoc", to_markdown=lambda path: ""):
                decision = ingest.decide_format(
                    "x.docx", env={ingest.ENV_DOCX_CONVERTER: str(binary)}
                )
        self.assertEqual(decision.converter, "docx-host")

    def test_xlsx_falls_through_to_markitdown_without_openpyxl(self) -> None:
        with stub_module("markitdown"):
            decision = ingest.decide_format("s.xlsx", env={}, has_openpyxl=False)
        self.assertEqual((decision.converter, decision.handling),
                         ("markitdown", "module-text"))

    def test_a_format_whose_whole_chain_is_missing_degrades_and_is_recorded(self) -> None:
        # 反空转：静默跳过会让「没转成」和「不用转」在账上长得一模一样，
        # 装上转换器那天也就没人问得出该重跑哪些件。
        with without_modules("anydoc"):
            decision = ingest.decide_format("x.docx", env=NO_CONVERTER)
        self.assertEqual(decision.expected_status, "placeholder")
        self.assertEqual(decision.code, f"{ingest.CODE_CONVERTER_MISSING}:docx")
        self.assertIn("docx-host/anydoc", decision.reason)

    def test_a_format_with_no_converter_at_all_is_not_reported_as_missing(self) -> None:
        # pptx 不是"没装"，是 v1.1 没承诺。两者的重跑价值完全不同，理由码
        # 必须分得开。
        decision = ingest.decide_format("deck.pptx", env={})
        self.assertEqual(decision.code, "pptx-not-converted-in-v1")

    def test_the_receipt_version_is_detected_never_read_off_the_pin(self) -> None:
        # 回执写 0.1.9 只是因为表里写着 0.1.9——那对产物的字节零证明力。
        self.assertEqual(ingest.CONVERTERS["anydoc"].pin, ingest.ANYDOC_PIN)
        self.assertEqual(ingest.converter_version("pdf-text"),
                         ingest.BUILTIN_CONVERTER_VERSION)
        # 本机二进制回答不了版本问题（可能压根不认 --version），于是照实说
        # unknown，把路径留在 converter_label 里，而不是编一个号出来。
        self.assertEqual(ingest.converter_version("docx-host"), ingest.UNKNOWN_VERSION)

    @unittest.skipIf(ingest.module_available("anydoc"), "本机装了 anydoc，探测结果不受控")
    def test_an_optional_module_reports_its_own_version_or_unknown(self) -> None:
        with stub_module("anydoc", to_markdown=lambda path: ""):
            self.assertEqual(ingest.converter_version("anydoc"), ingest.UNKNOWN_VERSION)
        with stub_module("anydoc", __version__="9.9.9-stub", to_markdown=lambda path: ""):
            self.assertEqual(ingest.converter_version("anydoc"), "9.9.9-stub")

    def test_the_label_keeps_the_host_detail_the_pair_cannot(self) -> None:
        docx = ingest.FormatDecision("docx", "docx-convert", "converted", "",
                                     converter="docx-host")
        self.assertEqual(
            ingest.converter_label(docx, {ingest.ENV_DOCX_CONVERTER: "/opt/bin/md2md"}),
            "docx:/opt/bin/md2md",
        )
        pdf = ingest.FormatDecision("pdf", "pdf-text", "converted", "", converter="pdf-text")
        self.assertEqual(ingest.converter_label(pdf, {}), "pdf:pdf-text")

    @unittest.skipUnless(ingest.openpyxl_available(), "本机没有 openpyxl（可选 extra）")
    def test_xlsx_keeps_the_cached_formula_value(self) -> None:
        # 这条是 xlsx 链排成 openpyxl → markitdown 的**依据**（kb_ingest.py
        # 的表里按名字引了它）：公式的缓存值就是结论，丢了它，表还在、结论
        # 没了，而且检索侧一个字都问不出来。
        sheets = ingest.convert_xlsx(xlsx_with_cached_formula())
        text = "".join(sheets.values())
        self.assertIn("82", text)
        self.assertNotIn("SUM(", text, "公式文本不是结论，缓存值才是")


# ── format factory v1.1: magic-byte sniffing ────────────────────────────────


class MagicByteSniffTests(unittest.TestCase):
    """无后缀件的判据面。投前库里三份关键流程文档就是这样进来的。"""

    NO_SUFFIX = "2095046023776104474-无后缀"

    def test_magic_bytes_name_the_format(self) -> None:
        for data, expected in (
            (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3", ".pdf"),
            (PNG_BYTES, ".png"),
            (JPEG_BYTES, ".jpg"),
            (GIF_BYTES, ".gif"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(ingest.sniff_suffix(data), expected)

    def test_a_zip_is_read_down_to_which_application_wrote_it(self) -> None:
        # PK 头只说"这是个 zip"。docx 和 xlsx 的处置完全不同，停在 zip 这一层
        # 等于把两份能转的正文一起判成占位。
        self.assertEqual(ingest.sniff_suffix(ooxml_zip("docx")), ".docx")
        self.assertEqual(ingest.sniff_suffix(ooxml_zip("xlsx")), ".xlsx")
        self.assertEqual(ingest.sniff_suffix(ooxml_zip("pptx")), ".pptx")
        self.assertEqual(ingest.sniff_suffix(plain_zip()), ".zip")

    def test_a_pk_header_with_an_unreadable_directory_is_not_guessed_at(self) -> None:
        # 名字从没声称这是压缩包，所以没有什么可以追究——判不出就是判不出。
        # （被*命名*为 .zip 的坏档案仍然必红，那条声称来自源，见 §V。）
        self.assertIsNone(ingest.sniff_suffix(ingest.ZIP_MAGIC + b"\x00" * 40))

    def test_bytes_that_say_nothing_yield_nothing(self) -> None:
        for data in (b"", "这就是一段中文正文，没有任何魔数。".encode("utf-8"),
                     b"\x00\x01\x02\x03"):
            with self.subTest(data=data[:8]):
                self.assertIsNone(ingest.sniff_suffix(data))

    def test_only_the_head_is_read(self) -> None:
        # 嗅探不做全文启发式：正文里出现 "%PDF" 不算数，否则一份讨论 PDF 的
        # 备忘录会被判成 PDF。
        self.assertIsNone(ingest.sniff_suffix(b"x" * 32 + b"%PDF-1.4"))

    def test_planning_has_no_bytes_and_says_so_instead_of_guessing(self) -> None:
        # 计划期不下载，所以没有字节可读。这时给出的必须是"待嗅探"的占位，
        # 而不是按文件名猜一个格式写进确认卡。
        decision = ingest.decide_format(self.NO_SUFFIX, env={})
        self.assertEqual((decision.expected_status, decision.code, decision.sniffed),
                         ("placeholder", ingest.CODE_UNKNOWN_FORMAT, None))

    def test_a_sniffed_pdf_enters_the_pdf_row_and_the_sniff_is_kept(self) -> None:
        decision = ingest.decide_format(
            self.NO_SUFFIX, env={}, data=latin_pdf(["RT-046 flow memo, no suffix"])
        )
        self.assertEqual(
            (decision.format, decision.handling, decision.expected_status, decision.sniffed),
            ("pdf", "pdf-text", "converted", ".pdf"),
        )

    def test_a_sniffed_docx_enters_the_docx_row_with_its_own_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "md2md"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            decision = ingest.decide_format(
                self.NO_SUFFIX, env={ingest.ENV_DOCX_CONVERTER: str(binary)},
                data=ooxml_zip("docx"),
            )
        self.assertEqual((decision.format, decision.converter, decision.sniffed),
                         ("docx", "docx-host", ".docx"))

    def test_a_sniffed_image_is_a_placeholder_not_a_pretend_document(self) -> None:
        decision = ingest.decide_format(self.NO_SUFFIX, env={}, data=PNG_BYTES)
        self.assertEqual((decision.format, decision.expected_status, decision.sniffed),
                         ("image", "placeholder", ".png"))

    def test_undecidable_bytes_are_unknown_format_not_a_guess(self) -> None:
        decision = ingest.decide_format(self.NO_SUFFIX, env={}, data=b"\x00\x01\x02\x03" * 8)
        self.assertEqual((decision.format, decision.code),
                         ("unknown", ingest.CODE_UNKNOWN_FORMAT))

    def test_a_name_that_carries_a_suffix_is_never_sniffed(self) -> None:
        # 名字说了话就以名字为准。反过来会让"内容以 %PDF 开头的 .md"变成 pdf，
        # 而这类件（转换回执、日志）在库里真实存在。
        decision = ingest.decide_format(
            "a.md", env={}, data="%PDF-1.4 讨论稿\n".encode("utf-8")
        )
        self.assertEqual((decision.handling, decision.sniffed), ("passthrough", None))


# ── format factory v1.1: PDF text, standard library only ────────────────────


class PdfTextExtractionTests(unittest.TestCase):
    """PDF 抽取的判据面：抽得出算数，抽不出必须说得出为什么。"""

    LATIN = ["RT-046 format factory acceptance line", "second line of the flow memo"]
    CJK = ["CMS投前阶段1流程诊断与补丁处理备忘录", "阶段一（S1）流程：立项、初筛、尽调"]

    def test_a_flate_content_stream_comes_back_as_text(self) -> None:
        text, reason = ingest.extract_pdf_text(latin_pdf(self.LATIN))
        self.assertEqual(reason, "pdf-text-ok")
        self.assertEqual(text, "\n".join(self.LATIN))

    def test_an_uncompressed_content_stream_reads_the_same(self) -> None:
        text, reason = ingest.extract_pdf_text(latin_pdf(self.LATIN, compress=False))
        self.assertEqual((text, reason), ("\n".join(self.LATIN), "pdf-text-ok"))

    def test_a_subset_cid_font_is_read_through_its_tounicode_cmap(self) -> None:
        # Word 导出的中文 PDF 一律是 Identity-H 子集字体。没有 CMap 支持，
        # 「支持 PDF」就退化成「每一份中文 PDF 都占位」——也就是没支持。
        for ranges in (False, True):
            with self.subTest(cmap="bfrange" if ranges else "bfchar"):
                text, reason = ingest.extract_pdf_text(cjk_pdf(self.CJK, ranges=ranges))
                self.assertEqual((text, reason), ("\n".join(self.CJK), "pdf-text-ok"))

    def test_a_subset_font_without_a_cmap_is_refused_not_published_as_cids(self) -> None:
        # 一份乱码正文比占位更坏：它进检索、进引文，而且哈希永远是绿的。
        text, reason = ingest.extract_pdf_text(cjk_pdf(self.CJK, tounicode=False))
        self.assertIsNone(text)
        self.assertEqual(reason, "pdf-text-unreliable")

    def test_a_page_with_no_text_operators_is_a_failure_not_an_empty_body(self) -> None:
        text, reason = ingest.extract_pdf_text(latin_pdf([]))
        self.assertIsNone(text)
        self.assertEqual(reason, "pdf-text-empty")

    def test_a_handful_of_characters_is_treated_as_a_failed_conversion(self) -> None:
        text, reason = ingest.extract_pdf_text(latin_pdf(["ok"]))
        self.assertIsNone(text)
        self.assertEqual(reason, "pdf-text-too-short")

    def test_bytes_that_are_not_a_pdf_are_refused_up_front(self) -> None:
        self.assertEqual(ingest.extract_pdf_text(plain_zip())[1], "pdf-not-a-pdf")

    def test_the_cmap_stream_is_not_mistaken_for_page_text(self) -> None:
        # 内容流按 /Contents 结构解析，不是"扫所有 stream"：ToUnicode 流里也
        # 全是 <hex> 形状的字节，扫进来就是一串控制字符，然后整份件被质量门
        # 判成不可信——正文明明是好的。
        data = cjk_pdf(self.CJK)
        streams = ingest._pdf_content_streams(ingest._pdf_objects(data))
        self.assertEqual(len(streams), 1)
        self.assertIn(b"BT", streams[0])
        self.assertNotIn(b"begincmap", streams[0])


# ── routing ─────────────────────────────────────────────────────────────────


class RoutePlacementTests(unittest.TestCase):
    def test_timeline_places_by_month_and_day(self) -> None:
        self.assertEqual(
            ingest.raw_path_for(
                route_mode="timeline", lineage="cwork:2095046023776104449",
                stable_id="2095046023776104449", name="2095046023776104449-八月周报.md",
                group="2026-08-14", date="2026-08-14",
            ),
            "raw/d-2026-08/d-2026-08-14/2095046023776104449-八月周报.md",
        )

    def test_classify_places_under_the_source_directory_name(self) -> None:
        self.assertEqual(
            ingest.raw_path_for(
                route_mode="classify", lineage="docdb:2087",
                stable_id="20875195938", name="20875195938-合同.docx",
                group="玄关合同", date="2026-08-14",
            ),
            "raw/classify/玄关合同/20875195938-合同.md",
        )

    def test_classify_digit_leading_group_gets_a_letter_prefix(self) -> None:
        # DSM 对「1-交付包-…」这类数字起头的目录名回 code=400（2026-09-05
        # 真机实测：2026-06、1-xxx 拒；2026x、2026_06、字母起头全过）。
        self.assertEqual(
            ingest.raw_path_for(
                route_mode="classify", lineage="docdb:2082736573367070722",
                stable_id="2082736573367070722", name="2082736573367070722-综述.md",
                group="1. 交付包_体外模拟N1-N11节点_20260730", date="2026-07-30",
            ),
            "raw/classify/c-1-交付包-体外模拟N1-N11节点-20260730/2082736573367070722-综述.md",
        )

    def test_timeline_without_a_date_uses_a_named_bucket_not_today(self) -> None:
        # 坏情形：无日期件落到"今天"。那样同一件重跑两次会落到两个路径，
        # 幂等判据全绿而 raw 里多出一份孤儿。
        path = ingest.raw_path_for(
            route_mode="timeline", lineage="cwork:2095046023776104449",
            stable_id="2095046023776104449", name="2095046023776104449-无日期.md",
            group="flat", date=None,
        )
        self.assertEqual(path, f"raw/{ingest.UNDATED_BUCKET}/2095046023776104449-无日期.md")

    def test_originals_path_depends_only_on_identity_and_bytes(self) -> None:
        # J1 的前置条件：档案路径不含日期、不含计数器。含了就会
        # "写一次"变成"每次 mtime 漂移写一次"。
        first = ingest.originals_path_for("cwork", "2095046023776104449", "a" * 64, "x.md")
        second = ingest.originals_path_for("cwork", "2095046023776104449", "a" * 64, "x.md")
        self.assertEqual(first, second)
        self.assertEqual(first, "originals/cwork/id-2095046023776104449/" + "a" * 64 + ".md")
        self.assertNotIn("2026", first)


# ── source adapter: cwork-mirror ────────────────────────────────────────────


class CworkMirrorAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mirror"
        write_mirror(self.root, "2026-08/2026-08-14/2095046023776104449-八月周报.md", b"# 8\n")
        write_mirror(self.root, "2026-06/2026-06-02/2095046023776104450-六月周报.md", b"# 6\n")
        write_mirror(self.root, "2026-08/2026-08-14/README.md", b"readme\n")

    def test_scan_returns_identified_items_sorted_by_date(self) -> None:
        items, unidentified = ingest.scan_cwork_mirror(self.root)
        self.assertEqual([item.stable_id for item in items],
                         ["2095046023776104450", "2095046023776104449"])
        self.assertEqual([item.date for item in items], ["2026-06-02", "2026-08-14"])

    def test_files_without_a_stable_id_are_reported_never_dropped(self) -> None:
        # 静默丢件是 §VI 存在的理由：扫描器不认识的东西必须留下痕迹。
        _, unidentified = ingest.scan_cwork_mirror(self.root)
        self.assertEqual(unidentified, ["2026-08/2026-08-14/README.md"])

    def test_since_filters_on_the_derived_date(self) -> None:
        items, _ = ingest.scan_cwork_mirror(self.root, since="2026-07-01")
        self.assertEqual([item.stable_id for item in items], ["2095046023776104449"])

    def test_j5_missing_source_directory_raises_instead_of_returning_empty(self) -> None:
        # 负例：源目录消失时返回空列表。那样"零件可摄取"和"源没了"
        # 长得一模一样，批次会全绿收工。
        with self.assertRaises(ingest.SourceError):
            ingest.scan_cwork_mirror(self.root.parent / "gone")

    def test_platform_system_ledgers_are_never_source_content(self) -> None:
        # 负例（Case 1 实事故 2026-09-05）：镜像 raw/_system/timelines/** 是
        # nightly 管道自己写的回复链事件账，不是 cwork 源内容。扫描器把它
        # 当源扫入，451 篇真报告波变成了 3827 件的计划（多数是 hex 名的
        # 事件 json，部分碰巧以 ≥8 位数字开头撞上稳定 ID 规则）。_system
        # 必须整体排除：items 和 unidentified 都不得含其下任何文件。
        write_mirror(
            self.root,
            "_system/timelines/2093235488570916866/events/94868867166fb7140a0.json",
            b"{}\n",
        )
        write_mirror(
            self.root,
            "_system/timelines/2093235488570916866/manifest.json",
            b"{}\n",
        )
        write_mirror(
            self.root,
            "_system/timelines/2093235488570916866/snapshots/94868867166ab.md",
            b"snapshot\n",
        )
        items, unidentified = ingest.scan_cwork_mirror(self.root)
        # items 与不含 _system 的期望一致（8 位数字前缀的事件 json 不得混入）
        self.assertEqual([item.stable_id for item in items],
                         ["2095046023776104450", "2095046023776104449"])
        # unidentified 只剩真实无 ID 文件，_system 不进清单
        self.assertEqual(unidentified, ["2026-08/2026-08-14/README.md"])


# ── source adapter: docdb ───────────────────────────────────────────────────


class DocdbAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        skill = Path(self.tmp.name) / "cms-docdb"
        (skill / "scripts" / "browse").mkdir(parents=True)
        (skill / "scripts" / "browse" / "browse.py").write_text("", encoding="utf-8")
        self.env = {
            ingest.ENV_DOCDB_SKILL_DIR: str(skill),
            "XG_BIZ_API_KEY": "fake-key-not-a-real-secret",
        }
        self.folders = {
            "100": [
                {"id": "200", "name": "合同", "type": "1"},
                {"fileId": "301", "name": "2026 年度计划.docx", "type": "2",
                 "updateTime": "2026-08-14 09:30:00", "size": "2048"},
            ],
            "200": [
                {"fileId": "302", "name": "供应商合同.pdf", "type": "2",
                 "updateTime": "1754000000000"},
            ],
        }

    def test_scan_walks_folders_and_uses_file_id_as_the_stable_id(self) -> None:
        fake = FakeDocdb(self.folders)
        items, _ = ingest.scan_docdb("100", env=self.env, retries=1,
                                     sleep=lambda _s: None, runner=fake)
        self.assertEqual(sorted(item.stable_id for item in items), ["301", "302"])
        by_id = {item.stable_id: item for item in items}
        self.assertEqual(by_id["301"].date, "2026-08-14")
        self.assertEqual(by_id["301"].group, "100")
        self.assertEqual(by_id["302"].group, "合同")

    def test_j5_adapter_5xx_raises_after_retries_instead_of_yielding_no_items(self) -> None:
        # 负例：把 500 当成"这个目录是空的"。那样源故障会伪装成
        # "没有新件"，批次全绿而整批数据没进来。
        fake = FakeDocdb(self.folders, fail_with="500 Internal Server Error")
        with self.assertRaises(ingest.SourceError) as ctx:
            ingest.scan_docdb("100", env=self.env, retries=2, sleep=lambda _s: None, runner=fake)
        self.assertIn("500", str(ctx.exception))
        self.assertEqual(len(fake.calls), 2, "瞬时错误应当按 retries 重试")

    def test_permanent_error_is_not_retried(self) -> None:
        fake = FakeDocdb(self.folders, fail_with="403 forbidden: no access to this folder")
        with self.assertRaises(ingest.SourceError):
            ingest.scan_docdb("100", env=self.env, retries=3, sleep=lambda _s: None, runner=fake)
        self.assertEqual(len(fake.calls), 1, "永久错误重试只会把清楚的失败拖慢")

    def test_credentials_come_from_the_environment_only(self) -> None:
        with self.assertRaises(ingest.SourceError) as ctx:
            ingest.docdb_env({ingest.ENV_DOCDB_SKILL_DIR: "/tmp"})
        self.assertIn("XG_BIZ_API_KEY", str(ctx.exception))


class CredentialFlagTests(unittest.TestCase):
    def test_a_secret_on_the_command_line_is_refused_before_anything_runs(self) -> None:
        code, payload = run_cli(["plan", "--source", "cwork-mirror", "--root", "/tmp",
                                 "--kb-root", "/tmp", "--token", "s3cret"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])


# ── plan + confirmation cards ───────────────────────────────────────────────


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.kb = self.base / "kb"
        self.kb_code = make_kb(self.kb)
        self.mirror = self.base / "mirror"
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104449-八月周报.md", b"# 8\n")
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104451-附件.rar", b"RAR\n")
        write_mirror(self.mirror, "2026-08/2026-08-14/notes.md", b"stray\n")

    def plan(self, *extra) -> dict:
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.mirror),
             "--kb-root", str(self.kb), *extra]
        )
        self.assertEqual(code, 0, payload)
        return payload

    def test_plan_defaults_route_by_source(self) -> None:
        payload = self.plan()
        self.assertEqual(payload["route"]["mode"], "timeline")
        self.assertEqual(payload["route"]["default_for_source"], "timeline")
        self.assertEqual(payload["kb_code"], self.kb_code)

    def test_docdb_defaults_to_classify(self) -> None:
        self.assertEqual(ingest.DEFAULT_ROUTE["docdb"], "classify")

    def test_route_override_changes_every_proposed_path(self) -> None:
        payload = self.plan("--route", "classify")
        for row in payload["items"]:
            self.assertTrue(row["confirmation"]["proposed_raw_path"].startswith("raw/classify/"))

    def test_each_item_carries_a_confirmation_card(self) -> None:
        payload = self.plan()
        card = next(
            row["confirmation"] for row in payload["items"]
            if row["stable_id"] == "2095046023776104449"
        )
        self.assertEqual(card["lineage_id"], "cwork:2095046023776104449")
        self.assertEqual(
            card["proposed_raw_path"],
            "raw/d-2026-08/d-2026-08-14/2095046023776104449-八月周报.md",
        )
        self.assertEqual(card["format"]["handling"], "passthrough")
        self.assertEqual(sorted(card["editable"]), ["proposed_raw_path", "route_mode"])

    def test_plan_reports_unidentified_files(self) -> None:
        payload = self.plan()
        self.assertEqual(payload["unidentified"], ["2026-08/2026-08-14/notes.md"])

    def test_plan_counts_expected_statuses(self) -> None:
        payload = self.plan()
        self.assertEqual(payload["expected_status_counts"],
                         {"converted": 1, "skipped": 1})

    def test_since_is_validated_before_the_source_is_touched(self) -> None:
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.mirror),
             "--kb-root", str(self.kb), "--since", "2026/08/01"]
        )
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", payload["error"])

    def test_plan_can_be_written_to_a_file_and_read_back(self) -> None:
        out = self.base / "plan.json"
        self.plan("--out", str(out))
        loaded = ingest.load_plan(out)
        self.assertEqual(loaded["schema"], ingest.PLAN_SCHEMA)

    def test_j5_plan_against_a_missing_source_is_red_and_says_so(self) -> None:
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.base / "gone"),
             "--kb-root", str(self.kb)]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_type"], "SourceError")

    def test_plan_refuses_a_kb_root_that_is_not_a_library(self) -> None:
        empty = self.base / "not-a-kb"
        empty.mkdir()
        code, payload = run_cli(
            ["plan", "--source", "cwork-mirror", "--root", str(self.mirror),
             "--kb-root", str(empty)]
        )
        self.assertEqual(code, 2)
        self.assertIn("kb.json", payload["error"])


class PlanValidationTests(unittest.TestCase):
    """一份被编辑过的计划是输入，不是权威——落盘前必须再验一次。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plan.json"

    def write(self, payload: dict) -> Path:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.path

    def base_plan(self, **card) -> dict:
        confirmation = {
            "lineage_id": "cwork:2095046023776104449",
            "route_mode": "timeline",
            "proposed_raw_path": "raw/d-2026-08/d-2026-08-14/2095046023776104449-x.md",
        }
        confirmation.update(card)
        return {
            "schema": ingest.PLAN_SCHEMA,
            "adapter": "cwork-mirror",
            "source": "cwork",
            "kb_root": "/tmp/kb",
            "items": [{"stable_id": "2095046023776104449", "confirmation": confirmation}],
        }

    def test_a_good_plan_loads(self) -> None:
        self.assertEqual(len(ingest.load_plan(self.write(self.base_plan()))["items"]), 1)

    def test_an_edited_card_cannot_place_an_artefact_outside_the_library(self) -> None:
        # 负例：确认卡可改 → 改成 ../../etc/cron.d/x。卡是操作员编辑过的输入，
        # 不能当权威直接落盘。
        with self.assertRaises(Exception):
            ingest.load_plan(self.write(self.base_plan(proposed_raw_path="../../etc/x.md")))

    def test_an_unknown_route_mode_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.load_plan(self.write(self.base_plan(route_mode="magic")))

    def test_a_card_with_a_rev_in_its_key_is_refused(self) -> None:
        with self.assertRaises(ingest.IngestError):
            ingest.load_plan(self.write(self.base_plan(lineage_id="docdb:2087@7")))

    def test_a_foreign_schema_is_refused(self) -> None:
        payload = self.base_plan()
        payload["schema"] = "something.else.v1"
        with self.assertRaises(ingest.IngestError):
            ingest.load_plan(self.write(payload))


# ── run: the pipeline end to end ────────────────────────────────────────────


class CountingBackend:
    """LocalFSBackend with a write counter.

    "零写入" has to be measured, not inferred from an unchanged tree: a run
    that rewrites a file with identical bytes leaves the tree identical and
    is still doing work, still racing another writer, still burning NAS
    round-trips.  The counter is what makes J1 mean what it says.
    """

    name = "counting"

    def __init__(self, root) -> None:
        self.inner = LocalFSBackend(root)
        self.writes = []

    def write(self, path, data):
        self.writes.append(path)
        return self.inner.write(path, data)

    def __getattr__(self, item):
        return getattr(self.inner, item)


class IngestFixture(unittest.TestCase):
    """A real RT-042 library plus a fake cwork mirror."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.kb = self.base / "kb"
        self.kb_code = make_kb(self.kb)
        self.mirror = self.base / "mirror"
        self.backend = CountingBackend(self.kb)
        self.report = write_mirror(
            self.mirror, "2026-08/2026-08-14/2095046023776104449-八月周报.md", b"# 8 \xe6\x9c\x88\n"
        )

    def make_plan(self, *, route=None, env=NO_CONVERTER, has_openpyxl=False, since=None) -> dict:
        return ingest.build_plan(
            adapter="cwork-mirror", root=str(self.mirror), kb_root=str(self.kb),
            route_mode=route, env=env, has_openpyxl=has_openpyxl, since=since,
            generated_at=FIXED_NOW,
        )

    def execute(self, plan=None, *, env=NO_CONVERTER, has_openpyxl=False, runner=None,
                now=FIXED_NOW) -> dict:
        return ingest.execute_plan(
            self.backend, plan if plan is not None else self.make_plan(),
            kb_code=self.kb_code, env=env, has_openpyxl=has_openpyxl,
            runner=runner or (lambda *a, **k: FakeProc(1, "")), retries=1,
            sleep=lambda _s: None, now=now,
        )

    def ingest_one(self, rel: str, body: bytes, *, env=NO_CONVERTER, has_openpyxl=False,
                   runner=None) -> dict:
        """Replace the fixture's markdown report with one item and run."""
        write_mirror(self.mirror, rel, body)
        self.report.unlink()
        return self.execute(
            self.make_plan(env=env, has_openpyxl=has_openpyxl),
            env=env, has_openpyxl=has_openpyxl, runner=runner,
        )

    def read_json_at(self, rel: str) -> dict:
        return json.loads(self.backend.read(rel).decode("utf-8"))

    def provenance(self) -> list:
        chain = self.backend.read(ingest.PROVENANCE_CHAIN_REL).decode("utf-8").splitlines()
        return [json.loads(line) for line in chain]

    def tree(self) -> dict:
        from kb_ledger import scan_tree

        return scan_tree(self.backend.inner)


class RunBasicsTests(IngestFixture):
    def test_a_markdown_item_lands_in_raw_originals_index_state_and_provenance(self) -> None:
        report = self.execute()
        self.assertTrue(report["ok"], report)
        raw = "raw/d-2026-08/d-2026-08-14/2095046023776104449-八月周报.md"
        self.assertEqual(self.backend.read(raw), self.report.read_bytes())

        index = self.read_json_at(ingest.RAW_INDEX_REL)
        entry = index["entries"]["cwork:2095046023776104449"]
        self.assertEqual(entry["path"], raw)
        self.assertEqual(entry["artifact_kind"], "document")
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["rule_version"], ingest.RULE_VERSION)

        self.assertTrue(self.backend.exists(entry["originals"]))
        self.assertTrue(entry["originals"].startswith("originals/cwork/id-2095046023776104449/"))

        state = self.read_json_at(ingest.INGEST_STATE_REL)
        self.assertEqual(state["items"]["cwork:2095046023776104449"]["status"], "converted")
        self.assertEqual(state["counts"]["converted"], 1)

        chain = self.backend.read(ingest.PROVENANCE_CHAIN_REL).decode("utf-8").splitlines()
        self.assertEqual(len(chain), 1)
        self.assertEqual(json.loads(chain[0])["lineage_id"], "cwork:2095046023776104449")

    def test_the_run_keeps_the_rt042_ledgers_true(self) -> None:
        from kb_doctor import verify_raw
        from kb_ledger import verify_manifest

        self.execute()
        self.assertTrue(verify_manifest(self.backend.inner).ok,
                        verify_manifest(self.backend.inner).describe())
        self.assertTrue(verify_raw(self.backend.inner).ok,
                        verify_raw(self.backend.inner).describe())

    def test_run_without_yes_is_a_confirmation_card_and_writes_nothing(self) -> None:
        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(self.make_plan(), ensure_ascii=False), encoding="utf-8")
        before = self.tree()
        code, payload = run_cli(["run", "--plan", str(plan_path)])
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["confirm_required"])
        self.assertEqual(payload["cards"][0]["lineage_id"], "cwork:2095046023776104449")
        self.assertEqual(self.tree(), before, "确认门必须零写入")

    def test_run_with_yes_applies_the_plan_through_the_cli(self) -> None:
        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(self.make_plan(), ensure_ascii=False), encoding="utf-8")
        code, payload = run_cli(["run", "--plan", str(plan_path), "--yes",
                                 "--kb-root", str(self.kb)])
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["counts"]["converted"], 1)

    def test_an_edited_card_moves_the_artefact(self) -> None:
        # 确认卡可改：改了 route_mode 和路径，产物就该落到新位置。
        plan = self.make_plan()
        plan["items"][0]["confirmation"]["route_mode"] = "classify"
        plan["items"][0]["confirmation"]["proposed_raw_path"] = "raw/classify/自定义/x.md"
        self.execute(plan)
        self.assertTrue(self.backend.exists("raw/classify/自定义/x.md"))
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104449"]
        self.assertEqual(entry["path"], "raw/classify/自定义/x.md")


class J1IdempotenceTests(IngestFixture):
    """J1 originals write-once：同源同件跑两遍，第二遍零写入零账变。"""

    def test_j1_second_pass_writes_nothing_and_changes_no_account(self) -> None:
        self.execute()
        before = self.tree()
        self.backend.writes.clear()

        report = self.execute()

        self.assertEqual(self.backend.writes, [], "第二遍不允许有任何一次写入")
        self.assertEqual(self.tree(), before, "第二遍不允许改动任何一个字节")
        self.assertEqual(report["counts"]["unchanged"], 1)
        self.assertEqual(report["counts"]["converted"], 0)

    def test_j1_a_second_pass_after_an_mtime_bump_still_writes_nothing(self) -> None:
        self.execute()
        os.utime(self.report, (0, 0))
        self.backend.writes.clear()
        self.execute()
        self.assertEqual(self.backend.writes, [])

    def test_j1_originals_are_write_once_even_without_the_state_ledger(self) -> None:
        # 上面那条判据走的是状态账的短路，证明不了 write-once 本身：把
        # archive_original 的存在性检查拆掉，它照样全绿（实测）。这条从
        # 状态账丢失的现实故障进——重跑必须重新落 raw，但绝不能把同一份
        # 字节在 originals/ 下再写一遍。
        self.execute()
        archived = [path for path in self.backend.writes if path.startswith("originals/")]
        self.assertEqual(len(archived), 1)

        state = self.read_json_at(ingest.INGEST_STATE_REL)
        state["items"] = {}
        self.backend.write(ingest.INGEST_STATE_REL,
                           json.dumps(state, ensure_ascii=False).encode("utf-8"))
        self.backend.writes.clear()

        report = self.execute()

        self.assertEqual(report["counts"]["converted"], 1, "状态账没了，raw 应当重新落位")
        self.assertEqual(
            [path for path in self.backend.writes if path.startswith("originals/")],
            [],
            "同一份字节不得在 originals/ 下写第二次",
        )

    def test_j1_archive_original_reports_that_it_wrote_nothing_the_second_time(self) -> None:
        payload = b"# same bytes\n"
        first = ingest.archive_original(
            self.backend, source="cwork", stable_id="2095046023776104449",
            name="x.md", data=payload,
        )
        self.backend.writes.clear()
        second = ingest.archive_original(
            self.backend, source="cwork", stable_id="2095046023776104449",
            name="x.md", data=payload,
        )
        self.assertEqual(first[0], second[0], "同 lineage 同 sha 必须落同一条路径")
        self.assertTrue(first[2])
        self.assertFalse(second[2])
        self.assertEqual(self.backend.writes, [])

    def test_j1_an_archive_whose_bytes_disagree_with_its_address_is_red(self) -> None:
        # 内容寻址的路径与内容对不上 = 存档层被改写过，不是新版本。
        payload = b"# same bytes\n"
        path, _digest, _wrote = ingest.archive_original(
            self.backend, source="cwork", stable_id="2095046023776104449",
            name="x.md", data=payload,
        )
        self.backend.write(path, b"tampered\n")
        with self.assertRaises(ingest.ItemFailure) as ctx:
            ingest.archive_original(
                self.backend, source="cwork", stable_id="2095046023776104449",
                name="x.md", data=payload,
            )
        self.assertEqual(ctx.exception.reason, "originals-sha-mismatch")

    def test_j1_a_changed_source_does_write(self) -> None:
        # 破坏实验的对照面：幂等不是"永远不写"。内容真的变了必须落盘，
        # 否则上面那条判据用一个"什么都不做"的实现也能全绿。
        self.execute()
        self.report.write_bytes(b"# 8 \xe6\x9c\x88 \xe4\xbf\xae\xe8\xae\xa2\n")
        self.backend.writes.clear()
        report = self.execute()
        self.assertTrue(self.backend.writes)
        self.assertEqual(report["counts"]["converted"], 1)


class J6LineageVersionTests(IngestFixture):
    """J6 lineage 寻址：同 ID 第二版 → versions+1、supersedes 链正确。"""

    def second_version(self) -> dict:
        self.execute()
        self.report.write_bytes(b"# 8 \xe6\x9c\x88 v2\n")
        self.execute()
        return self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104449"]

    def test_j6_a_new_version_extends_the_same_entry(self) -> None:
        entry = self.second_version()
        index = self.read_json_at(ingest.RAW_INDEX_REL)
        self.assertEqual(len(index["entries"]), 1, "第二版不得另开条目")
        self.assertEqual(entry["version"], 2)
        self.assertEqual(len(entry["versions"]), 2)

    def test_j6_the_supersedes_chain_points_backwards_correctly(self) -> None:
        entry = self.second_version()
        self.assertIsNone(entry["versions"][0]["supersedes"])
        self.assertEqual(entry["versions"][1]["supersedes"], 1)
        self.assertNotEqual(entry["versions"][0]["origin_sha256"],
                            entry["versions"][1]["origin_sha256"])

    def test_j6_timeline_mode_keeps_the_first_version_on_disk(self) -> None:
        # raw 只增不改：timeline 库的第二版落在旁边，不覆盖第一版。
        entry = self.second_version()
        self.assertTrue(self.backend.exists(entry["versions"][0]["path"]))
        self.assertTrue(entry["path"].endswith(".v2.md"))
        self.assertNotEqual(entry["versions"][0]["path"], entry["path"])

    def test_j6_classify_mode_updates_the_live_document_in_place(self) -> None:
        # 活文档模型（§II）：classify 库当前文件就是文档本体，链记住旧版。
        plan = self.make_plan(route="classify")
        self.execute(plan)
        self.report.write_bytes(b"# v2\n")
        self.execute(self.make_plan(route="classify"))
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104449"]
        self.assertEqual(entry["version"], 2)
        self.assertEqual(entry["versions"][0]["path"], entry["path"])
        self.assertEqual(self.backend.read(entry["path"]), b"# v2\n")

    def test_j6_provenance_keeps_the_earlier_lines_verbatim(self) -> None:
        # append-only：第二批不得重写第一批已经写下的行。
        self.execute()
        first = self.backend.read(ingest.PROVENANCE_CHAIN_REL)
        self.report.write_bytes(b"# v2\n")
        self.execute()
        second = self.backend.read(ingest.PROVENANCE_CHAIN_REL)
        self.assertTrue(second.startswith(first))
        self.assertEqual(len(second.splitlines()), 2)


class J3IndexAtomicityTests(IngestFixture):
    """J3 raw-index 原子写：写中断后旧 index 完整可用。"""

    def interrupt_index_writes(self) -> None:
        import kb_storage

        real = kb_storage.os.replace

        def flaky(src, dst):
            if str(dst).endswith(ingest.RAW_INDEX_REL):
                raise OSError("模拟发布 raw-index 时进程被打断")
            return real(src, dst)

        kb_storage.os.replace = flaky
        self.addCleanup(setattr, kb_storage.os, "replace", real)

    def test_j3_an_interrupted_publish_leaves_the_old_index_byte_identical(self) -> None:
        self.execute()
        before = self.backend.read(ingest.RAW_INDEX_REL)
        self.assertIn("cwork:2095046023776104449", json.loads(before.decode("utf-8"))["entries"])

        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104460-新件.md", b"# new\n")
        self.interrupt_index_writes()
        with self.assertRaises(OSError):
            self.execute()

        after = self.backend.read(ingest.RAW_INDEX_REL)
        self.assertEqual(after, before, "旧 index 必须逐字节完整")
        self.assertIn("cwork:2095046023776104449", json.loads(after.decode("utf-8"))["entries"])

    def test_j3_the_previous_generation_copy_holds_the_same_old_index(self) -> None:
        self.execute()
        before = self.backend.read(ingest.RAW_INDEX_REL)
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104460-新件.md", b"# new\n")
        self.interrupt_index_writes()
        with self.assertRaises(OSError):
            self.execute()
        self.assertEqual(self.backend.read(ingest.RAW_INDEX_PREV_REL), before)

    def test_j3_the_cli_reports_the_interruption_instead_of_exiting_green(self) -> None:
        self.execute()
        plan_path = self.base / "plan.json"
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104460-新件.md", b"# new\n")
        plan_path.write_text(json.dumps(self.make_plan(), ensure_ascii=False), encoding="utf-8")
        self.interrupt_index_writes()
        code, payload = run_cli(["run", "--plan", str(plan_path), "--yes",
                                 "--kb-root", str(self.kb)])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])


class J4ResumeTests(IngestFixture):
    """J4 状态账与断点续跑：failed 件重跑只处理未完成。"""

    def setUp(self) -> None:
        super().setUp()
        self.second = write_mirror(
            self.mirror, "2026-08/2026-08-15/2095046023776104450-十五日报.md", b"# 15\n"
        )

    def first_pass_with_one_failure(self) -> dict:
        plan = self.make_plan()
        self.second.unlink()  # 源件在 plan 之后消失
        return self.execute(plan)

    def test_j4_a_failed_item_is_recorded_with_a_reason(self) -> None:
        report = self.first_pass_with_one_failure()
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], ["cwork:2095046023776104450"])
        row = self.read_json_at(ingest.INGEST_STATE_REL)["items"]["cwork:2095046023776104450"]
        self.assertEqual(row["status"], "failed")
        self.assertIn("SourceError", row["reason"])

    def test_j4_the_rerun_touches_only_the_unfinished_item(self) -> None:
        self.first_pass_with_one_failure()
        done_before = self.read_json_at(ingest.INGEST_STATE_REL)["items"][
            "cwork:2095046023776104449"
        ]
        write_mirror(self.mirror, "2026-08/2026-08-15/2095046023776104450-十五日报.md", b"# 15\n")
        self.backend.writes.clear()

        report = self.execute()

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["unchanged"], 1, "已完成件不该被重新处理")
        self.assertEqual(report["counts"]["converted"], 1)
        done_after = self.read_json_at(ingest.INGEST_STATE_REL)["items"][
            "cwork:2095046023776104449"
        ]
        self.assertEqual(done_after["attempts"], done_before["attempts"],
                         "已完成件不该再记一次尝试")
        self.assertFalse(
            [path for path in self.backend.writes if "2095046023776104449" in path],
            "已完成件不该产生任何写入",
        )

    def test_j4_a_batch_where_everything_failed_still_leaves_a_true_manifest(self) -> None:
        # 全批失败也写了状态账。root-manifest 不跟着重签，
        # kb_doctor verify --manifest 会红在一件本来就记下来的事故上。
        from kb_ledger import verify_manifest

        plan = self.make_plan()
        self.report.unlink()
        self.second.unlink()
        report = self.execute(plan)
        self.assertEqual(report["counts"]["failed"], 2)
        self.assertTrue(verify_manifest(self.backend.inner).ok,
                        verify_manifest(self.backend.inner).describe())

    def test_j4_a_failed_item_does_not_stop_the_items_after_it(self) -> None:
        # 负例：一件失败就中止整批。那样一个坏件会把当晚剩下的全部拖住，
        # 而状态账会显示它们"从没来过"。
        third = write_mirror(
            self.mirror, "2026-08/2026-08-16/2095046023776104451-十六日报.md", b"# 16\n"
        )
        plan = self.make_plan()
        self.second.unlink()
        report = self.execute(plan)
        statuses = {row["lineage_id"]: row["status"] for row in report["results"]}
        self.assertEqual(statuses["cwork:2095046023776104450"], "failed")
        self.assertEqual(statuses["cwork:2095046023776104451"], "converted")
        self.assertTrue(third.exists())


class FormatFactoryExecutionTests(IngestFixture):
    """格式工厂 v1 的产出面：每种形态落什么、记什么状态。"""

    def test_docx_converts_through_the_host_binary(self) -> None:
        converter = self.base / "md2md"
        converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        converter.chmod(0o755)
        env = {ingest.ENV_DOCX_CONVERTER: str(converter)}
        runner = lambda *a, **k: FakeProc(0, "# 转换出来的正文\n\n足够长的一段内容。\n")
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104470-合同.docx", b"PK\x03\x04docx",
            env=env, runner=runner,
        )
        self.assertEqual(report["counts"]["converted"], 1)
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104470"]
        self.assertEqual(entry["artifact_kind"], "document")
        self.assertIn("转换出来的正文", self.backend.read(entry["path"]).decode("utf-8"))

    def test_docx_falls_back_to_a_placeholder_without_the_host_binary(self) -> None:
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104470-合同.docx", b"PK\x03\x04docx"
        )
        self.assertEqual(report["counts"]["placeholder"], 1)
        row = self.read_json_at(ingest.INGEST_STATE_REL)["items"]["cwork:2095046023776104470"]
        self.assertEqual(row["status"], "placeholder")

    def test_an_empty_docx_conversion_is_a_placeholder_not_a_green_hash(self) -> None:
        # 质量门：转换"成功"但正文是空的，哈希照样对得上——不设门就等于
        # 把空文档当合格产物入库。
        converter = self.base / "md2md"
        converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        converter.chmod(0o755)
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104470-合同.docx", b"PK\x03\x04docx",
            env={ingest.ENV_DOCX_CONVERTER: str(converter)},
            runner=lambda *a, **k: FakeProc(0, "   "),
        )
        self.assertEqual(report["counts"]["placeholder"], 1)
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104470"]
        self.assertEqual(entry["placeholder_reason"], "docx-convert-empty")

    def test_xlsx_writes_one_csv_per_sheet(self) -> None:
        original = ingest.convert_xlsx
        ingest.convert_xlsx = lambda data: {"汇总": "a,b\n1,2\n", "明细": "c\n3\n"}
        self.addCleanup(setattr, ingest, "convert_xlsx", original)
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104471-台账.xlsx", b"PK\x03\x04xlsx",
            has_openpyxl=True,
        )
        self.assertEqual(report["counts"]["converted"], 1)
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104471"]
        csvs = [path for path in entry["artifacts"] if path.endswith(".csv")]
        self.assertEqual(len(csvs), 2, entry["artifacts"])
        self.assertEqual(self.backend.read(csvs[0]).decode("utf-8"), "c\n3\n")

    def test_xlsx_without_any_converter_degrades_to_a_placeholder(self) -> None:
        # 「本机没有 xlsx 转换器」必须由测试来规定，不能由跑测试的那台机器
        # 决定：装了 markitdown 的开发机会正确地降到 module-text。
        with without_modules("openpyxl", "markitdown"):
            report = self.ingest_one(
                "2026-08/2026-08-14/2095046023776104471-台账.xlsx", b"PK\x03\x04xlsx",
                has_openpyxl=False,
            )
        self.assertEqual(report["counts"]["placeholder"], 1)
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104471"]
        self.assertEqual(entry["placeholder_reason"], "converter-missing:xlsx")

    def test_zip_gets_a_placeholder_carrying_its_central_directory(self) -> None:
        buffer = io.BytesIO()
        with __import__("zipfile").ZipFile(buffer, "w") as archive:
            archive.writestr("合同/正本.pdf", b"x")
            archive.writestr("合同/附件.xlsx", b"y")
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104472-打包.zip", buffer.getvalue()
        )
        self.assertEqual(report["counts"]["placeholder"], 1)
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104472"]
        body = self.backend.read(entry["path"]).decode("utf-8")
        self.assertIn("合同/正本.pdf", body)
        self.assertIn("合同/附件.xlsx", body)

    def test_an_unreadable_zip_is_red_not_a_placeholder(self) -> None:
        # §V：清单读取失败必红。降级会把坏档案和好档案摆在一起、状态一样绿。
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104472-打包.zip", b"not a zip at all"
        )
        self.assertFalse(report["ok"])
        row = self.read_json_at(ingest.INGEST_STATE_REL)["items"]["cwork:2095046023776104472"]
        self.assertEqual(row["status"], "failed")
        self.assertIn("zip-directory-unreadable", row["reason"])

    def test_rar_is_skipped_but_its_bytes_are_still_archived(self) -> None:
        report = self.ingest_one(
            "2026-08/2026-08-14/2095046023776104473-附件.rar", b"Rar!\x1a\x07\x00"
        )
        self.assertEqual(report["counts"]["skipped"], 1)
        row = self.read_json_at(ingest.INGEST_STATE_REL)["items"]["cwork:2095046023776104473"]
        self.assertEqual(row["status"], "skipped")
        self.assertTrue(self.backend.exists(row["originals"]), "跳过的是转换，不是存档")
        self.assertNotIn("cwork:2095046023776104473",
                         self.read_json_at(ingest.RAW_INDEX_REL)["entries"])

    def test_a_placeholder_body_carries_no_wall_clock(self) -> None:
        # 占位件带时间戳会让 J1 的整树比较每次都不一样——判据变噪声。
        first = ingest.placeholder_body(
            lineage="cwork:1", name="a.pptx", origin_sha="f" * 64, origin_size=3,
            reason="pptx-not-converted-in-v1",
        )
        second = ingest.placeholder_body(
            lineage="cwork:1", name="a.pptx", origin_sha="f" * 64, origin_size=3,
            reason="pptx-not-converted-in-v1",
        )
        self.assertEqual(first, second)


class FormatFactoryV11ExecutionTests(IngestFixture):
    """RT-046 的产出面：嗅探件、可选转换器、以及「转出 0 字」的下场。"""

    NO_SUFFIX_REL = "2026-08/2026-08-14/2095046023776104474-无后缀"
    NO_SUFFIX_KEY = "cwork:2095046023776104474"
    FLOW = ["CMS投前阶段1流程诊断与补丁处理备忘录", "阶段一（S1）流程：立项、初筛、尽调"]

    def entry(self, key: str) -> dict:
        return self.read_json_at(ingest.RAW_INDEX_REL)["entries"][key]

    def test_a_no_suffix_pdf_is_sniffed_converted_and_readable(self) -> None:
        # 投前库三份关键流程文档就是这个形状：无后缀、PDF、中文子集字体。
        # v1 把它们判成 unknown 占位，正文因此一个字都问不出来。
        report = self.ingest_one(self.NO_SUFFIX_REL, cjk_pdf(self.FLOW))
        self.assertEqual(report["counts"]["converted"], 1, report)
        entry = self.entry(self.NO_SUFFIX_KEY)
        self.assertEqual(entry["artifact_kind"], "document")
        body = self.backend.read(entry["path"]).decode("utf-8")
        self.assertIn("阶段一", body)
        record = self.provenance()[-1]
        self.assertEqual(record["converter"]["name"], "pdf-text")
        # 嗅探结果进回执：同样的字节下次必须得到同样的判决，而"上次为什么
        # 判成 pdf"只有这一行能回答。
        self.assertEqual(record["conversion"]["sniffed"], ".pdf")
        self.assertTrue(record["conversion"]["ok"])

    def test_a_no_suffix_file_whose_bytes_say_nothing_is_a_recorded_unknown(self) -> None:
        report = self.ingest_one(self.NO_SUFFIX_REL, b"\x00\x01\x02\x03" * 16)
        self.assertEqual(report["counts"]["placeholder"], 1, report)
        self.assertEqual(self.entry(self.NO_SUFFIX_KEY)["placeholder_reason"],
                         ingest.CODE_UNKNOWN_FORMAT)
        record = self.provenance()[-1]
        self.assertFalse(record["conversion"]["ok"])
        self.assertIsNone(record["conversion"]["sniffed"], "判不出就是没嗅出后缀")

    def test_a_sniffed_pdf_that_holds_no_text_says_so_instead_of_landing_empty(self) -> None:
        # 扫描件的正常下场。占位理由必须精确到"抽不出文本"，否则日后接
        # OCR 时没人知道该重跑哪一批。
        self.ingest_one(self.NO_SUFFIX_REL, latin_pdf([]))
        self.assertEqual(self.entry(self.NO_SUFFIX_KEY)["placeholder_reason"], "pdf-text-empty")
        self.assertFalse(self.provenance()[-1]["conversion"]["ok"])

    @unittest.skipIf(ingest.module_available("anydoc"), "本机装了 anydoc，替身不成立")
    def test_an_optional_module_converts_and_names_itself_in_the_receipt(self) -> None:
        fake = FakeOptionalConverter("# 无后缀流程文档\n\n阶段一：立项、初筛、尽调。\n")
        with stub_module("anydoc", to_markdown=fake.to_markdown, __version__=ingest.ANYDOC_PIN):
            report = self.ingest_one(self.NO_SUFFIX_REL, ooxml_zip("docx"))
        self.assertEqual(report["counts"]["converted"], 1, report)
        record = self.provenance()[-1]
        self.assertEqual(record["converter"], {"name": "anydoc", "version": ingest.ANYDOC_PIN})
        self.assertEqual(record["conversion"]["sniffed"], ".docx")
        # 两个转换器都按后缀选解析器：递一个没后缀的临时文件过去，嗅探出来的
        # docx 会被当纯文本处理，产物看不出错、账上也看不出错。
        self.assertTrue(fake.paths and fake.paths[0].endswith(".docx"), fake.paths)

    def test_a_module_conversion_that_returns_nothing_is_a_failure_in_the_receipt(self) -> None:
        # 反空转：转出 0 字按失败处理（蓝本 DI-006 的教训——转"成功"但 0 字
        # 且静默）。件仍然落地，但落的是说得出原因的占位。
        fake = FakeOptionalConverter("   \n  ")
        with stub_module("anydoc", to_markdown=fake.to_markdown):
            report = self.ingest_one(self.NO_SUFFIX_REL, ooxml_zip("docx"))
        self.assertEqual(report["counts"]["placeholder"], 1, report)
        entry = self.entry(self.NO_SUFFIX_KEY)
        self.assertEqual(entry["placeholder_reason"], "anydoc-convert-empty")
        self.assertGreater(entry["size"], 0, "占位件本身不许是空正文")
        record = self.provenance()[-1]
        self.assertEqual(record["conversion"], {
            "ok": False, "reason": "anydoc-convert-empty",
            "format": "docx", "handling": "module-text", "sniffed": ".docx",
        })

    def test_a_missing_converter_lands_as_a_rerun_candidate_not_as_silence(self) -> None:
        with without_modules("anydoc"):
            report = self.ingest_one(
                "2026-08/2026-08-14/2095046023776104470-合同.docx", ooxml_zip("docx")
            )
        self.assertEqual(report["counts"]["placeholder"], 1, report)
        entry = self.entry("cwork:2095046023776104470")
        self.assertEqual(entry["placeholder_reason"], f"{ingest.CODE_CONVERTER_MISSING}:docx")
        row = self.read_json_at(ingest.INGEST_STATE_REL)["items"]["cwork:2095046023776104470"]
        self.assertEqual(row["status"], "placeholder")
        # 状态账也要说得出理由：装上转换器那天，重跑名单是从这里查出来的。
        self.assertEqual(row["reason"], f"{ingest.CODE_CONVERTER_MISSING}:docx")

    def test_an_empty_source_never_becomes_a_zero_byte_raw_body(self) -> None:
        # 直通面的反空转。空正文照样进索引、照样对得上哈希，只是永远问不出
        # 东西——它在账上和一份好文档长得一模一样。
        report = self.ingest_one("2026-08/2026-08-14/2095046023776104475-空件.md", b"\n  \n")
        self.assertEqual(report["counts"]["placeholder"], 1, report)
        entry = self.entry("cwork:2095046023776104475")
        self.assertEqual(entry["placeholder_reason"], "passthrough-empty")


class ReceiptChainTests(IngestFixture):
    """RT-046 回执三链：converter{name,version} + 源 sha256 + 产物 sha256。

    三链的用处只有一个——回答「现在库里的正文，是哪个转换器的哪个 build 造
    的，从哪份字节造的」。所以两个 sha 必须能独立复算，而不是账里互相抄。
    """

    def test_a_converted_item_carries_all_three_links(self) -> None:
        self.execute()
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104449"]
        record = self.provenance()[-1]
        self.assertEqual(record["converter"],
                         {"name": "passthrough", "version": ingest.BUILTIN_CONVERTER_VERSION})
        self.assertEqual(record["origin_sha256"],
                         ingest.sha256_bytes(self.backend.read(entry["originals"])))
        self.assertEqual(record["artifact_sha256"],
                         ingest.sha256_bytes(self.backend.read(entry["path"])))
        self.assertTrue(record["conversion"]["ok"])

    def test_the_receipt_separates_the_name_from_the_build(self) -> None:
        # 一个把两者混在一起的字符串（docx:/opt/homebrew/bin/md2md）回答不了
        # 「哪些正文是这一版转换器造的」，那正是换版时唯一要问的问题。
        converter = self.base / "md2md"
        converter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        converter.chmod(0o755)
        self.ingest_one(
            "2026-08/2026-08-14/2095046023776104470-合同.docx", ooxml_zip("docx"),
            env={ingest.ENV_DOCX_CONVERTER: str(converter)},
            runner=lambda *a, **k: FakeProc(0, "# 合同正文\n\n足够长的一段内容。\n"),
        )
        record = self.provenance()[-1]
        self.assertEqual(record["converter"],
                         {"name": "docx-host", "version": ingest.UNKNOWN_VERSION})
        self.assertEqual(record["converter_label"], f"docx:{converter}")

    def test_a_skipped_item_still_gets_a_line_of_its_own(self) -> None:
        self.ingest_one("2026-08/2026-08-14/2095046023776104473-附件.rar", b"Rar!\x1a\x07\x00")
        record = self.provenance()[-1]
        self.assertEqual(record["event"], "skipped")
        self.assertEqual(record["converter"]["name"], "skip")
        self.assertEqual(record["derived"], [])


# ── status + reconcile ──────────────────────────────────────────────────────


class StatusTests(IngestFixture):
    def test_status_summarises_the_state_ledger(self) -> None:
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104473-附件.rar", b"Rar!\n")
        self.execute()
        code, payload = run_cli(["status", "--kb-root", str(self.kb)])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"]["converted"], 1)
        self.assertEqual(payload["counts"]["skipped"], 1)
        self.assertEqual(payload["kb_code"], self.kb_code)
        self.assertEqual(payload["last_batch"]["item_count"], 2)

    def test_status_shows_failures_with_their_reasons(self) -> None:
        second = write_mirror(
            self.mirror, "2026-08/2026-08-15/2095046023776104450-十五日报.md", b"# 15\n"
        )
        plan = self.make_plan()
        second.unlink()
        self.execute(plan)
        code, payload = run_cli(["status", "--kb-root", str(self.kb)])
        self.assertEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed"][0]["lineage_id"], "cwork:2095046023776104450")
        self.assertIn("SourceError", payload["failed"][0]["reason"])

    def test_status_on_a_fresh_library_is_empty_not_an_error(self) -> None:
        code, payload = run_cli(["status", "--kb-root", str(self.kb)])
        self.assertEqual(code, 0)
        self.assertEqual(payload["item_count"], 0)


class J2CoverageReconcileTests(IngestFixture):
    """J2 覆盖率对账：手工删掉一个 raw 产物 → reconcile 必红。"""

    def reconcile(self) -> tuple:
        return run_cli(["reconcile", "--kb-root", str(self.kb)])

    def test_reconcile_is_green_right_after_a_clean_run(self) -> None:
        # 对照面：判据必须先能绿，否则"必红"没有意义。
        write_mirror(self.mirror, "2026-08/2026-08-14/2095046023776104473-附件.rar", b"Rar!\n")
        self.execute()
        code, payload = self.reconcile()
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checked"]["index_entries"], 1)
        self.assertEqual(payload["checked"]["originals_files"], 2, "跳过件的原件也要在档")

    def test_j2_a_deleted_raw_artefact_makes_reconcile_red(self) -> None:
        self.execute()
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104449"]
        (self.kb / entry["path"]).unlink()

        code, payload = self.reconcile()

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["missing_raw"],
                         [{"lineage_id": "cwork:2095046023776104449", "path": entry["path"]}])

    def test_j2_a_hand_edited_raw_file_is_reported_as_edited_not_as_lost(self) -> None:
        # §IV 红线：raw 可移动、可重命名，不可编辑内容。报错要说对是哪一种，
        # 否则运维会按"文件丢了"去重跑，把手工修改覆盖掉。
        self.execute()
        entry = self.read_json_at(ingest.RAW_INDEX_REL)["entries"]["cwork:2095046023776104449"]
        (self.kb / entry["path"]).write_bytes("# 有人手工改了这里\n".encode("utf-8"))

        code, payload = self.reconcile()

        self.assertEqual(code, 1)
        self.assertEqual(payload["missing_raw"], [])
        self.assertEqual(payload["raw_modified_by_hand"][0]["path"], entry["path"])

    def test_j2_an_originals_file_no_account_explains_is_red(self) -> None:
        # §VI 的静默丢件方向：originals 有、index 无。
        self.execute()
        self.backend.write("originals/cwork/id-999/" + "e" * 64 + ".md", b"orphan\n")
        code, payload = self.reconcile()
        self.assertEqual(code, 1)
        self.assertEqual(len(payload["orphan_originals"]), 1)

    def test_j2_a_raw_file_no_index_entry_references_is_red(self) -> None:
        # 反方向：批次写完 raw 就断电，账没落。三账各自自洽，件却对不上。
        self.execute()
        self.backend.write("raw/d-2026-08/d-2026-08-14/来路不明.md", b"x\n")
        code, payload = self.reconcile()
        self.assertEqual(code, 1)
        self.assertEqual(payload["orphan_raw"], ["raw/d-2026-08/d-2026-08-14/来路不明.md"])

    def test_j2_a_state_row_with_no_index_entry_is_red(self) -> None:
        self.execute()
        index = self.read_json_at(ingest.RAW_INDEX_REL)
        entry = index["entries"].pop("cwork:2095046023776104449")
        self.backend.write(ingest.RAW_INDEX_REL,
                           json.dumps(index, ensure_ascii=False).encode("utf-8"))
        code, payload = self.reconcile()
        self.assertEqual(code, 1)
        self.assertEqual(payload["missing_index"],
                         [{"lineage_id": "cwork:2095046023776104449", "status": "converted"}])
        self.assertTrue(payload["orphan_raw"], "没人认领的 raw 产物同时要报出来")
        self.assertIn(entry["path"], payload["orphan_raw"])

    def test_j2_a_failed_item_keeps_reconcile_red_until_it_is_re_run(self) -> None:
        second = write_mirror(
            self.mirror, "2026-08/2026-08-15/2095046023776104450-十五日报.md", b"# 15\n"
        )
        plan = self.make_plan()
        second.unlink()
        self.execute(plan)
        code, payload = self.reconcile()
        self.assertEqual(code, 1)
        self.assertEqual(payload["failed_items"][0]["lineage_id"], "cwork:2095046023776104450")

        write_mirror(self.mirror, "2026-08/2026-08-15/2095046023776104450-十五日报.md", b"# 15\n")
        self.execute()
        code, payload = self.reconcile()
        self.assertEqual(code, 0, payload)

    def test_unidentified_files_are_listed_but_do_not_fake_a_shortfall(self) -> None:
        write_mirror(self.mirror, "2026-08/2026-08-14/notes.md", b"stray\n")
        self.execute()
        code, payload = self.reconcile()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["unidentified"], ["2026-08/2026-08-14/notes.md"])


# ── J5 at batch level, through the docdb adapter ────────────────────────────


class J5SourceFaultTests(unittest.TestCase):
    """J5 源故障必红：适配器 5xx → 批次红，已完成件保留。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.kb = self.base / "kb"
        self.kb_code = make_kb(self.kb, sources=("docdb",))
        self.backend = CountingBackend(self.kb)
        skill = self.base / "cms-docdb"
        for part in ("browse", "query"):
            (skill / "scripts" / part).mkdir(parents=True)
        self.env = {
            ingest.ENV_DOCDB_SKILL_DIR: str(skill),
            "XG_BIZ_API_KEY": "fake-key-not-a-real-secret",
        }
        self.folders = {
            "100": [
                {"fileId": "301", "name": "301-年度计划.md", "type": "2",
                 "updateTime": "2026-08-14 09:30:00"},
                {"fileId": "302", "name": "302-季度总结.md", "type": "2",
                 "updateTime": "2026-08-15 09:30:00"},
            ]
        }

    def plan_and_run(self, fake) -> dict:
        plan = ingest.build_plan(
            adapter="docdb", root="100", kb_root=str(self.kb), env=self.env,
            has_openpyxl=False, generated_at=FIXED_NOW, retries=1,
            sleep=lambda _s: None, runner=FakeDocdb(self.folders),
        )
        return ingest.execute_plan(
            self.backend, plan, kb_code=self.kb_code, env=self.env, runner=fake,
            retries=2, sleep=lambda _s: None, now=FIXED_NOW, has_openpyxl=False,
        )

    def test_j5_a_5xx_on_one_item_fails_the_batch_and_keeps_the_others(self) -> None:
        fake = FakeDocdb(self.folders, blobs={"301": b"# 301\n"}, fail_ids={"302"})
        report = self.plan_and_run(fake)

        self.assertFalse(report["ok"], "源故障不许被吞成绿灯")
        self.assertEqual(report["failed"], ["docdb:302"])
        self.assertEqual(report["counts"]["converted"], 1)

        index = json.loads(self.backend.read(ingest.RAW_INDEX_REL).decode("utf-8"))
        self.assertIn("docdb:301", index["entries"], "已完成件必须保留")
        self.assertTrue(self.backend.exists(index["entries"]["docdb:301"]["path"]))

        state = json.loads(self.backend.read(ingest.INGEST_STATE_REL).decode("utf-8"))
        self.assertEqual(state["items"]["docdb:302"]["status"], "failed")
        self.assertIn("503", state["items"]["docdb:302"]["reason"])

    def test_j5_docdb_items_default_to_the_classify_tree(self) -> None:
        fake = FakeDocdb(self.folders, blobs={"301": b"# 301\n", "302": b"# 302\n"})
        report = self.plan_and_run(fake)
        self.assertTrue(report["ok"], report)
        index = json.loads(self.backend.read(ingest.RAW_INDEX_REL).decode("utf-8"))
        for entry in index["entries"].values():
            self.assertTrue(entry["path"].startswith("raw/classify/"), entry["path"])

    def test_j5_the_next_run_finishes_the_item_that_failed(self) -> None:
        self.plan_and_run(FakeDocdb(self.folders, blobs={"301": b"# 301\n"}, fail_ids={"302"}))
        self.backend.writes.clear()
        report = self.plan_and_run(
            FakeDocdb(self.folders, blobs={"301": b"# 301\n", "302": b"# 302\n"})
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["unchanged"], 1)
        self.assertEqual(report["counts"]["converted"], 1)


# ── DocDB 在线文档：download 给壳，正文在另一个接口 ─────────────────────────


DOCDB_SHELL = (
    b"<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head><meta charset=\"utf-8\">"
    b"<title>\xe5\x9c\xa8\xe7\xba\xbf\xe6\x96\x87\xe6\xa1\xa3</title>"
    b"<script src=\"/static/viewer.js\"></script></head>\n"
    b"<body><div id=\"app\"></div></body>\n</html>\n"
)


class DocdbShellDetectionTests(unittest.TestCase):
    """壳检测的两个条件必须**同时**成立，否则真 HTML 附件会被改写。"""

    def test_an_html_body_on_a_suffixless_name_is_a_shell(self) -> None:
        self.assertTrue(ingest.looks_like_docdb_shell(DOCDB_SHELL, "2087523108876566530-流程"))

    def test_the_doctype_may_be_preceded_by_whitespace_and_any_case(self) -> None:
        self.assertTrue(ingest.looks_like_docdb_shell(b"\n  <HTML><body>x", "计划"))

    def test_a_real_html_attachment_is_never_rewritten(self) -> None:
        # 这条是壳检测里"且文件名不以 .html/.htm 结尾"那半句的全部理由：
        # 一份真 HTML 附件的开头和查看器壳一模一样，只有名字分得开它们。
        for name in ("周报.html", "index.HTM"):
            self.assertFalse(ingest.looks_like_docdb_shell(DOCDB_SHELL, name), name)

    def test_bytes_that_are_not_html_are_not_a_shell(self) -> None:
        self.assertFalse(ingest.looks_like_docdb_shell(b"# \xe6\xa0\x87\xe9\xa2\x98\n", "计划"))
        self.assertFalse(ingest.looks_like_docdb_shell(b"", "计划"))


class DocdbFullContentFetchTests(unittest.TestCase):
    """实测证据：三个不同 fileId 的 download-file 返回同一份 10,557B 查看器壳
    （无正文），而 get-full-content 返回 15,920B / 28,110B / 513B 的真身。
    所以"下载成功"和"拿到文档"是两件事，回执必须分得开。"""

    NO_SUFFIX = "2087523108876566530-无后缀流程文档"
    BODY = "# 无后缀流程文档\n\n阶段一：立项、初筛、尽调。\n"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.kb = self.base / "kb"
        self.kb_code = make_kb(self.kb, sources=("docdb",))
        self.backend = CountingBackend(self.kb)
        skill = self.base / "cms-docdb"
        for part in ("browse", "query"):
            (skill / "scripts" / part).mkdir(parents=True)
        self.env = {
            ingest.ENV_DOCDB_SKILL_DIR: str(skill),
            "XG_BIZ_API_KEY": "fake-key-not-a-real-secret",
        }
        self.folders = {
            "100": [
                {"fileId": "401", "name": self.NO_SUFFIX, "type": "2",
                 "updateTime": "2026-08-14 09:30:00"},
            ]
        }

    def run_with(self, fake) -> dict:
        plan = ingest.build_plan(
            adapter="docdb", root="100", kb_root=str(self.kb), env=self.env,
            has_openpyxl=False, generated_at=FIXED_NOW, retries=1,
            sleep=lambda _s: None, runner=FakeDocdb(self.folders),
        )
        return ingest.execute_plan(
            self.backend, plan, kb_code=self.kb_code, env=self.env, runner=fake,
            retries=1, sleep=lambda _s: None, now=FIXED_NOW, has_openpyxl=False,
        )

    def provenance(self) -> list:
        text = self.backend.read(ingest.PROVENANCE_CHAIN_REL).decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_a_shell_is_refetched_through_get_full_content(self) -> None:
        fake = FakeDocdb(
            self.folders, blobs={"401": DOCDB_SHELL},
            full_content={"401": self.BODY},
        )
        report = self.run_with(fake)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["converted"], 1, report)

        record = self.provenance()[-1]
        self.assertEqual(record["fetch_mode"], ingest.FETCH_DOCDB_FULL_CONTENT)
        self.assertEqual(record["fetch_reason"], "")
        # 落地的是真身，不是壳：源哈希对的是 get-full-content 的字节。
        self.assertEqual(record["origin_sha256"], ingest.sha256_bytes(self.BODY.encode("utf-8")))
        entry = json.loads(
            self.backend.read(ingest.RAW_INDEX_REL).decode("utf-8")
        )["entries"]["docdb:401"]
        self.assertIn("阶段一", self.backend.read(entry["path"]).decode("utf-8"))
        # 而且确实是第二个接口去取的，不是 download-file 重试出来的。
        scripts = [Path(call[1]).name for call in fake.calls]
        self.assertEqual(scripts, ["download-file.py", "get-full-content.py"], scripts)
        self.assertIn("--file-id", fake.calls[-1])

    def test_a_failed_refetch_keeps_the_shell_and_says_so_on_the_account(self) -> None:
        # 不静默：取不到真身就落占位，理由写进两本账，重跑名单从这里查。
        fake = FakeDocdb(
            self.folders, blobs={"401": DOCDB_SHELL}, full_content={"401": None},
        )
        report = self.run_with(fake)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["placeholder"], 1, report)

        record = self.provenance()[-1]
        self.assertEqual(record["fetch_mode"], ingest.FETCH_DOCDB_DOWNLOAD)
        self.assertIn(ingest.CODE_DOCDB_SHELL, record["fetch_reason"])
        self.assertEqual(record["origin_sha256"], ingest.sha256_bytes(DOCDB_SHELL))
        entry = json.loads(
            self.backend.read(ingest.RAW_INDEX_REL).decode("utf-8")
        )["entries"]["docdb:401"]
        self.assertEqual(entry["placeholder_reason"], ingest.CODE_DOCDB_SHELL)
        state = json.loads(self.backend.read(ingest.INGEST_STATE_REL).decode("utf-8"))
        self.assertEqual(state["items"]["docdb:401"]["status"], "placeholder")

    def test_an_empty_data_field_counts_as_a_failed_refetch(self) -> None:
        fake = FakeDocdb(self.folders, blobs={"401": DOCDB_SHELL}, full_content={"401": "   "})
        report = self.run_with(fake)
        self.assertEqual(report["counts"]["placeholder"], 1, report)
        self.assertIn("data 为空", self.provenance()[-1]["fetch_reason"])

    def test_bytes_that_are_not_a_shell_go_straight_through(self) -> None:
        # 直通分支：一份真 markdown 不许被多打一次接口。
        self.folders["100"][0]["name"] = "401-年度计划.md"
        fake = FakeDocdb(self.folders, blobs={"401": self.BODY.encode("utf-8")})
        report = self.run_with(fake)
        self.assertEqual(report["counts"]["converted"], 1, report)
        record = self.provenance()[-1]
        self.assertEqual(record["fetch_mode"], ingest.FETCH_DOCDB_DOWNLOAD)
        self.assertEqual(record["fetch_reason"], "")
        self.assertEqual([Path(call[1]).name for call in fake.calls], ["download-file.py"])

    def test_a_real_html_attachment_is_downloaded_once_and_not_refetched(self) -> None:
        # 壳检测的第二个条件在整条链上的样子：真 .html 附件照旧只打一次接口，
        # 走 v1.1 的通用占位路径（HTML 正文转换未列入本版），不被当成壳。
        self.folders["100"][0]["name"] = "401-说明.html"
        fake = FakeDocdb(self.folders, blobs={"401": DOCDB_SHELL})
        self.run_with(fake)
        self.assertEqual([Path(call[1]).name for call in fake.calls], ["download-file.py"])
        record = self.provenance()[-1]
        self.assertEqual(record["fetch_mode"], ingest.FETCH_DOCDB_DOWNLOAD)
        self.assertEqual(record["fetch_reason"], "")

    def test_a_local_file_records_the_file_fetch_mode(self) -> None:
        # 镜像面不做壳检测：一份本地 .html 就是一份 .html。
        path = self.base / "page.html"
        path.write_bytes(DOCDB_SHELL)
        fetched = ingest.fetch_bytes({"kind": "file", "path": str(path)})
        self.assertEqual((fetched.mode, fetched.reason, fetched.data),
                         (ingest.FETCH_FILE, "", DOCDB_SHELL))


# ── the real NAS: a smoke test that skips without credentials ───────────────


class NasSmokeTests(unittest.TestCase):
    """真 NAS 冒烟：没有凭据就 skip，绝不在 CI 上偷偷连出去。"""

    def test_nas_round_trip_of_the_ingest_accounts(self) -> None:
        import kb_storage

        missing = [
            name for name in (kb_storage.ENV_HOST, kb_storage.ENV_USER,
                              kb_storage.ENV_PASSWORD, kb_storage.ENV_SHARE)
            if not os.environ.get(name)
        ]
        if missing:
            self.skipTest("没有 NAS 凭据（" + ", ".join(missing) + "），跳过真 NAS 冒烟")
        prefix = os.environ.get("CWK_NAS_KB_SMOKE_PREFIX")
        if not prefix:
            self.skipTest("未设置 CWK_NAS_KB_SMOKE_PREFIX，跳过真 NAS 冒烟")
        backend = kb_storage.build_backend("nas", prefix=prefix)
        try:
            index, state = ingest.load_accounts_readonly(backend)
            self.assertIsInstance(index.get("entries"), dict)
            self.assertIsInstance(state.get("items"), dict)
        finally:
            kb_storage.close_backend(backend)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
