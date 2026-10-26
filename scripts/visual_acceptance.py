"""Reproducible Office visual-acceptance evidence from native DeckSpec decks.

Never calls an LLM. A PDF/PNG rendering proves a viewer rendered something,
not that a human checked typography, clipping, edit persistence or semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from struct import unpack

from mini_deerflow.demo.phase2_pptx_offline import demo_spec
from mini_deerflow.pptx import DeckSpec, render_deck, validate_pptx

LANGUAGES = ("vi", "en")
THEMES = ("light", "dark", "academic")
LAYOUTS = (
    "title", "bullets", "comparison", "metric",
    "steps", "chart", "table", "conclusion",
)
CHECKLIST = (
    "clipping",
    "wrapping",
    "overlap",
    "vietnamese_diacritics",
    "font_substitution",
    "contrast",
    "chart_labels",
    "table_readability",
    "source_label_readability",
    "edit_and_resave_pptx",
    "powerpoint_viewer",
)
ENGLISH_CONTENT = (
    ("Fixture research overview", {
        "kind": "title",
        "subtitle": "Mini DeerFlow — offline, editable PowerPoint objects",
    }),
    ("Scope and verification goals", {
        "kind": "bullets",
        "items": [
            "All text, shapes, native charts and table cells remain editable",
            "Screenshots test rendering, not factual accuracy or editing",
            "This is illustrative fixture data, not real measurements",
        ],
    }),
    ("Compare visual delivery", {
        "kind": "comparison",
        "left": {
            "heading": "Static screenshot",
            "items": ["Elements cannot be edited individually", "Source values remain opaque"],
        },
        "right": {
            "heading": "Native objects",
            "items": ["Edit text directly in PowerPoint", "Charts retain embedded workbook data"],
        },
    }),
    ("Illustrative slide count", {
        "kind": "metric",
        "value": "8",
        "label": "Editable content slides",
        "insight": "Fixture total only, not a runtime benchmark.",
    }),
    ("Rendering acceptance process", {
        "kind": "steps",
        "items": [
            "Validate the DeckSpec contract",
            "Render native editable objects",
            "Open with a real office renderer",
            "Review fonts, overlap and edits",
        ],
    }),
    ("Chart — illustrative series", {
        "kind": "chart",
        "chart_type": "column",
        "categories": ["January", "February", "March"],
        "series": [
            {"name": "Example A", "values": [12, 18, 24]},
            {"name": "Example B", "values": [10, 16, 21]},
        ],
        "caption": "Source: fixture values, units: examples; not observed research.",
    }),
    ("Native table readability", {
        "kind": "table",
        "headers": ["Object", "Count", "Editable"],
        "rows": [
            ["Native chart", 1, "Workbook"],
            ["Native table", 1, "Each cell"],
            ["Sample slides", 8, "Yes"],
        ],
        "caption": "Source: offline fixture contract, not measured results.",
    }),
    ("Conclusions and sources", {
        "kind": "conclusion",
        "items": [
            "Verify visuals in a real viewer, not just OOXML",
            "Inspect and save an edited PPTX before approval",
        ],
        "sources": [
            (
                "Source: internal, illustrative Mini DeerFlow fixture — "
                "sample evidence label for visual wrapping and legibility; "
                "this is not a real research citation."
            ),
        ],
    }),
)


def make_spec(language: str, theme: str) -> DeckSpec:
    """Eight native layouts; both locales stress fonts, tables and chart labels."""
    if language not in LANGUAGES or theme not in THEMES:
        raise ValueError("unsupported acceptance language/theme")
    spec = demo_spec(theme).model_dump(mode="json")
    if language == "en":
        spec["title"] = "Mini DeerFlow — English visual fixture"
        spec["slides"] = [
            {"id": previous["id"], "title": title, "content": content}
            for previous, (title, content) in zip(
                spec["slides"], ENGLISH_CONTENT, strict=True
            )
        ]
    else:
        spec["title"] = "Mini DeerFlow — mẫu dấu tiếng Việt"
        spec["slides"][1]["content"]["items"][0] = (
            "Kiểm tra đầy đủ dấu: ă â ê ô ơ ư đ — Ă Â Ê Ô Ơ Ư Đ"
        )
        spec["slides"][5]["content"]["categories"] = [
            "Tháng một", "Tháng hai", "Tháng ba"
        ]
        spec["slides"][7]["content"]["sources"] = [
            (
                "Nguồn: bộ dữ liệu minh họa Mini DeerFlow; nhãn nguồn dài "
                "để kiểm tra xuống dòng, dấu tiếng Việt và khả năng đọc. "
                "Không phải trích dẫn nghiên cứu thực tế."
            )
        ]
    resolved = DeckSpec.model_validate(spec)
    if tuple(s.content.kind for s in resolved.slides) != LAYOUTS:
        raise ValueError("fixture missing an editable layout")
    if max(len(x) for x in resolved.slides[7].content.sources) < 100:
        raise ValueError("source label does not exercise line wrapping")
    return resolved


def matrix() -> tuple[tuple[str, str, DeckSpec], ...]:
    return tuple(
        (lang, theme, make_spec(lang, theme))
        for lang in LANGUAGES
        for theme in THEMES
    )


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("viewer output is not a PNG")
    width, height = unpack(">II", header[16:24])
    if width < 700 or height < 380:
        raise ValueError("viewer screenshot is too small to review")
    return width, height


def _command(args: list[str], *, timeout: int = 150) -> str:
    completed = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=timeout,
    )
    if completed.returncode != 0:
        # External programs may echo local paths or secret-bearing environment.
        raise RuntimeError("viewer_command_failed")
    return completed.stdout


def _pages(pdfinfo: str) -> int:
    match = re.search(r"^Pages:\s+(\d+)\s*$", pdfinfo, flags=re.MULTILINE)
    if match is None:
        raise ValueError("viewer_pdf_page_count_unknown")
    return int(match.group(1))


def _pdf_text_coverage(text: str, language: str) -> bool:
    """Informational PDF text extraction check, never a visual PASS."""
    tokens = (
        ("Mini DeerFlow", "chỉnh sửa", "Tháng")
        if language == "vi"
        else ("Mini DeerFlow", "editable", "Source")
    )
    return all(token.casefold() in text.casefold() for token in tokens)


def _write_manifest(destination: Path, report: dict[str, object]) -> None:
    (destination / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def run(
    destination: Path, *,
    render: bool = False,
    soffice: str = "soffice",
    pdfinfo: str = "pdfinfo",
    pdftoppm: str = "pdftoppm",
    pdffonts: str = "pdffonts",
    pdftotext: str = "pdftotext",
) -> dict[str, object]:
    """Generate PPTX; optionally collect actual LibreOffice-rendered PNG evidence."""
    destination.mkdir(parents=True, exist_ok=True)
    deck_dir = destination / "pptx"
    deck_dir.mkdir(exist_ok=True)
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "fixture_origin": "real DeckSpec via mini_deerflow.pptx.render_deck",
        "viewer": "LibreOffice headless" if render else None,
        "matrix": [],
        "visual_reviewer_status": "UNVERIFIED",
        "powerpoint_visual_status": "UNVERIFIED",
        "manual_edit_resave_status": "UNVERIFIED",
        "manual_checklist": {name: "UNVERIFIED" for name in CHECKLIST},
        "overall_status": "BLOCKED_VIEWER_NOT_RUN",
    }
    for language, theme, spec in matrix():
        name = f"{language}-{theme}"
        data = render_deck(spec)
        validation = validate_pptx(data, spec, min_layouts=8)
        if not validation.valid:
            raise ValueError("native deck validation failed")
        (deck_dir / f"{name}.pptx").write_bytes(data)
        report["matrix"].append({
            "case": name,
            "language": language,
            "theme": theme,
            "layouts": list(LAYOUTS),
            "slide_count": 8,
            "pptx": f"pptx/{name}.pptx",
            "pptx_sha256": hashlib.sha256(data).hexdigest(),
            "native_editability_structural": "PASS",
            "viewer_render": "NOT_RUN",
            "rendered_pages": 0,
            "screenshots": [],
            "font_report": None,
            "pdf_text_extractable": None,
            "manual_review": "UNVERIFIED",
        })
    _write_manifest(destination, report)
    if not render:
        return report

    binaries = (soffice, pdfinfo, pdftoppm, pdffonts, pdftotext)
    if not all(shutil.which(binary) for binary in binaries):
        report["overall_status"] = "BLOCKED_VIEWER_UNAVAILABLE"
        _write_manifest(destination, report)
        return report

    pdf_dir = destination / "pdf"
    image_dir = destination / "screenshots"
    font_dir = destination / "fonts"
    for directory in (pdf_dir, image_dir, font_dir):
        directory.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="mini-deerflow-office-") as temporary:
            profile = (Path(temporary) / "lo-profile").resolve()
            paths = sorted(deck_dir.glob("*.pptx"))
            _command([
                soffice,
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless", "--convert-to", "pdf",
                "--outdir", str(pdf_dir), *map(str, paths),
            ], timeout=240)
        for entry in report["matrix"]:
            name = entry["case"]
            pdf = pdf_dir / f"{name}.pdf"
            if not pdf.is_file() or pdf.stat().st_size < 1000:
                raise ValueError("viewer did not produce PDF")
            count = _pages(_command([pdfinfo, str(pdf)]))
            if count != 8:
                raise ValueError("viewer page count mismatch")
            font_text = _command([pdffonts, str(pdf)])
            font_name = f"fonts/{name}.txt"
            (font_dir / f"{name}.txt").write_text(font_text, encoding="utf-8")
            page_text = _command([pdftotext, "-layout", "-enc", "UTF-8", str(pdf), "-"])
            _command([
                pdftoppm, "-f", "1", "-l", "8",
                "-scale-to", "1400", "-png", str(pdf), str(image_dir / name),
            ], timeout=180)
            images = sorted(
                image_dir.glob(f"{name}-*.png"),
                key=lambda p: int(p.stem.rsplit("-", 1)[1]),
            )
            if len(images) != 8:
                raise ValueError("viewer screenshot count mismatch")
            for picture in images:
                png_dimensions(picture)
                if picture.stat().st_size <= 1500:
                    raise ValueError("viewer screenshot has no useful data")
            entry.update({
                "viewer_render": "PASS",
                "rendered_pages": count,
                "screenshots": [f"screenshots/{p.name}" for p in images],
                "font_report": font_name,
                "pdf_text_extractable": _pdf_text_coverage(
                    page_text, entry["language"]
                ),
            })
            _write_manifest(destination, report)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        report["overall_status"] = "FAILED_VIEWER_PIPELINE"
        _write_manifest(destination, report)
        return report

    report["overall_status"] = "RENDERED_AWAITING_HUMAN_QA"
    _write_manifest(destination, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--soffice", default="soffice")
    args = parser.parse_args(argv)
    report = run(
        args.output_dir, render=args.render, soffice=args.soffice
    )
    print(json.dumps({
        "overall_status": report["overall_status"],
        "cases": len(report["matrix"]),
        "rendered": sum(x["rendered_pages"] for x in report["matrix"]),
        "manifest": str(args.output_dir / "manifest.json"),
        "manual_review": report["visual_reviewer_status"],
    }, sort_keys=True))
    return 0 if report["overall_status"] in {
        "BLOCKED_VIEWER_NOT_RUN", "RENDERED_AWAITING_HUMAN_QA"
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
