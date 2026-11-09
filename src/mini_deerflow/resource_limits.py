"""Closed application resource-limit identities; never raw provider data."""

from typing import Literal

ResourceLimitReason = Literal[
    "overall_deadline_exceeded",
    "synthesis_deadline_exceeded",
    "synthesis_output_limit_exceeded",
]


class ResourceLimitExceeded(RuntimeError):
    """An enforceable local wall-clock or response-size policy was exhausted."""

    def __init__(self, reason: ResourceLimitReason) -> None:
        self.reason: ResourceLimitReason = reason
        super().__init__(reason)
