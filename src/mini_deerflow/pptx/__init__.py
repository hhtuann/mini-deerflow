"""Deterministic, offline, editable presentation engine (no agent dependencies)."""

from mini_deerflow.pptx.renderer import render_deck
from mini_deerflow.pptx.schemas import DeckSpec, SlideSpec
from mini_deerflow.pptx.slide_agent import (
    PresentationRequest,
    SlideAgentService,
    plan_deck,
)
from mini_deerflow.pptx.validator import PptxValidationError, validate_pptx

__all__ = [
    "DeckSpec",
    "PptxValidationError",
    "PresentationRequest",
    "SlideAgentService",
    "SlideSpec",
    "plan_deck",
    "render_deck",
    "validate_pptx",
]
