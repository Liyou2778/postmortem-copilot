"""Word export tests.

The export is only useful if it keeps structure (tables, headings) *and*
traceability (evidence ids as real hyperlinks). Both are asserted here by
reopening the generated document, not by trusting the writer.
"""
from __future__ import annotations

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from docx import Document  # noqa: E402

from app.docx_export import markdown_to_docx  # noqa: E402

TASK_ID = "11111111-2222-3333-4444-555555555555"
BASE = "http://example.test/tasks/1111/evidence"

REPORT = """# 故障复盘初稿

**服务**：order-service　**时间窗**：30m

## 一、时间线

| 时间 | 类型 | 事件 | 证据 |
|---|---|---|---|
| 10:02 | metric | 延迟拐点 | E-TLM-001 |
| 10:03 | deploy | 发布变更 | E-CHG-001 |

## 二、根因候选

1. 连接池上限从 200 降到 50，证据 `E-CHG-001`。

- 列表项 A
- 列表项 B
"""


class TestDocxExport(unittest.TestCase):
    def setUp(self) -> None:
        blob = markdown_to_docx(REPORT, task_id=TASK_ID, base_url=BASE)
        self.blob = blob
        self.doc = Document(io.BytesIO(blob))

    def test_produces_valid_docx(self):
        self.assertTrue(self.blob.startswith(b"PK"), "docx 应是 zip 容器")
        self.assertGreater(len(self.doc.paragraphs), 0)

    def test_headings_preserved(self):
        texts = [p.text for p in self.doc.paragraphs]
        self.assertTrue(any("故障复盘初稿" in t for t in texts))
        # markdown h2 becomes a real heading style
        levels = [p.style.name for p in self.doc.paragraphs]
        self.assertIn("Heading 2", levels)

    def test_table_preserved_with_evidence_ids(self):
        self.assertEqual(len(self.doc.tables), 1)
        table = self.doc.tables[0]
        header = [c.text for c in table.rows[0].cells]
        self.assertEqual(header[:2], ["时间", "类型"])
        self.assertEqual(len(table.rows), 3, "表头 + 2 行数据")

    def test_evidence_ids_become_hyperlinks(self):
        rels = self.doc.part.rels
        targets = [r.target_ref for r in rels.values() if "hyperlink" in r.reltype]
        self.assertTrue(targets, "应至少生成一个超链接关系")
        self.assertTrue(
            any(t.endswith("/E-TLM-001") for t in targets),
            f"表格里的证据编号应指向证据页，实际: {targets}",
        )
        self.assertTrue(
            any(t.endswith("/E-CHG-001") for t in targets),
            f"正文反引号里的证据编号也应变成链接，实际: {targets}",
        )

    def test_hyperlink_element_present_in_body_and_table(self):
        def has_link(paragraphs) -> bool:
            return any("w:hyperlink" in p._p.xml for p in paragraphs)

        body_has = has_link(self.doc.paragraphs)
        table_has = any(
            has_link(cell.paragraphs) for row in self.doc.tables[0].rows for cell in row.cells
        )
        self.assertTrue(body_has, "正文段落里应有 w:hyperlink 元素")
        self.assertTrue(table_has, "表格单元格里应有 w:hyperlink 元素")

    def test_bullets_use_list_style(self):
        styles = {p.style.name for p in self.doc.paragraphs}
        self.assertIn("List Bullet", styles)

    def test_code_span_is_rendered_as_text(self):
        texts = "\n".join(p.text for p in self.doc.paragraphs)
        self.assertIn("E-CHG-001", texts)
        self.assertNotIn("`", texts, "反引号不应出现在成品里")


if __name__ == "__main__":
    unittest.main(verbosity=2)
