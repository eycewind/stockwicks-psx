from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


QUALITY_KEYS = (
    "missing_required_adjusted_field",
    "duplicate_date",
    "non_positive_ohlc",
    "non_finite_ohlcv",
    "negative_volume",
    "high_below_low",
    "open_outside_range",
    "close_outside_range",
    "missing_open_fallback",
    "missing_adj_factor",
    "non_positive_adj_factor",
    "missing_ldcp",
    "invalid_ldcp",
)


@dataclass(frozen=True)
class MarketDataRequest:
    symbol: str
    start_date: date
    end_date: date
    frequency_type: str = "daily"
    frequency: int = 1


@dataclass(frozen=True)
class AuditBar:
    trade_date: date
    raw_open: float | None
    raw_high: float | None
    raw_low: float | None
    raw_close: float | None
    raw_volume: float | None
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    adjusted_volume: float
    adj_factor: float | None
    ldcp: float | None
    open_missing: bool


@dataclass
class QualitySummary:
    symbol: str
    start_date: str
    end_date: str
    returned_bar_count: int = 0
    counts: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key in QUALITY_KEYS}
    )
    affected_dates: dict[str, list[str]] = field(
        default_factory=lambda: {key: [] for key in QUALITY_KEYS}
    )

    def record(self, classification: str, trade_date: date, sample_limit: int = 5) -> None:
        self.counts[classification] += 1
        samples = self.affected_dates[classification]
        value = trade_date.isoformat()
        if len(samples) < sample_limit and value not in samples:
            samples.append(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "returnedBarCount": self.returned_bar_count,
            "counts": dict(self.counts),
            "affectedDates": {
                key: list(values)
                for key, values in self.affected_dates.items()
                if values
            },
        }


@dataclass(frozen=True)
class MarketDataResult:
    symbol: str
    candles: list[dict[str, int | float]]
    audit_bars: list[AuditBar]
    quality: QualitySummary

    def compatibility_response(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "empty": not self.candles,
            "candles": self.candles,
            "quality": self.quality.as_dict(),
        }
