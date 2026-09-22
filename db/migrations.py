from datetime import timezone

from sqlalchemy import inspect, text, update
from sqlalchemy.orm import Session

from .models import MarketPrice, MarketSnapshot


LEGACY_SKHY_SYMBOL = "OTC:SKHY"
CURRENT_SKHY_SYMBOL = "NASDAQ:SKHY"


def migrate_stage3_symbols(session: Session) -> None:
    """Keep Stage 2 data if the pre-Nasdaq SKHY internal symbol already exists."""
    session.execute(
        update(MarketPrice)
        .where(MarketPrice.symbol == LEGACY_SKHY_SYMBOL)
        .values(symbol=CURRENT_SKHY_SYMBOL)
    )

    legacy = session.get(MarketSnapshot, LEGACY_SKHY_SYMBOL)
    current = session.get(MarketSnapshot, CURRENT_SKHY_SYMBOL)

    if legacy is not None:
        if current is None:
            session.add(
                MarketSnapshot(
                    symbol=CURRENT_SKHY_SYMBOL,
                    observed_at=legacy.observed_at,
                    price=legacy.price,
                    change_pct=legacy.change_pct,
                    source=legacy.source,
                )
            )
        else:
            legacy_time = legacy.observed_at
            current_time = current.observed_at
            if legacy_time.tzinfo is None:
                legacy_time = legacy_time.replace(tzinfo=timezone.utc)
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)

            if legacy_time > current_time:
                current.observed_at = legacy.observed_at
                current.price = legacy.price
                current.change_pct = legacy.change_pct
                current.source = legacy.source

        session.delete(legacy)

    session.commit()


def migrate_market_snapshot_business_time(session: Session) -> None:
    """Add durable exchange/business time to existing SQLite runtime databases."""
    bind = session.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("market_snapshot")}
    if "business_time" not in columns:
        session.execute(text("ALTER TABLE market_snapshot ADD COLUMN business_time VARCHAR(6)"))
        session.commit()


def migrate_market_snapshot_change_amount(session: Session) -> None:
    """Add durable KIS previous-close amount to existing SQLite runtime databases."""
    bind = session.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("market_snapshot")}
    if "change_amount" not in columns:
        session.execute(text("ALTER TABLE market_snapshot ADD COLUMN change_amount FLOAT"))
        session.commit()

