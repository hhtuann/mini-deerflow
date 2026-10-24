"""Generate and independently reopen an eight-slide Vietnamese fixture deck."""

import argparse
import json
from pathlib import Path
from tempfile import mkdtemp

from mini_deerflow.pptx import DeckSpec, render_deck, validate_pptx


def demo_spec(theme: str = "light") -> DeckSpec:
    """Illustrative fixture data, not research findings or measured benchmarks."""
    contents = [
        (
            "intro",
            "Báo cáo minh họa: Nghiên cứu có cấu trúc",
            {
                "kind": "title",
                "subtitle": "Mini DeerFlow — PowerPoint chỉnh sửa được, hoàn toàn ngoại tuyến",
            },
        ),
        (
            "goals",
            "Mục tiêu và phạm vi",
            {
                "kind": "bullets",
                "items": [
                    "Giữ nguyên dấu tiếng Việt và nội dung gốc",
                    "Văn bản, hình khối, bảng và biểu đồ đều chỉnh sửa được",
                    "Dữ liệu minh họa, không phải kết quả nghiên cứu thực tế",
                ],
            },
        ),
        (
            "compare",
            "So sánh cách trình bày",
            {
                "kind": "comparison",
                "left": {
                    "heading": "Ảnh tĩnh",
                    "items": ["Khó sửa từng thành phần", "Dữ liệu không có cấu trúc"],
                },
                "right": {
                    "heading": "Đối tượng gốc",
                    "items": [
                        "Chỉnh sửa văn bản trực tiếp",
                        "Biểu đồ có dữ liệu nhúng",
                    ],
                },
            },
        ),
        (
            "metric",
            "Chỉ số minh họa",
            {
                "kind": "metric",
                "value": "8",
                "label": "Trang trình chiếu có nội dung",
                "insight": "Đây là số trang của bộ mẫu, không phải chỉ số hiệu năng.",
            },
        ),
        (
            "steps",
            "Quy trình dựng bản trình chiếu",
            {
                "kind": "steps",
                "items": [
                    "Kiểm tra hợp đồng DeckSpec",
                    "Áp dụng bố cục và chủ đề",
                    "Dựng đối tượng PowerPoint gốc",
                    "Mở lại và kiểm tra độc lập",
                ],
            },
        ),
        (
            "chart",
            "Biểu đồ — dữ liệu giả lập",
            {
                "kind": "chart",
                "chart_type": "column",
                "categories": ["Tháng 1", "Tháng 2", "Tháng 3"],
                "series": [
                    {"name": "Mẫu A", "values": [12, 18, 24]},
                    {"name": "Mẫu B", "values": [10, 16, 21]},
                ],
                "caption": "Nguồn: dữ liệu fixture minh họa, đơn vị: lượt; không phản ánh thực tế.",
            },
        ),
        (
            "table",
            "Bảng kiểm tra khả năng chỉnh sửa",
            {
                "kind": "table",
                "headers": ["Thành phần", "Số lượng", "Trạng thái"],
                "rows": [
                    ["Biểu đồ gốc", 1, "Có dữ liệu nhúng"],
                    ["Bảng gốc", 1, "Sửa từng ô"],
                    ["Trang minh họa", 8, "Mở lại thành công"],
                ],
                "caption": "Nguồn: cấu trúc bộ mẫu ngoại tuyến.",
            },
        ),
        (
            "summary",
            "Kết luận và nguồn",
            {
                "kind": "conclusion",
                "items": [
                    "Tạo tệp PPTX thật với đối tượng chỉnh sửa được",
                    "Kiểm tra cấu trúc không thay thế kiểm tra hiển thị",
                ],
                "sources": [
                    "Nguồn: fixture nội bộ Mini DeerFlow (dữ liệu giả lập).",
                    "Thư viện: python-pptx; không sử dụng tài nguyên tải từ mạng.",
                ],
            },
        ),
    ]
    return DeckSpec.model_validate(
        {
            "title": "Mini DeerFlow — Minh họa ngoại tuyến",
            "theme": theme,
            "slides": [
                {"id": id_, "title": title, "content": content}
                for id_, title, content in contents
            ],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Caller-owned output directory; default: persistent temp directory",
    )
    parser.add_argument(
        "--theme", choices=("light", "dark", "academic"), default="light"
    )
    args = parser.parse_args()
    directory = args.output_dir or Path(mkdtemp(prefix="mini-deerflow-pptx-"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "phase2-offline.pptx"
    # Exclusive creation refuses overwrites and existing symlinks. Directory is
    # a trusted local CLI choice, not an agent-controlled workspace path.
    spec = demo_spec(args.theme)
    data = render_deck(spec)
    validate_pptx(data, spec, min_layouts=4)
    with path.open("xb") as f:
        f.write(data)
    report = validate_pptx(path, spec, min_layouts=4)
    print(
        json.dumps(
            {"file": str(path.resolve()), **report.to_dict()},
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
