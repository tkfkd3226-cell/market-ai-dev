from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from yfinance.data import YfData

from collectors.types import MarketObservation
from market.provider_map import YahooMapping


class YFinanceCollector:
    """Zero-key development feed backed by Yahoo Finance via yfinance."""

    source_name = "yfinance"

    def __init__(self, mappings: tuple[YahooMapping, ...], *, timeout_seconds: int = 15):
        self.mappings = mappings
        self.timeout_seconds = timeout_seconds

    @property
    def enabled_mappings(self) -> tuple[YahooMapping, ...]:
        return tuple(item for item in self.mappings if item.provider_symbol)

    @property
    def disabled_mappings(self) -> tuple[YahooMapping, ...]:
        return tuple(item for item in self.mappings if not item.provider_symbol)

    def fetch(self) -> tuple[list[MarketObservation], dict[str, str]]:
        enabled = self.enabled_mappings
        provider_symbols = [item.provider_symbol for item in enabled if item.provider_symbol]
        if not provider_symbols:
            return [], {}

        intraday = yf.download(
            tickers=provider_symbols,
            period="5d",
            interval="1m",
            group_by="ticker",
            auto_adjust=False,
            prepost=True,
            threads=True,
            progress=False,
            timeout=self.timeout_seconds,
            ignore_tz=False,
            multi_level_index=True,
        )
        daily = yf.download(
            tickers=provider_symbols,
            period="10d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            prepost=False,
            threads=True,
            progress=False,
            timeout=self.timeout_seconds,
            ignore_tz=True,
            multi_level_index=True,
        )

        observations: list[MarketObservation] = []
        errors: dict[str, str] = {}
        futures_symbols = [
            item.provider_symbol for item in enabled if self._uses_futures_quote(item)
        ]
        futures_quotes: dict[str, dict] = {}
        quote_error = None
        if futures_symbols:
            try:
                futures_quotes = self._fetch_futures_quotes(futures_symbols)
            except Exception as exc:
                # A quote failure must not block cash indices/stocks or force a
                # futures percentage calculated from a different daily session.
                quote_error = f"futures quote unavailable: {exc}"

        for mapping in enabled:
            assert mapping.provider_symbol is not None
            try:
                observation = self._build_observation(
                    mapping, intraday, daily,
                    futures_quote=futures_quotes.get(mapping.provider_symbol),
                )
            except Exception as exc:  # Provider failures should not stop other symbols.
                errors[mapping.symbol] = str(exc)
                continue

            if observation is None:
                errors[mapping.symbol] = "no price data returned"
                continue

            observations.append(observation)
            if self._uses_futures_quote(mapping) and observation.change_pct is None:
                errors[mapping.symbol] = quote_error or (
                    "futures quote price/time or regularMarketPreviousClose unavailable/stale; "
                    "price only, change_pct omitted"
                )

        return observations, errors

    @staticmethod
    def _uses_futures_quote(mapping: YahooMapping) -> bool:
        # The optional KOSPI200 cash-index proxy keeps its existing semantics.
        return mapping.symbol.startswith("FUTURES:") and not mapping.is_proxy

    def _fetch_futures_quotes(self, symbols: list[str]) -> dict[str, dict]:
        # Same authenticated quote endpoint used by yfinance's Quote scraper.
        # Batch it once with the configured timeout instead of fetching each
        # Ticker.info (which also requests unrelated fundamentals).
        payload = YfData().get_raw_json(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": ",".join(symbols), "formatted": "false"},
            timeout=self.timeout_seconds,
        )
        response = payload.get("quoteResponse") or {}
        if response.get("error"):
            raise ValueError("Yahoo futures quote response contains an error")
        return {
            row["symbol"]: row
            for row in response.get("result", [])
            if isinstance(row, dict) and row.get("symbol") in symbols
        }

    @staticmethod
    def _positive_number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @classmethod
    def _futures_quote_values(
        cls, mapping: YahooMapping, quote: dict | None,
    ) -> tuple[float, datetime, float | None] | None:
        if not quote or quote.get("symbol") != mapping.provider_symbol:
            return None
        if quote.get("quoteType") != "FUTURE":
            return None
        price = cls._positive_number(quote.get("regularMarketPrice"))
        timestamp = cls._positive_number(quote.get("regularMarketTime"))
        if price is None or timestamp is None:
            return None
        try:
            observed_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
        previous_close = cls._positive_number(quote.get("regularMarketPreviousClose"))
        # Never substitute previousClose, fast_info, chartPreviousClose or a
        # daily Close: their session/range meaning need not match this quote.
        return price, observed_at, previous_close

    def _build_observation(
        self,
        mapping: YahooMapping,
        intraday: pd.DataFrame,
        daily: pd.DataFrame,
        *,
        futures_quote: dict | None = None,
    ) -> MarketObservation | None:
        provider_symbol = mapping.provider_symbol
        if provider_symbol is None:
            return None

        intraday_frame = self._ticker_frame(intraday, provider_symbol)
        close_series = (
            intraday_frame["Close"].dropna()
            if "Close" in intraday_frame else pd.Series(dtype=float)
        )
        latest_price = None
        observed_at = None
        if not close_series.empty:
            latest_price = self._positive_number(close_series.iloc[-1])
            observed_at = self._to_utc_datetime(close_series.index[-1])

        previous_close = None
        if self._uses_futures_quote(mapping):
            quote_values = self._futures_quote_values(mapping, futures_quote)
            if quote_values is not None:
                quote_price, quote_time, quote_close = quote_values
                if observed_at is None or quote_time >= observed_at:
                    # All three values come from one response. Do not combine a
                    # new bar/rolled contract price with an older quote's base.
                    latest_price, observed_at, previous_close = quote_price, quote_time, quote_close
        elif latest_price is not None and observed_at is not None:
            daily_frame = self._ticker_frame(daily, provider_symbol)
            previous_close = self._previous_close(
                daily_frame,
                intraday_frame=intraday_frame if mapping.symbol.startswith("INDEX:") else None,
                observed_at=observed_at,
                market_timezone=mapping.market_timezone,
            )
        if latest_price is None or observed_at is None:
            return None

        change_pct = None
        if previous_close not in (None, 0.0):
            change_pct = ((latest_price / previous_close) - 1.0) * 100.0

        source = f"{self.source_name}:{provider_symbol}"
        if mapping.is_proxy:
            source += ":proxy"

        return MarketObservation(
            symbol=mapping.symbol,
            provider_symbol=provider_symbol,
            price=latest_price,
            change_pct=change_pct,
            observed_at=observed_at,
            source=source,
            is_proxy=mapping.is_proxy,
        )

    @staticmethod
    def _ticker_frame(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame()

        if isinstance(frame.columns, pd.MultiIndex):
            level_0 = frame.columns.get_level_values(0)
            if ticker in level_0:
                result = frame[ticker]
                return result if isinstance(result, pd.DataFrame) else result.to_frame()

            level_1 = frame.columns.get_level_values(1)
            if ticker in level_1:
                result = frame.xs(ticker, axis=1, level=1)
                return result if isinstance(result, pd.DataFrame) else result.to_frame()

            return pd.DataFrame(index=frame.index)

        # yfinance may flatten columns when only one symbol is requested.
        return frame

    @staticmethod
    def _to_utc_datetime(value: object) -> datetime:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize(timezone.utc)
        else:
            timestamp = timestamp.tz_convert(timezone.utc)
        return timestamp.to_pydatetime()

    @staticmethod
    def _previous_close(
        daily_frame: pd.DataFrame,
        *,
        intraday_frame: pd.DataFrame | None = None,
        observed_at: datetime,
        market_timezone: str,
    ) -> float | None:
        local_date = observed_at.astimezone(ZoneInfo(market_timezone)).date()

        daily_date: object | None = None
        daily_close: float | None = None
        if not daily_frame.empty and "Close" in daily_frame:
            closes = daily_frame["Close"].dropna()
            for index, value in reversed(list(closes.items())):
                candidate_date = pd.Timestamp(index).date()
                if candidate_date < local_date:
                    daily_date = candidate_date
                    daily_close = float(value)
                    break

        if intraday_frame is not None:
            intraday_candidate = YFinanceCollector._previous_intraday_session_close(
                intraday_frame,
                observed_at=observed_at,
                market_timezone=market_timezone,
            )
            if intraday_candidate is not None:
                intraday_date, intraday_close = intraday_candidate
                # Yahoo's batched daily feed can occasionally lag by one session.
                # For cash indices, use the newer completed intraday session only
                # when it is strictly newer than the daily candidate.
                if daily_date is None or intraday_date > daily_date:
                    return intraday_close

        return daily_close

    @staticmethod
    def _previous_intraday_session_close(
        intraday_frame: pd.DataFrame,
        *,
        observed_at: datetime,
        market_timezone: str,
    ) -> tuple[object, float] | None:
        if intraday_frame.empty or "Close" not in intraday_frame:
            return None

        closes = intraday_frame["Close"].dropna()
        if closes.empty:
            return None

        market_tz = ZoneInfo(market_timezone)
        local_date = observed_at.astimezone(market_tz).date()

        index = pd.DatetimeIndex(closes.index)
        if index.tz is None:
            index = index.tz_localize(timezone.utc)
        local_index = index.tz_convert(market_tz)

        candidate_positions = [
            position
            for position, timestamp in enumerate(local_index)
            if timestamp.date() < local_date
        ]
        if not candidate_positions:
            return None

        previous_session_date = max(local_index[position].date() for position in candidate_positions)
        previous_session_positions = [
            position
            for position in candidate_positions
            if local_index[position].date() == previous_session_date
        ]
        if not previous_session_positions:
            return None

        last_position = previous_session_positions[-1]
        return previous_session_date, float(closes.iloc[last_position])
