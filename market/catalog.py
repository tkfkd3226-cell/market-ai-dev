MARKET_INSTRUMENTS = (
    {
        "symbol": "KRX:005930",
        "name": "Samsung Electronics",
        "group": "korea_semiconductor",
    },
    {
        "symbol": "KRX:000660",
        "name": "SK hynix",
        "group": "korea_semiconductor",
    },
    {
        "symbol": "NASDAQ:SKHY",
        "name": "SK hynix ADR",
        "group": "us_semiconductor",
    },
    {
        "symbol": "NASDAQ:NVDA",
        "name": "NVIDIA",
        "group": "us_semiconductor",
    },
    {
        "symbol": "NASDAQ:MU",
        "name": "Micron",
        "group": "us_semiconductor",
    },
    {
        "symbol": "INDEX:SOX",
        "name": "PHLX Semiconductor Index",
        "group": "market_index",
    },
    {
        "symbol": "FUTURES:SOX",
        "name": "E-mini PHLX Semiconductor Sector Futures",
        "group": "futures",
    },
    {
        "symbol": "INDEX:KOSPI",
        "name": "KOSPI Composite Index",
        "group": "market_index",
    },
    {
        "symbol": "FUTURES:NQ",
        "name": "Nasdaq 100 Futures",
        "group": "futures",
    },
    {
        "symbol": "FUTURES:KOSPI200",
        "name": "KOSPI 200 Futures",
        "group": "futures",
    },
    {
        "symbol": "FX:USDKRW",
        "name": "USD/KRW",
        "group": "macro",
    },
    {
        "symbol": "COMMODITY:WTI",
        "name": "WTI Crude Oil",
        "group": "macro",
    },
    {
        "symbol": "COMMODITY:BRENT",
        "name": "Brent Crude Oil",
        "group": "macro",
    },
    {
        "symbol": "RATE:US10Y",
        "name": "US 10Y Treasury Yield",
        "group": "rates",
    },
    {
        "symbol": "RATE:US30Y",
        "name": "US 30Y Treasury Yield",
        "group": "rates",
    },
)

MARKET_SYMBOLS = frozenset(item["symbol"] for item in MARKET_INSTRUMENTS)
