from __future__ import annotations

from typing import Any

from .errors import MarketDataConfigurationError
from .psx_sqlite import PsxSqliteMarketDataProvider


def get_market_data_provider(config: Any):
    provider = (config.market_data_provider or "schwab").strip().lower()
    if provider == "schwab":
        return None
    if provider == "psx_sqlite":
        if not config.psx_db_path:
            raise MarketDataConfigurationError(
                "PSX_DB_PATH is required when MARKET_DATA_PROVIDER=psx_sqlite"
            )
        return PsxSqliteMarketDataProvider(
            config.psx_db_path, price_mode=config.psx_price_mode
        )
    raise MarketDataConfigurationError(
        f"Unknown MARKET_DATA_PROVIDER: {config.market_data_provider!r}"
    )
