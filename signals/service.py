import asyncio
from datetime import datetime, timezone

from db.database import SessionLocal
from db.signal_repository import save_signal_run
from calibration.service import apply_active_models_to_signal, serialize_signal_calibration

from .engine import build_signal


class SignalService:
    def __init__(
        self,
        *,
        enabled: bool,
        poll_seconds: int,
        news_lookback_hours: int,
        minimum_data_weight: float,
        ai_news_active: bool,
    ) -> None:
        self.enabled = enabled
        self.poll_seconds = poll_seconds
        self.news_lookback_hours = news_lookback_hours
        self.minimum_data_weight = minimum_data_weight
        self.ai_news_active = ai_news_active
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None
        self.last_saved = False
        self.last_reason: str | None = None
        self.last_signal: dict[str, object] | None = None

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="market-ai-signal-engine")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run_once(self) -> dict[str, object]:
        async with self._run_lock:
            self.last_started_at = datetime.now(timezone.utc)
            self.last_finished_at = None
            self.last_error = None
            self.last_saved = False
            self.last_reason = None
            try:
                saved = await asyncio.to_thread(self._calculate_and_save)
                if saved is None:
                    self.last_reason = "insufficient market data for configured minimum weight"
                else:
                    result, calibration_payload = saved
                    self.last_saved = True
                    self.last_signal = {
                        "kospi_score": result.kospi_score,
                        "semiconductor_score": result.semiconductor_score,
                        "gap_up_probability": result.gap_up_probability,
                        "up_close_probability": result.up_close_probability,
                        "confidence": result.confidence,
                        "data_completeness": result.data_completeness,
                        "calibrated": result.calibrated,
                        "calibration": calibration_payload,
                    }
            except Exception as exc:
                self.last_error = str(exc)
            finally:
                self.last_finished_at = datetime.now(timezone.utc)
            return self.status()

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
            "poll_seconds": self.poll_seconds,
            "news_lookback_hours": self.news_lookback_hours,
            "minimum_data_weight": self.minimum_data_weight,
            "ai_news_active": self.ai_news_active,
            "last_started_at": self._iso_or_none(self.last_started_at),
            "last_finished_at": self._iso_or_none(self.last_finished_at),
            "last_error": self.last_error,
            "last_saved": self.last_saved,
            "last_reason": self.last_reason,
            "last_signal": self.last_signal,
        }

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    def _calculate_and_save(self):
        with SessionLocal() as session:
            result = build_signal(
                session,
                news_lookback_hours=self.news_lookback_hours,
                minimum_data_weight=self.minimum_data_weight,
                ai_news_active=self.ai_news_active,
            )
            if result is None:
                return None
            row = save_signal_run(session, result)
            calibration = apply_active_models_to_signal(session, row)
            if calibration is not None:
                session.commit()
                session.refresh(calibration)
            return result, serialize_signal_calibration(calibration)

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
