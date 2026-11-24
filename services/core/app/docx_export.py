"""Markdown report → Word document.

The point of a Word export is that a postmortem gets circulated, reviewed and
signed outside the service. Two things must survive that trip:

* **Structure** — headings, tables and bullets, so the document is navigable.
* **Traceability** — every `E-XXX-001` becomes a real hyperlink back to its
  evidence page, so a reader in Word can still verify a claim.

python-docx has no public hyperlink API, so `add_hyperlink` builds the
`w:hyperlink` element and relationship manually. That is the documented
workaround and the only non-obvious part of this module.
"""
from __future__ import annotations

import io
import re
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

CJK_FONT = "微软雅黑"
MONO_FONT = "Consolas"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_SEP = re.compile(r"^\|[\s:|-]+\|$")
_EV_ID = re.compile(r"\bE-[A-Z]{3}-\d{3}\b")
# Split on the three inline constructs we support, keeping delimiters.
_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|E-[A-Z]{3}-\d{3})")


def add_hyperlink(paragraph, url: str, text: str) -> None:
    """Append a clickable external link to a paragraph."""
    part = paragraph.part
    rel_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), rel_id)

    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1F4E79")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(color)
    props.append(underline)
    run.append(props)

    text_el = OxmlElement("w:t")
    text_el.text = text
    run.append(text_el)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _style_run(run, *, size: float = 10.5, bold: bool = False,
               mono: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = MONO_FONT if mono else CJK_FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)
    run.font.size = Pt(size)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _add_inline(paragraph, text: str, base_url: str, size: float = 10.5) -> None:
    """Render **bold**, `code` and evidence ids (as hyperlinks) inside a run seq.

    Evidence ids wrapped in backticks still become links: in the report the id is
    usually written as `` `E-TLM-001` ``, and turning that into dead code-styled
    text would silently drop the whole traceability promise of the export.
    """
    for chunk in _INLINE.split(text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**") and len(chunk) > 4:
            _style_run(paragraph.add_run(chunk[2:-2]), size=size, bold=True)
        elif chunk.startswith("`") and chunk.endswith("`") and len(chunk) > 2:
            inner = chunk[1:-1]
            if _EV_ID.fullmatch(inner):
                add_hyperlink(paragraph, f"{base_url.rstrip('/')}/{inner}", inner)
            else:
                _style_run(paragraph.add_run(inner), size=size - 0.5,
                           mono=True, color=RGBColor(0xB0, 0x30, 0x60))
        elif _EV_ID.fullmatch(chunk):
            add_hyperlink(paragraph, f"{base_url.rstrip('/')}/{chunk}", chunk)
        else:
            _style_run(paragraph.add_run(chunk), size=size)


def markdown_to_docx(markdown_text: str, *, task_id: str, base_url: str,
                     title: str = "故障复盘报告") -> bytes:
    """Return the .docx bytes. ``base_url`` should be the report page URL."""
    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.name = CJK_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)

    head = doc.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _style_run(head.add_run(title), size=18, bold=True, color=ACCENT)

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _style_run(meta.add_run(f"任务 {task_id}"), size=9, color=RGBColor(0x66, 0x66, 0x66))

    lines = markdown_text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _SEP.match(lines[i + 1].strip()):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table = doc.add_table(rows=1, cols=len(header))
            table.style = "Table Grid"
            for idx, cell in enumerate(table.rows[0].cells):
                cell.text = ""
                para = cell.paragraphs[0]
                _add_inline(para, header[idx] if idx < len(header) else "", base_url, size=9)
                for run in para.runs:
                    run.bold = True
            for row in rows:
                cells = table.add_row().cells
                for idx, cell in enumerate(cells):
                    cell.text = ""
                    _add_inline(cell.paragraphs[0], row[idx] if idx < len(row) else "",
                                base_url, size=9)
            doc.add_paragraph()
            continue

        if stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            doc.add_heading(stripped[2:], level=1)
        elif stripped.startswith("- "):
            while i < len(lines) and lines[i].strip().startswith("- "):
                para = doc.add_paragraph(style="List Bullet")
                _add_inline(para, lines[i].strip()[2:], base_url)
                i += 1
            continue
        else:
            para = doc.add_paragraph()
            _add_inline(para, stripped, base_url)
        i += 1

    note = doc.add_paragraph()
    _style_run(
        note.add_run("说明：报告中形如 E-XXX-001 的编号为证据链接，点击可在系统中查看证据原文、"
                     "抓取时间与内容哈希。"),
        size=8.5, color=RGBColor(0x66, 0x66, 0x66),
    )

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
