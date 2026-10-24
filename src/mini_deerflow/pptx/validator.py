"""Independent OOXML/package inspection plus python-pptx reopening.

No renderer imports: expected content is derived from contracts, never a render
manifest. Validation proves structure/content/geometry, not visual font shaping.
"""

import math
import posixpath
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from pptx import Presentation
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE_TYPE

from mini_deerflow.pptx.schemas import DeckSpec

MAX_FILE_BYTES = 10_000_000
MAX_UNPACKED_BYTES = 40_000_000
NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/package/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


class PptxValidationError(ValueError):
    """The package is corrupt, unsafe, off-canvas, or differs from its contract."""


@dataclass(frozen=True)
class SlideInspection:
    id: str
    layout: str
    title: str
    text: tuple[str, ...]
    text_boxes: int
    shapes: int
    tables: int
    charts: int
    geometry_checked: int


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    slide_count: int
    layouts: tuple[str, ...]
    native_objects: dict[str, int]
    geometry_checked: int
    file_bytes: int
    slides: tuple[SlideInspection, ...]
    visual_rendering_verified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PptxValidationError(message)


def _package(data: bytes, *, workbook: bool = False) -> dict:
    """Bound expansion, parse XML securely, check CRCs and internal relationships."""
    with ZipFile(BytesIO(data)) as z:
        infos = z.infolist()
        names = [i.orig_filename for i in infos]
        _require(len(names) == len(set(names)), "duplicate ZIP members")
        _require(len(names) <= 2000, "too many ZIP members")
        _require(
            sum(i.file_size for i in infos) <= MAX_UNPACKED_BYTES,
            "ZIP expansion limit exceeded",
        )
        _require(
            not any(
                (
                    n.lower().endswith(".bin")
                    and (
                        workbook
                        or re.fullmatch(
                            r"ppt/printerSettings/printerSettings[0-9]+\.bin", n
                        )
                        is None
                    )
                )
                or "activex" in n.lower()
                for n in names
            ),
            "active binary content is unsupported",
        )
        _require(
            all(
                not n.startswith("/")
                and ".." not in n.split("/")
                and "\\" not in n
                and "\x00" not in n
                and ":" not in n
                for n in names
            ),
            "unsafe ZIP member path",
        )
        _require(z.testzip() is None, "ZIP CRC mismatch")
        _require(
            {
                "[Content_Types].xml",
                "xl/workbook.xml" if workbook else "ppt/presentation.xml",
            }
            <= set(names),
            "missing presentation parts",
        )
        roots = {
            n: ET.fromstring(z.read(n)) for n in names if n.endswith((".xml", ".rels"))
        }
        for name, root in roots.items():
            if not name.endswith(".rels"):
                continue
            base = posixpath.dirname(posixpath.dirname(name))
            ids = []
            for rel in root:
                ids.append(rel.get("Id"))
                _require(
                    not any(
                        token in rel.get("Type", "").lower()
                        for token in ("vba", "oleobject", "activex")
                    ),
                    "active relationship type is unsupported",
                )
                _require(
                    rel.get("TargetMode") != "External",
                    "external relationships are unsupported",
                )
                target = rel.get("Target", "")
                resolved = (
                    posixpath.normpath(posixpath.join(base, target.lstrip("/")))
                    if not target.startswith("/")
                    else target[1:]
                )
                _require(resolved in names, f"missing relationship target: {resolved}")
            _require(len(ids) == len(set(ids)), "duplicate relationship IDs")
        return roots


def _expected_text(content) -> dict[str, tuple[str, ...]]:
    match content.kind:
        case "title":
            return {"subtitle": (content.subtitle,)}
        case "bullets":
            return {"items": tuple(f"• {t}" for t in content.items)}
        case "comparison":
            return {
                "left-heading": (content.left.heading,),
                "right-heading": (content.right.heading,),
                "left-items": tuple(f"• {t}" for t in content.left.items),
                "right-items": tuple(f"• {t}" for t in content.right.items),
            }
        case "metric":
            return {
                "value": (content.value,),
                "label": (content.label,),
                "insight": (content.insight,),
            }
        case "steps":
            return {
                f"step-{i}": (f"{i + 1}. {t}",) for i, t in enumerate(content.items)
            }
        case "chart" | "table":
            return {"caption": (content.caption,)}
        case "conclusion":
            return {
                "items": tuple(f"• {t}" for t in content.items),
                "sources": content.sources,
            }
    raise PptxValidationError("unsupported layout")


def _workbook(data: bytes, content) -> None:
    """Check the editable embedded worksheet, separately from chart caches."""
    roots = _package(data, workbook=True)
    _require(
        not any(root.findall(".//s:f", NS) for root in roots.values()),
        "workbook formulas are unsupported",
    )
    _require(
        not any("vbaProject" in name or "externalLink" in name for name in roots),
        "active workbook content is unsupported",
    )
    sheets = roots["xl/workbook.xml"].findall("s:sheets/s:sheet", NS)
    _require(
        len(sheets) == 1 and sheets[0].get("name") == "Sheet1",
        "unexpected workbook sheets",
    )
    rel_id = sheets[0].get(
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    )
    sheet_targets = [
        r.get("Target")
        for r in roots["xl/_rels/workbook.xml.rels"]
        if r.get("Id") == rel_id
    ]
    _require(
        sheet_targets == ["worksheets/sheet1.xml"], "unexpected worksheet relationship"
    )
    with ZipFile(BytesIO(data)) as z:
        _require(
            sum(i.file_size for i in z.infolist()) < MAX_UNPACKED_BYTES,
            "workbook expansion limit",
        )
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = [
                re.sub(
                    r"_x([0-9A-Fa-f]{4})_",
                    lambda m: chr(int(m[1], 16)),
                    "".join(t.itertext()),
                )
                for t in ET.fromstring(z.read("xl/sharedStrings.xml"))
            ]
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        actual = {}
        for cell in root.findall(".//s:c", NS):
            value = cell.find("s:v", NS)
            if value is None:
                continue
            address = cell.get("r")
            _require(address not in actual, "duplicate workbook cell")
            if cell.get("t") == "s":
                index = int(value.text)
                _require(
                    0 <= index < len(strings), "invalid workbook shared-string index"
                )
                actual[address] = strings[index]
            else:
                actual[address] = float(value.text)
        expected = {f"{chr(66 + i)}1": s.name for i, s in enumerate(content.series)}
        for ri, category in enumerate(content.categories, start=2):
            expected[f"A{ri}"] = category
            for si, series in enumerate(content.series):
                expected[f"{chr(66 + si)}{ri}"] = series.values[ri - 2]
        _require(actual == expected, "embedded chart workbook data mismatch")


def _chart_references(chart, content) -> None:
    root = ET.fromstring(chart.part.blob)
    series = root.findall(".//c:ser", NS)
    _require(len(series) == len(content.series), "chart series count mismatch")
    end = len(content.categories) + 1
    for i, item in enumerate(series):
        for cache_path, count in (
            ("c:tx/c:strRef/c:strCache", 1),
            ("c:cat/c:strRef/c:strCache", len(content.categories)),
            ("c:val/c:numRef/c:numCache", len(content.categories)),
        ):
            cache = item.find(cache_path, NS)
            _require(cache is not None, "missing chart cache")
            pt_count = cache.find("c:ptCount", NS)
            points = cache.findall("c:pt", NS)
            _require(
                pt_count is not None
                and pt_count.get("val") == str(count)
                and [point.get("idx") for point in points]
                == [str(n) for n in range(count)],
                "invalid chart cache point count/indices",
            )
        col = chr(66 + i)
        formulas = tuple(f.text for f in item.findall(".//c:f", NS))
        _require(
            formulas
            == (
                f"Sheet1!${col}$1",
                f"Sheet1!$A$2:$A${end}",
                f"Sheet1!${col}$2:${col}${end}",
            ),
            "chart workbook references mismatch",
        )
        categories = tuple(
            v.text for v in item.findall("c:cat/c:strRef/c:strCache/c:pt/c:v", NS)
        )
        _require(categories == content.categories, "chart category cache mismatch")


def _text_geometry(shape) -> None:
    """Independent conservative capacity check using reopened font/box metrics."""
    width, height = shape.width / 12700, shape.height / 12700  # points
    required = 0.0
    for paragraph in shape.text_frame.paragraphs:
        size = paragraph.font.size
        _require(size is not None and 10 <= size.pt <= 72, "invalid text font size")
        capacity = max(1, int((width - 0.22 * 72) / (size.pt * 0.65)))
        units = sum(
            2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
            for c in paragraph.text
            if not unicodedata.combining(c)
        )
        required += max(1, math.ceil(units / capacity)) * size.pt * 1.3 + 8
    _require(
        width >= 0.3 * 72 and required <= height - 0.12 * 72,
        "text geometry capacity exceeded",
    )


def validate_pptx(
    source: bytes | str | Path, expected: DeckSpec, *, min_layouts: int = 1
) -> ValidationReport:
    """Validate a generated deck against its complete expected contract.

    Raises PptxValidationError on any failed criterion; no success-shaped partial
    results. Path input is local caller authority, never taken from DeckSpec.
    """
    expected = DeckSpec.model_validate(expected.model_dump())
    try:
        if isinstance(source, bytes):
            data = source
        else:
            path = Path(source)
            _require(path.stat().st_size <= MAX_FILE_BYTES, "file size limit exceeded")
            with path.open("rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
        _require(0 < len(data) <= MAX_FILE_BYTES, "file size limit exceeded")
        roots = _package(data)
        prs = Presentation(BytesIO(data))
        _require(
            (prs.slide_width, prs.slide_height) == (11704320, 6583680),
            "expected 16:9 canvas",
        )
        _require(prs.core_properties.title == expected.title, "deck title mismatch")
        _require(len(prs.slides) == len(expected.slides), "slide count mismatch")
        ids = roots["ppt/presentation.xml"].findall("p:sldIdLst/p:sldId", NS)
        xml_slides = [
            r
            for n, r in roots.items()
            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        ]
        _require(
            len(ids) == len(xml_slides) == len(expected.slides),
            "OOXML slide count mismatch",
        )
        inspections = []
        totals = Counter(text_boxes=0, shapes=0, tables=0, charts=0)
        with ZipFile(BytesIO(data)) as z:
            for i, (slide, spec) in enumerate(zip(prs.slides, expected.slides)):
                _require(
                    slide.name == f"{spec.id}:{spec.content.kind}",
                    "slide identity/order/layout mismatch",
                )
                wanted = {
                    "title": (spec.title,),
                    "page": (str(i + 1),),
                    **_expected_text(spec.content),
                }
                texts = {}
                counts = Counter(text_boxes=0, shapes=0, tables=0, charts=0)
                names = []
                for shape in slide.shapes:
                    names.append(shape.name)
                    _require(shape.rotation == 0, "rotated geometry unsupported")
                    _require(
                        shape.left >= 0
                        and shape.top >= 0
                        and shape.width > 0
                        and shape.height > 0
                        and shape.left + shape.width <= prs.slide_width
                        and shape.top + shape.height <= prs.slide_height,
                        f"off-canvas geometry on slide {i + 1}: {shape.name}",
                    )
                    if shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX:
                        _text_geometry(shape)
                        counts["text_boxes"] += 1
                        texts[shape.name] = tuple(
                            p.text for p in shape.text_frame.paragraphs
                        )
                    elif shape.has_table:
                        counts["tables"] += 1
                        _require(
                            spec.content.kind == "table", "unexpected native table"
                        )
                        table = shape.table
                        _require(
                            all(col.width >= 457200 for col in table.columns)
                            and all(row.height >= 274320 for row in table.rows)
                            and sum(col.width for col in table.columns) <= shape.width
                            and sum(row.height for row in table.rows) <= shape.height,
                            "invalid table geometry",
                        )
                        actual = tuple(
                            tuple(c.text for c in row.cells) for row in table.rows
                        )
                        wanted_rows = tuple(
                            tuple(str(v) for v in row)
                            for row in (spec.content.headers, *spec.content.rows)
                        )
                        _require(
                            actual == wanted_rows,
                            "table cell content/dimensions mismatch",
                        )
                    elif shape.has_chart:
                        counts["charts"] += 1
                        _require(
                            spec.content.kind == "chart", "unexpected native chart"
                        )
                        chart, c = shape.chart, spec.content
                        _require(
                            shape.width >= 1828800 and shape.height >= 1828800,
                            "invalid chart geometry",
                        )
                        chart_type = {
                            "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
                            "bar": XL_CHART_TYPE.BAR_CLUSTERED,
                            "line": XL_CHART_TYPE.LINE,
                        }[c.chart_type]
                        _require(chart.chart_type == chart_type, "chart type mismatch")
                        _require(
                            tuple(cat.label for cat in chart.plots[0].categories)
                            == c.categories,
                            "chart categories mismatch",
                        )
                        _require(
                            tuple((s.name, tuple(s.values)) for s in chart.series)
                            == tuple((s.name, s.values) for s in c.series),
                            "chart data mismatch",
                        )
                        _require(
                            chart.plots[0].has_data_labels, "missing chart data labels"
                        )
                        _chart_references(chart, c)
                        _workbook(chart.part.chart_workbook.xlsx_part.blob, c)
                    elif shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                        counts["shapes"] += 1
                    else:
                        raise PptxValidationError("unsupported/non-native object")
                _require(len(names) == len(set(names)), "duplicate object names")
                _require(
                    texts == wanted, f"text missing/changed/unexpected on slide {i + 1}"
                )
                _require(
                    counts["tables"] == int(spec.content.kind == "table"),
                    "native table missing",
                )
                _require(
                    counts["charts"] == int(spec.content.kind == "chart"),
                    "native chart missing",
                )
                shape_count = 1 + (
                    2
                    if spec.content.kind == "comparison"
                    else len(spec.content.items)
                    if spec.content.kind == "steps"
                    else 0
                )
                _require(
                    counts["shapes"] == shape_count,
                    "native decorative shapes missing/unexpected",
                )
                root = ET.fromstring(z.read(str(slide.part.partname).lstrip("/")))
                _require(
                    len(root.findall(".//p:sp", NS))
                    == counts["text_boxes"] + counts["shapes"],
                    "OOXML native shape count mismatch",
                )
                _require(
                    len(root.findall(".//a:tbl", NS)) == counts["tables"],
                    "OOXML table count mismatch",
                )
                _require(
                    len(root.findall(".//c:chart", NS)) == counts["charts"],
                    "OOXML chart count mismatch",
                )
                totals.update(counts)
                inspections.append(
                    SlideInspection(
                        spec.id,
                        spec.content.kind,
                        spec.title,
                        tuple(t for lines in texts.values() for t in lines),
                        **counts,
                        geometry_checked=len(slide.shapes),
                    )
                )
        layouts = tuple(sorted({s.layout for s in inspections}))
        _require(len(layouts) >= min_layouts, "insufficient layout coverage")
        return ValidationReport(
            True,
            len(inspections),
            layouts,
            dict(totals),
            sum(s.geometry_checked for s in inspections),
            len(data),
            tuple(inspections),
        )
    except PptxValidationError:
        raise
    except (
        BadZipFile,
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        IndexError,
        OSError,
        ET.ParseError,
        DefusedXmlException,
    ) as exc:
        raise PptxValidationError(
            f"invalid PPTX package: {type(exc).__name__}"
        ) from exc
