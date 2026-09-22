from dataclasses import dataclass


@dataclass(frozen=True)
class YahooMapping:
    symbol: str
    provider_symbol: str | None
    market_timezone: str
    is_proxy: bool = False
    note: str | None = None


YAHOO_MAPPINGS = (
    YahooMapping("KRX:005930", "005930.KS", "Asia/Seoul"),
    YahooMapping("KRX:000660", "000660.KS", "Asia/Seoul"),
    YahooMapping("NASDAQ:SKHY", "SKHY", "America/New_York"),
    YahooMapping("NASDAQ:NVDA", "NVDA", "America/New_York"),
    YahooMapping("NASDAQ:MU", "MU", "America/New_York"),
    YahooMapping("INDEX:SOX", "^SOX", "America/New_York"),
    YahooMapping("FUTURES:SOX", "SOX=F", "America/Chicago"),
    YahooMapping("INDEX:KOSPI", "^KS11", "Asia/Seoul"),
    YahooMapping("FUTURES:NQ", "NQ=F", "America/Chicago"),
    YahooMapping(
        "FUTURES:KOSPI200",
        None,
        "Asia/Seoul",
        note="No reliable Yahoo Finance KOSPI 200 futures ticker is configured.",
    ),
    YahooMapping("FX:USDKRW", "KRW=X", "UTC"),
    YahooMapping("COMMODITY:WTI", "CL=F", "America/Chicago"),
    YahooMapping("COMMODITY:BRENT", "BZ=F", "America/Chicago"),
    YahooMapping("RATE:US10Y", "^TNX", "America/New_York"),
    YahooMapping("RATE:US30Y", "^TYX", "America/New_York"),
)


def get_yahoo_mappings(*, allow_kospi200_index_proxy: bool) -> tuple[YahooMapping, ...]:
    if not allow_kospi200_index_proxy:
        return YAHOO_MAPPINGS

    return tuple(
        YahooMapping(
            symbol=item.symbol,
            provider_symbol="^KS200",
            market_timezone="Asia/Seoul",
            is_proxy=True,
            note="KOSPI 200 spot index proxy; this is not a futures quote.",
        )
        if item.symbol == "FUTURES:KOSPI200"
        else item
        for item in YAHOO_MAPPINGS
    )
