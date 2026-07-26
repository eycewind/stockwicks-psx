class MarketDataError(RuntimeError):
    """Base class for market-data failures."""


class MarketDataConfigurationError(MarketDataError):
    pass


class MarketDataRequestError(MarketDataError):
    pass


class UnsupportedMarketDataInterval(MarketDataRequestError):
    pass


class UnknownSymbolError(MarketDataRequestError):
    pass


class MarketDataQualityError(MarketDataError):
    def __init__(self, message: str, quality: dict | None = None):
        super().__init__(message)
        self.quality = quality


class MarketDataProviderError(MarketDataError):
    pass
