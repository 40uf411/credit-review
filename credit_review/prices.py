"""What a model call costs, from the token usage every call already records.

Prices live in `config/model_prices.yaml`, keyed by model or deployment name, in EUR per million
tokens. A model that is not listed has no cost (None), which reports show as "n/a" rather than 0.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

DEFAULT_PRICES = Path(__file__).resolve().parents[1] / "config" / "model_prices.yaml"


class Price(BaseModel):
    input: float                 # EUR per 1M input tokens
    output: float                # EUR per 1M output tokens


def load_prices(path: str | Path | None = None) -> dict[str, Price]:
    return _load(str(path or os.environ.get("CREDIT_REVIEW_PRICES_FILE") or DEFAULT_PRICES))


@lru_cache(maxsize=8)
def _load(path: str) -> dict[str, Price]:
    file = Path(path)
    if not file.exists():
        return {}
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return {name: Price.model_validate(p) for name, p in (data.get("models") or {}).items()}


def cost_eur(model: str, usage: dict[str, Any], prices: dict[str, Price] | None = None) -> float | None:
    """Cost of one call; None when the model has no price or the usage was not reported."""
    price = (load_prices() if prices is None else prices).get(model)
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if price is None or tokens_in is None or tokens_out is None:
        return None
    return round((tokens_in * price.input + tokens_out * price.output) / 1_000_000, 6)
