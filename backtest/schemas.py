from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MarketOutcomeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    session_date: date
    source: str = Field(min_length=1, max_length=80)
    source_reference: str | None = Field(default=None, max_length=500)
    kospi_prev_close: float = Field(gt=0)
    kospi_open: float = Field(gt=0)
    kospi_close: float = Field(gt=0)
    semiconductor_return_pct: float | None = Field(default=None, ge=-50.0, le=50.0)
    is_final: bool = True
    details: dict[str, Any] = Field(default_factory=dict)
