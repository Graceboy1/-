"""Render the maintained technical Markdown, without loading app or secrets."""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "output" / "pdf" / "智慧交易助手-参赛技术文档-20261117.pdf"
INK, MUTED, LINE, PAPER = map(colors.HexColor, ("#171a20", "#5b626d", "#d9dde3", "#f3f5f7"))
MARGIN = 48
PAGE_WIDTH, PAGE_HEIGHT = A4
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN


def markup(text: str) -> str:
    text = escape(text.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-"))
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r'<font color="#36475e">\1</font>', text)
    text = re.sub(r"\[([^\]]+)\]\((https://[^\s)]+)\)", r'<link href="\2" color="#36475e">\1</link>', text)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)


class TechnicalDocument(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in {"section", "subsection"}:
            level = 0 if flowable.style.name == "section" else 1
            anchor = f"section-{self.seq.nextf('heading')}"
            self.canv.bookmarkPage(anchor)
            self.canv.addOutlineEntry(flowable.getPlainText(), anchor, level=level, closed=False)


def export(source: Path, output: Path, regular_font: Path, bold_font: Path) -> int:
    for font in (regular_font, bold_font):
        if not font.is_file():
            raise SystemExit(f"Font unavailable: {font}. Supply --font and --bold-font.")
    pdfmetrics.registerFont(TTFont("DeskBody", str(regular_font), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("DeskBold", str(bold_font), subfontIndex=0))
    pdfmetrics.registerFontFamily("DeskBody", normal="DeskBody", bold="DeskBold")
    body = ParagraphStyle("body", fontName="DeskBody", fontSize=10.8, leading=18,
                          textColor=INK, wordWrap="CJK", spaceAfter=8, allowWidows=0, allowOrphans=0)
    title = ParagraphStyle("title", parent=body, fontName="DeskBold", fontSize=24,
                           leading=34, spaceAfter=15, keepWithNext=True)
    section = ParagraphStyle("section", parent=body, fontName="DeskBold", fontSize=15,
                             leading=23, spaceBefore=14, spaceAfter=8, keepWithNext=True)
    subsection = ParagraphStyle("subsection", parent=body, fontName="DeskBold", fontSize=12,
                                leading=20, spaceBefore=9, spaceAfter=5, keepWithNext=True)
    label = ParagraphStyle("label", parent=body, fontSize=9, leading=15, textColor=MUTED)
    small = ParagraphStyle("small", parent=body, fontSize=9.2, leading=15, spaceAfter=0)
    header = ParagraphStyle("table-header", parent=small, fontName="DeskBold", textColor=colors.white)
    code = ParagraphStyle("code", parent=body, fontSize=9.1, leading=14.8,
                          leftIndent=8, rightIndent=8, spaceAfter=2)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=13, firstLineIndent=-9, spaceAfter=5)
    story = [Paragraph("技术文档　材料快照 2026-11-17", label), Spacer(1, 9)]
    lines = source.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if line.startswith("# "):
            story.append(Paragraph(markup(line[2:]), title))
        elif line.startswith("## "):
            story.append(Paragraph(markup(line[3:]), section))
        elif line.startswith("### "):
            story.append(Paragraph(markup(line[4:]), subsection))
        elif line.startswith("```"):
            block = [Spacer(1, 3)]
            while i < len(lines) and not lines[i].strip().startswith("```"):
                text = lines[i].rstrip().replace("\t", "  ")
                block.append(Paragraph(escape(text).replace("  ", "&#160;&#160;") or "&#160;", code))
                i += 1
            i += 1
            block.append(Spacer(1, 7))
            # Architecture chains and installation snippets must not be split
            # after one line when preceding account text grows across versions.
            story.append(KeepTogether(block))
        elif line.startswith("|"):
            raw = []
            i -= 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                    raw.append(cells)
                i += 1
            columns = len(raw[0])
            if any(len(row) != columns for row in raw):
                raise ValueError("Malformed Markdown table")
            ratios = {2: [0.26, 0.74], 3: [0.23, 0.38, 0.39], 4: [0.20, 0.26, 0.27, 0.27]}.get(columns)
            if columns == 2 and raw[0][0] == "路径":
                # Keep real source filenames readable instead of splitting .py/.tsx.
                ratios = [0.42, 0.58]
            widths = [CONTENT_WIDTH * ratio for ratio in ratios] if ratios else [CONTENT_WIDTH / columns] * columns
            rows = [[Paragraph(markup(cell), header if n == 0 else small) for cell in row] for n, row in enumerate(raw)]
            table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#303b4a")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PAPER]),
                ("GRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.extend([table, Spacer(1, 10)])
        elif line.startswith("- "):
            story.append(Paragraph("• " + markup(line[2:]), bullet))
        else:
            story.append(Paragraph(markup(line), body))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(MUTED)
        canvas.setFont("DeskBody", 8)
        canvas.drawString(MARGIN, 25, "智慧交易助手　真实资料研究 / 虚拟资金模拟")
        canvas.drawRightString(PAGE_WIDTH - MARGIN, 25, f"{doc.page}")
        canvas.restoreState()

    output.parent.mkdir(parents=True, exist_ok=True)
    document = TechnicalDocument(str(output), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                                 topMargin=42, bottomMargin=48, title="智慧交易助手参赛技术文档",
                                 author="金融大佬 - 兰州交通大学", subject="多席位策略研究与交易复盘的设计与实现")
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    from pypdf import PdfReader
    count = len(PdfReader(output).pages)
    if count > 30:
        raise SystemExit(f"PDF has {count} pages, exceeding the 30-page submission limit.")
    print(f"PDF ready: {output} ({count} pages)")
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "docs" / "技术说明.md")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyh.ttc"))
    parser.add_argument("--bold-font", type=Path, default=Path("C:/Windows/Fonts/msyhbd.ttc"))
    args = parser.parse_args()
    export(args.source, args.output, args.font, args.bold_font)


if __name__ == "__main__":
    main()
