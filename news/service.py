import asyncio
from datetime import datetime, timezone

from db.database import SessionLocal
from db.news_repository import save_news_candidate

from .gdelt_collector import GDELTNewsCollector
from .types import NewsCandidate


class NewsCollectorService:
    def __init__(
        self,
        provider: GDELTNewsCollector,
        *,
        enabled: bool,
        poll_seconds: int,
    ) -> None:
        self.provider = provider
        self.enabled = enabled
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None
        self.last_topic_errors: dict[str, str] = {}
        self.last_fetched = 0
        self.last_new_articles = 0
        self.last_new_topic_links = 0
        self.last_duplicates = 0

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="market-ai-news-collector")

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
            self.last_error = None
            self.last_topic_errors = {}
            self.last_fetched = 0
            self.last_new_articles = 0
            self.last_new_topic_links = 0
            self.last_duplicates = 0

            try:
                items, topic_errors = await asyncio.to_thread(self.provider.fetch)
                self.last_topic_errors = topic_errors
                self.last_fetched = len(items)
                stats = await asyncio.to_thread(self._persist, items)
                self.last_new_articles = stats["new_articles"]
                self.last_new_topic_links = stats["new_topic_links"]
                self.last_duplicates = stats["duplicates"]
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
            "provider": self.provider.source_name,
            "topic_count": len(self.provider.topics),
            "lookback_hours": self.provider.lookback_hours,
            "max_records_per_topic": self.provider.max_records_per_topic,
            "last_started_at": self._iso_or_none(self.last_started_at),
            "last_finished_at": self._iso_or_none(self.last_finished_at),
            "last_error": self.last_error,
            "last_topic_errors": self.last_topic_errors,
            "last_fetched": self.last_fetched,
            "last_new_articles": self.last_new_articles,
            "last_new_topic_links": self.last_new_topic_links,
            "last_duplicates": self.last_duplicates,
        }

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_seconds,
                )
            except TimeoutError:
                continue

    def _persist(self, items: list[tuple[str, NewsCandidate]]) -> dict[str, int]:
        stats = {
            "new_articles": 0,
            "new_topic_links": 0,
            "duplicates": 0,
        }

        with SessionLocal() as session:
            for topic, candidate in items:
                result = save_news_candidate(
                    session,
                    candidate=candidate,
                    topic=topic,
                    provider=self.provider.source_name,
                )
                stats["new_articles"] += int(result.article_created)
                stats["new_topic_links"] += int(result.topic_link_created)
                if not result.article_created and not result.topic_link_created:
                    stats["duplicates"] += 1
            session.commit()

        return stats

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
