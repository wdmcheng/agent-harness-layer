"""Base DTO utilities for stable boundary payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class HarnessDTO(BaseModel):
    """Base class for public boundary DTOs."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-compatible payload for API, trace, or event boundaries."""

        return self.model_dump(mode="json", exclude_none=True)
