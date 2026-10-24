"""Closed presentation contracts; tuple order is slide/content order.

Text is literal, never HTML, a filename, an expression, or a network resource.
Budgets deliberately favor readable slides over arbitrary document density.
"""

import unicodedata
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _xml_text(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    if any(
        unicodedata.category(c) in {"Cc", "Cs"} or ord(c) in {0xFFFE, 0xFFFF}
        for c in value
    ):
        raise ValueError(
            "text must contain XML-safe Unicode without control characters"
        )
    return value


Text = Annotated[str, Field(min_length=1, max_length=180), AfterValidator(_xml_text)]
Title = Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_xml_text)]
Label = Annotated[str, Field(min_length=1, max_length=36), AfterValidator(_xml_text)]
Cell = Annotated[str, Field(min_length=1, max_length=48), AfterValidator(_xml_text)]
Number = Annotated[
    float,
    Field(strict=True, ge=-1_000_000_000, le=1_000_000_000, allow_inf_nan=False),
    # Canonical Excel precision is part of the validated contract.
    AfterValidator(lambda value: float(format(value, ".15g"))),
]
Items = Annotated[tuple[Text, ...], Field(min_length=1, max_length=6)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TitleContent(Contract):
    kind: Literal["title"] = "title"
    subtitle: Text


class BulletsContent(Contract):
    kind: Literal["bullets"] = "bullets"
    items: Items


class Column(Contract):
    heading: Label
    items: Annotated[tuple[Text, ...], Field(min_length=1, max_length=4)]


class ComparisonContent(Contract):
    kind: Literal["comparison"] = "comparison"
    left: Column
    right: Column


class MetricContent(Contract):
    kind: Literal["metric"] = "metric"
    value: Label
    label: Title
    insight: Text


class StepsContent(Contract):
    kind: Literal["steps"] = "steps"
    items: Annotated[tuple[Title, ...], Field(min_length=2, max_length=5)]


class ChartSeries(Contract):
    name: Label
    values: Annotated[tuple[Number, ...], Field(min_length=1, max_length=8)]


class ChartContent(Contract):
    kind: Literal["chart"] = "chart"
    chart_type: Literal["column", "bar", "line"] = "column"
    categories: Annotated[tuple[Label, ...], Field(min_length=1, max_length=8)]
    series: Annotated[tuple[ChartSeries, ...], Field(min_length=1, max_length=3)]
    caption: Text

    @model_validator(mode="after")
    def matching_series(self) -> Self:
        if any(len(s.values) != len(self.categories) for s in self.series):
            raise ValueError("chart series must match category count")
        if len({s.name for s in self.series}) != len(self.series):
            raise ValueError("chart series names must be unique")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("chart categories must be unique")
        return self


class TableContent(Contract):
    kind: Literal["table"] = "table"
    headers: Annotated[tuple[Cell, ...], Field(min_length=1, max_length=5)]
    rows: Annotated[
        tuple[tuple[Cell | Number, ...], ...], Field(min_length=1, max_length=8)
    ]
    caption: Text

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("table rows must match header count")
        return self


class ConclusionContent(Contract):
    kind: Literal["conclusion"] = "conclusion"
    items: Annotated[tuple[Text, ...], Field(min_length=1, max_length=3)]
    sources: Annotated[tuple[Text, ...], Field(min_length=1, max_length=3)]


Content = Annotated[
    TitleContent
    | BulletsContent
    | ComparisonContent
    | MetricContent
    | StepsContent
    | ChartContent
    | TableContent
    | ConclusionContent,
    Field(discriminator="kind"),
]


class SlideSpec(Contract):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
    title: Title
    content: Content


class DeckSpec(Contract):
    title: Title
    aspect_ratio: Literal["16:9"] = "16:9"
    theme: Literal["light", "dark", "academic"] = "light"
    slides: Annotated[tuple[SlideSpec, ...], Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({s.id for s in self.slides}) != len(self.slides):
            raise ValueError("slide IDs must be unique")
        return self
