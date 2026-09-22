from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import MarketPrice, MarketSnapshot


def save_market_observation(
    session: Session,
    *,
    symbol: str,
    price: float,
    change_pct: float | None,
    change_amount: float | None = None,
    source: str,
    observed_at: datetime | None = None,
    business_time: str | None = None,
    write_history: bool = True,
) -> MarketPrice | None:
    normalized_symbol = symbol.strip()
    normalized_source = source.strip()
    normalized_business_time = None
    if business_time is not None:
        candidate = str(business_time).strip()
        if (
            len(candidate) == 6
            and candidate.isdigit()
            and 0 <= int(candidate[0:2]) <= 23
            and 0 <= int(candidate[2:4]) <= 59
            and 0 <= int(candidate[4:6]) <= 59
        ):
            normalized_business_time = candidate

    if not normalized_symbol:
        raise ValueError("symbol is required")
    if not normalized_source:
        raise ValueError("source is required")

    timestamp = observed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    history_row: MarketPrice | None = None
    if write_history:
        history_row = MarketPrice(
            observed_at=timestamp,
            symbol=normalized_symbol,
            price=float(price),
            change_pct=None if change_pct is None else float(change_pct),
            source=normalized_source,
        )
        session.add(history_row)

    # Snapshot freshness must be decided atomically in SQLite.  Comparing against
    # a MarketSnapshot ORM object that this Session loaded earlier leaves a race:
    # another Session can commit a newer KIS tick after our read and an older
    # fallback observation can then overwrite it.  ON CONFLICT ... DO UPDATE
    # keeps the freshness/source guard and the write in one database statement.
    snapshot_table = MarketSnapshot.__table__
    insert_stmt = sqlite_insert(snapshot_table).values(
        symbol=normalized_symbol,
        observed_at=timestamp,
        price=float(price),
        change_amount=None if change_amount is None else float(change_amount),
        change_pct=None if change_pct is None else float(change_pct),
        source=normalized_source,
        business_time=normalized_business_time,
    )
    excluded = insert_stmt.excluded
    proxy_cannot_replace_verified_kis = and_(
        snapshot_table.c.symbol == "FUTURES:KOSPI200",
        snapshot_table.c.source.like("kis-efriend:%"),
        excluded.source.like("%:proxy%"),
    )
    lower_priority_same_time = and_(
        excluded.observed_at == snapshot_table.c.observed_at,
        snapshot_table.c.source.like("kis-efriend:%"),
        excluded.source.like("yfinance:%"),
    )
    snapshot_upsert = insert_stmt.on_conflict_do_update(
        index_elements=[snapshot_table.c.symbol],
        set_={
            "observed_at": excluded.observed_at,
            "price": excluded.price,
            "change_amount": excluded.change_amount,
            "change_pct": excluded.change_pct,
            "source": excluded.source,
            "business_time": excluded.business_time,
        },
        where=and_(
            excluded.observed_at >= snapshot_table.c.observed_at,
            ~proxy_cannot_replace_verified_kis,
            ~lower_priority_same_time,
        ),
    )
    session.execute(snapshot_upsert)

    session.commit()
    if history_row is not None:
        session.refresh(history_row)
    return history_row


def get_market_snapshot(session: Session) -> list[MarketSnapshot]:
    return list(
        session.scalars(select(MarketSnapshot).order_by(MarketSnapshot.symbol)).all()
    )


def get_market_history(
    session: Session,
    symbol: str,
    *,
    limit: int = 50,
) -> list[MarketPrice]:
    statement = (
        select(MarketPrice)
        .where(MarketPrice.symbol == symbol)
        .order_by(MarketPrice.observed_at.desc(), MarketPrice.id.desc())
        .limit(limit)
    )
    return list(session.scalars(statement).all())
