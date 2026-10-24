"""Render a validated contract into native editable PowerPoint objects.

Bytes are returned to the caller: this layer has no workspace or path authority.
Determinism is semantic (ZIP timestamps are not a public byte-level contract).
"""

import math
import unicodedata
from datetime import UTC, datetime
from io import BytesIO

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt
from xlsxwriter import Workbook

from mini_deerflow.pptx.design import (
    BODY_HEIGHT,
    BODY_TOP,
    GAP,
    HEIGHT,
    MARGIN,
    THEMES,
    WIDTH,
    Theme,
)
from mini_deerflow.pptx.schemas import DeckSpec


class LayoutOverflowError(ValueError):
    """Content exceeds a conservative layout density budget; split the slide."""


def _units(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
        for c in text
        if not unicodedata.combining(c)
    )


def _fit(lines: tuple[str, ...], width: float, height: float, size: int) -> None:
    # Conservative preflight, not a font shaping / visual overflow proof.
    capacity = max(1, int((width - 0.22) * 72 / (size * 0.65)))
    required = sum(
        max(1, math.ceil(_units(t) / capacity)) * size * 1.3 + 8 for t in lines
    )
    if required > (height - 0.12) * 72:
        raise LayoutOverflowError(
            "content density exceeds layout; shorten text or split slide"
        )


def _text(
    slide,
    name: str,
    lines: tuple[str, ...],
    box: tuple[float, float, float, float],
    theme: Theme,
    size: int,
    *,
    bold: bool = False,
    color: str | None = None,
):
    x, y, w, h = box
    _fit(lines, w, h, size)
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    shape.name = name
    frame = shape.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = Inches(0.08)
    frame.margin_top = frame.margin_bottom = Inches(0.04)
    for i, line in enumerate(lines):
        p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        p.text = line
        p.font.name = theme.font
        p.font.size = Pt(size)
        p.font.bold = bold
        p.font.color.rgb = RGBColor.from_string(color or theme.foreground)
        p.space_after = Pt(8)
    return shape


def _panel(slide, name: str, box, theme: Theme):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *(Inches(v) for v in box))
    shape.name = name
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(theme.surface)
    shape.line.fill.background()


def _chart_workbook(content) -> bytes:
    """Literal, inert data and fixed metadata, matching python-pptx's ranges."""
    output = BytesIO()
    with Workbook(
        output,
        {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False},
    ) as book:
        book.set_properties({"created": datetime(2000, 1, 1, tzinfo=UTC)})
        sheet = book.add_worksheet("Sheet1")
        for i, category in enumerate(content.categories, start=1):
            sheet.write_string(i, 0, category)
        for col, series in enumerate(content.series, start=1):
            sheet.write_string(0, col, series.name)
            for row, value in enumerate(series.values, start=1):
                sheet.write_number(row, col, value)
    return output.getvalue()


def _render_slide(prs, spec, index: int, theme: Theme) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.name = f"{spec.id}:{spec.content.kind}"
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(theme.background)
    _panel(slide, "accent", (MARGIN, 0.45, 0.75, 0.08), theme)
    slide.shapes[-1].fill.fore_color.rgb = RGBColor.from_string(theme.accent)
    _text(
        slide,
        "title",
        (spec.title,),
        (MARGIN, 0.7, WIDTH - 2 * MARGIN, 0.85),
        theme,
        theme.title_pt,
        bold=True,
    )
    _text(
        slide, "page", (str(index + 1),), (11.6, 6.5, 0.6, 0.55), theme, theme.small_pt
    )
    c = spec.content
    w = WIDTH - 2 * MARGIN
    match c.kind:
        case "title":
            _text(slide, "subtitle", (c.subtitle,), (MARGIN, 2.6, w, 2.2), theme, 28)
        case "bullets":
            _text(
                slide,
                "items",
                tuple(f"• {t}" for t in c.items),
                (MARGIN, BODY_TOP, w, BODY_HEIGHT),
                theme,
                theme.body_pt,
            )
        case "comparison":
            cw = (w - GAP) / 2
            for name, column, x in (
                ("left", c.left, MARGIN),
                ("right", c.right, MARGIN + cw + GAP),
            ):
                _panel(slide, f"{name}-panel", (x, BODY_TOP, cw, BODY_HEIGHT), theme)
                _text(
                    slide,
                    f"{name}-heading",
                    (column.heading,),
                    (x + 0.15, 1.9, cw - 0.3, 0.75),
                    theme,
                    24,
                    bold=True,
                )
                _text(
                    slide,
                    f"{name}-items",
                    tuple(f"• {t}" for t in column.items),
                    (x + 0.15, 2.7, cw - 0.3, 3.1),
                    theme,
                    20,
                )
        case "metric":
            _text(
                slide,
                "value",
                (c.value,),
                (MARGIN, 1.9, w, 1.6),
                theme,
                54,
                bold=True,
                color=theme.accent,
            )
            _text(slide, "label", (c.label,), (MARGIN, 3.6, w, 0.9), theme, 26)
            _text(slide, "insight", (c.insight,), (MARGIN, 4.8, w, 1.25), theme, 22)
        case "steps":
            for i, item in enumerate(c.items):
                y = BODY_TOP + i * 0.85
                _panel(slide, f"step-panel-{i}", (MARGIN, y, w, 0.72), theme)
                _text(
                    slide,
                    f"step-{i}",
                    (f"{i + 1}. {item}",),
                    (MARGIN + 0.15, y + 0.04, w - 0.3, 0.64),
                    theme,
                    20,
                )
        case "chart":
            data = CategoryChartData()
            data.categories = c.categories
            for series in c.series:
                data.add_series(series.name, series.values)
            kind = {
                "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
                "bar": XL_CHART_TYPE.BAR_CLUSTERED,
                "line": XL_CHART_TYPE.LINE,
            }[c.chart_type]
            chart = slide.shapes.add_chart(
                kind, Inches(MARGIN), Inches(BODY_TOP), Inches(w), Inches(4.1), data
            ).chart
            chart.part.chart_workbook.update_from_xlsx_blob(_chart_workbook(c))
            slide.shapes[-1].name = "chart"
            chart.font.name = theme.font
            chart.font.size = Pt(theme.small_pt)
            chart.font.color.rgb = RGBColor.from_string(theme.foreground)
            chart.has_legend = True
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            chart.plots[0].has_data_labels = True
            palette = (theme.accent, theme.muted, theme.foreground)
            for series, color in zip(chart.series, palette):
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = RGBColor.from_string(color)
                series.format.line.color.rgb = RGBColor.from_string(color)
            _text(slide, "caption", (c.caption,), (MARGIN, 5.95, w, 0.6), theme, 14)
        case "table":
            rows = (c.headers, *c.rows)
            table = slide.shapes.add_table(
                len(rows),
                len(c.headers),
                Inches(MARGIN),
                Inches(BODY_TOP),
                Inches(w),
                Inches(4.1),
            ).table
            slide.shapes[-1].name = "table"
            for ri, row in enumerate(rows):
                for ci, value in enumerate(row):
                    cell = table.cell(ri, ci)
                    text = str(value)
                    _fit((text,), w / len(c.headers), 4.1 / len(rows), 16)
                    cell.text = text
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor.from_string(
                        theme.surface if ri == 0 else theme.background
                    )
                    p = cell.text_frame.paragraphs[0]
                    p.font.name = theme.font
                    p.font.size = Pt(16)
                    p.font.bold = ri == 0
                    p.font.color.rgb = RGBColor.from_string(theme.foreground)
            _text(slide, "caption", (c.caption,), (MARGIN, 5.95, w, 0.6), theme, 14)
        case "conclusion":
            _text(
                slide,
                "items",
                tuple(f"• {t}" for t in c.items),
                (MARGIN, BODY_TOP, w, 2.5),
                theme,
                theme.body_pt,
            )
            _text(
                slide,
                "sources",
                c.sources,
                (MARGIN, 4.5, w, 1.8),
                theme,
                16,
                color=theme.muted,
            )


def render_deck(spec: DeckSpec) -> bytes:
    """Revalidate even model_construct/model_copy inputs; return genuine PPTX bytes."""
    spec = DeckSpec.model_validate(spec.model_dump())
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(WIDTH), Inches(HEIGHT)
    props = prs.core_properties
    props.title = spec.title
    props.author = "Mini DeerFlow"
    props.created = props.modified = datetime(2000, 1, 1, tzinfo=UTC)
    props.revision = 1
    for i, slide in enumerate(spec.slides):
        _render_slide(prs, slide, i, THEMES[spec.theme])
    output = BytesIO()
    prs.save(output)
    return output.getvalue()
