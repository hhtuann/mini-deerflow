"""Static slide tokens, independent of the application UI's SaaS examples."""

from dataclasses import dataclass
from types import MappingProxyType

WIDTH = 12.8
HEIGHT = 7.2
MARGIN = 0.6
GAP = 0.4
BODY_TOP = 1.7
BODY_HEIGHT = 4.4


@dataclass(frozen=True)
class Theme:
    background: str
    foreground: str
    muted: str
    accent: str
    surface: str
    font: str = "Arial"  # Helvetica / sans-serif substitution is viewer-owned.
    title_pt: int = 30
    body_pt: int = 22
    small_pt: int = 14


THEMES = MappingProxyType(
    {
        "light": Theme("F8FAFC", "0F172A", "475569", "4F46E5", "E2E8F0"),
        "dark": Theme("0F172A", "F8FAFC", "CBD5E1", "A5B4FC", "1E293B"),
        "academic": Theme("FFFCF5", "192E28", "445A50", "25634C", "E6EDE6"),
    }
)
