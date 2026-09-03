import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))

from cwk_cloud_wiki_compile import owner_involved, partition_year  # noqa: E402


WRITER_OWNER = """---
report_id: "9001"
title: "owner writer"
writer: "张成鹏"
create_time: "2026-09-03 10:00:00"
---

# 样本

<content>
正文。
</content>
"""

ROLE_NAME = """---
report_id: "9002"
title: "role name"
writer: "刘丽华"
---

<content>
建议人：李四
审批人：张成鹏
</content>
"""

ROLE_EMPID = """---
report_id: "9003"
title: "role empid"
writer: "王五"
---

<content>
部门负责人[{"avatar":"","id":"1514822105176903682","name":"张三"}]
</content>
"""

META_BULLET = """---
report_id: "9005"
title: "meta bullet"
writer: "刘丽华"
---

<meta>
- **汇报人**: 张成鹏
</meta>

<content>
正文。
</content>
"""

PROSE_ONLY = """---
report_id: "9004"
title: "prose only"
writer: "刘丽华"
---

<content>
今天张成鹏来过会议室，正文里提到了他，但没有任何角色字段。
决策人：王五
</content>
"""


class OwnerInvolvedTests(unittest.TestCase):
    def _tmp_raw(self, content: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raw = Path(tmp.name) / "sample.md"
        raw.write_text(content, encoding="utf-8")
        return raw

    def test_writer_match(self):
        self.assertTrue(owner_involved(self._tmp_raw(WRITER_OWNER), "张成鹏"))

    def test_role_line_name_match(self):
        self.assertTrue(owner_involved(self._tmp_raw(ROLE_NAME), "张成鹏"))

    def test_role_line_empid_match(self):
        self.assertTrue(owner_involved(self._tmp_raw(ROLE_EMPID), "1514822105176903682"))

    def test_meta_bullet_role_match(self):
        self.assertTrue(owner_involved(self._tmp_raw(META_BULLET), "张成鹏"))

    def test_prose_mention_does_not_match(self):
        self.assertFalse(owner_involved(self._tmp_raw(PROSE_ONLY), "张成鹏"))

    def test_bystander_no_match(self):
        self.assertFalse(owner_involved(self._tmp_raw(ROLE_NAME), "赵六"))

    def test_no_needles_false(self):
        self.assertFalse(owner_involved(self._tmp_raw(WRITER_OWNER)))


class PartitionYearTests(unittest.TestCase):
    def test_partition_year(self):
        self.assertEqual(partition_year(Path("/m/raw/2026-08/2026-08-01/x.md")), 2026)
        self.assertEqual(partition_year(Path("/m/raw/2022-03/2022-03-22/y.md")), 2022)

    def test_partition_year_unknown(self):
        self.assertEqual(partition_year(Path("/m/elsewhere/x.md")), 0)


class CliValidationTests(unittest.TestCase):
    def test_owner_scope_requires_identity(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "cwk_cloud_wiki_compile.py"), "--refine-scope", "owner"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--refine-scope owner requires", result.stderr)

    def test_negative_min_year_rejected(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "cwk_cloud_wiki_compile.py"), "--min-year", "-1"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--min-year must be", result.stderr)


if __name__ == "__main__":
    unittest.main()
