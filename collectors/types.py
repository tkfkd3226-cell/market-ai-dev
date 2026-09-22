from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    provider_symbol: str
    price: float
    change_pct: float | None
    observed_at: datetime
    source: str
    is_proxy: bool = False
