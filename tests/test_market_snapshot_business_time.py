from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from db.market_repository import save_market_observation
from db.migrations import migrate_market_snapshot_business_time, migrate_market_snapshot_change_amount


class MarketSnapshotBusinessTimeMigrationTests(unittest.TestCase):
    def test_existing_runtime_db_is_upgraded_and_preserves_exchange_time(self):
        engine = create_engine("sqlite:///:memory:")
        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE market_snapshot (
                        symbol VARCHAR(40) PRIMARY KEY,
                        observed_at DATETIME NOT NULL,
                        price FLOAT NOT NULL,
                        change_pct FLOAT,
                        source VARCHAR(64) NOT NULL
                    )
                    """
                )
            )

        with Session() as session:
            migrate_market_snapshot_business_time(session)
            migrate_market_snapshot_change_amount(session)
            columns = {column["name"] for column in inspect(engine).get_columns("market_snapshot")}
            self.assertIn("business_time", columns)
            self.assertIn("change_amount", columns)

            save_market_observation(
                session,
                symbol="KRX:123456",
                price=12345,
                change_amount=150.0,
                change_pct=1.25,
                source="kis-efriend:SC_R:123456",
                observed_at=datetime(2026, 9, 11, 6, 29, 59, tzinfo=timezone.utc),
                business_time="152959",
                write_history=False,
            )
            stored = session.execute(
                text("SELECT business_time, change_amount FROM market_snapshot WHERE symbol = :symbol"),
                {"symbol": "KRX:123456"},
            ).one()
            self.assertEqual(stored.business_time, "152959")
            self.assertEqual(stored.change_amount, 150.0)

    def test_invalid_business_time_is_not_persisted(self):
        engine = create_engine("sqlite:///:memory:")
        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE market_snapshot (
                        symbol VARCHAR(40) PRIMARY KEY,
                        observed_at DATETIME NOT NULL,
                        price FLOAT NOT NULL,
                        change_pct FLOAT,
                        change_amount FLOAT,
                        source VARCHAR(64) NOT NULL,
                        business_time VARCHAR(6)
                    )
                    """
                )
            )

        with Session() as session:
            save_market_observation(
                session,
                symbol="INDEX:KOSPI",
                price=3000,
                change_pct=0.1,
                source="kis-efriend:JUC_R:0001",
                observed_at=datetime(2026, 9, 11, 6, 29, 59, tzinfo=timezone.utc),
                business_time="888888",
                write_history=False,
            )
            stored = session.execute(
                text("SELECT business_time FROM market_snapshot WHERE symbol = :symbol"),
                {"symbol": "INDEX:KOSPI"},
            ).scalar_one()
            self.assertIsNone(stored)



def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 15, hour, minute, tzinfo=timezone.utc)


def test_atomic_snapshot_upsert_rejects_stale_fallback_after_other_session_commits(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}")
    from db.models import MarketSnapshot

    MarketSnapshot.__table__.create(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as seed:
        save_market_observation(
            seed,
            symbol="INDEX:KOSPI",
            price=100.0,
            change_pct=0.0,
            source="kis-efriend:JUC_R:0001",
            observed_at=_utc(1),
            write_history=False,
        )

    with Session() as stale_session:
        stale_snapshot = stale_session.get(MarketSnapshot, "INDEX:KOSPI")
        assert stale_snapshot is not None and stale_snapshot.price == 100.0
        stale_session.commit()

        with Session() as fresh_session:
            save_market_observation(
                fresh_session,
                symbol="INDEX:KOSPI",
                price=120.0,
                change_pct=1.2,
                source="kis-efriend:JUC_R:0001",
                observed_at=_utc(3),
                write_history=False,
            )

        save_market_observation(
            stale_session,
            symbol="INDEX:KOSPI",
            price=110.0,
            change_pct=0.5,
            source="yfinance:^KS11",
            observed_at=_utc(2),
            write_history=False,
        )

    with Session() as verify:
        snapshot = verify.scalar(select(MarketSnapshot).where(MarketSnapshot.symbol == "INDEX:KOSPI"))
        assert snapshot is not None
        assert snapshot.price == 120.0
        assert snapshot.source == "kis-efriend:JUC_R:0001"
        assert snapshot.observed_at.replace(tzinfo=timezone.utc) == _utc(3)


def test_same_timestamp_yfinance_does_not_replace_verified_kis_snapshot(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'priority.db').as_posix()}")
    from db.models import MarketSnapshot

    MarketSnapshot.__table__.create(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as session:
        save_market_observation(
            session,
            symbol="KRX:005930",
            price=80000.0,
            change_pct=1.0,
            source="kis-efriend:SC_R:005930",
            observed_at=_utc(4),
            write_history=False,
        )
        save_market_observation(
            session,
            symbol="KRX:005930",
            price=79900.0,
            change_pct=0.8,
            source="yfinance:005930.KS",
            observed_at=_utc(4),
            write_history=False,
        )
        snapshot = session.get(MarketSnapshot, "KRX:005930")
        session.refresh(snapshot)
        assert snapshot.price == 80000.0
        assert snapshot.source == "kis-efriend:SC_R:005930"


if __name__ == "__main__":
    unittest.main()
