import asyncio
from datetime import datetime, timezone

from db.database import SessionLocal
from db.news_ai_repository import (
    get_pending_news_for_ai,
    save_ai_api_usage,
    save_news_ai_analysis,
)
from db.news_repository import get_news_topics_for_articles

from .openai_analyzer import OpenAINewsAnalyzer, OpenAIUsage
from .pricing import estimate_token_cost_usd
from .schemas import ArticleAIResult


class NewsAIService:
    def __init__(
        self,
        analyzer: OpenAINewsAnalyzer,
        *,
        enabled: bool,
        poll_seconds: int,
        batch_size: int,
    ) -> None:
        self.analyzer = analyzer
        self.enabled = enabled
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._run_lock = asyncio.Lock()

        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None
        self.last_selected = 0
        self.last_analyzed = 0
        self.last_usage: dict[str, object] | None = None
        self.last_usage_error: str | None = None

    @property
    def active(self) -> bool:
        return self.enabled and self.analyzer.configured

    @property
    def state(self) -> str:
        if self.active:
            return "active"
        if self.analyzer.configured:
            return "manual_only"
        return "disabled_by_config"

    @property
    def disabled_reason(self) -> str | None:
        if self.active:
            return None
        if not self.analyzer.configured:
            configuration_error = getattr(self.analyzer, "configuration_error", None)
            if configuration_error:
                return configuration_error
            return "OPENAI_API_KEY is not configured"
        return "automatic AI news analysis is disabled by MARKET_AI_AI_ENABLED=false"

    async def start(self) -> None:
        if not self.active or self._task is not None:
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="market-ai-news-analyzer")

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

    async def run_once(self, *, limit: int | None = None) -> dict[str, object]:
        async with self._run_lock:
            self.last_started_at = datetime.now(timezone.utc)
            self.last_finished_at = None
            self.last_error = None
            self.last_selected = 0
            self.last_analyzed = 0
            self.last_usage = None
            self.last_usage_error = None

            requested_limit = self.batch_size if limit is None else limit
            requested_limit = max(1, min(25, requested_limit))

            if not self.analyzer.configured:
                self.last_finished_at = datetime.now(timezone.utc)
                return self.status()

            try:
                articles, article_payloads = await asyncio.to_thread(
                    self._load_pending,
                    requested_limit,
                )
                self.last_selected = len(articles)
                if not articles:
                    return self.status()

                try:
                    parsed = await asyncio.to_thread(
                        self.analyzer.analyze,
                        article_payloads,
                    )
                finally:
                    await asyncio.to_thread(
                        self._record_last_usage,
                        len(article_payloads),
                    )
                self._validate_batch(
                    [article.id for article in articles],
                    parsed.analyses,
                )
                await asyncio.to_thread(self._persist, parsed.analyses)
                self.last_analyzed = len(parsed.analyses)
            except Exception as exc:
                # Leave articles pending on failure so the next run can safely retry.
                self.last_error = str(exc)
            finally:
                self.last_finished_at = datetime.now(timezone.utc)

            return self.status()

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "configured": self.analyzer.configured,
            "active": self.active,
            "state": self.state,
            "disabled_reason": self.disabled_reason,
            "running": self._task is not None and not self._task.done(),
            "poll_seconds": self.poll_seconds,
            "batch_size": self.batch_size,
            "provider": self.analyzer.source_name,
            "model": self.analyzer.model,
            "prompt_version": self.analyzer.prompt_version,
            "reasoning_effort": getattr(self.analyzer, "reasoning_effort", "none"),
            "configuration_error": getattr(self.analyzer, "configuration_error", None),
            "last_started_at": self._iso_or_none(self.last_started_at),
            "last_finished_at": self._iso_or_none(self.last_finished_at),
            "last_error": self.last_error,
            "last_selected": self.last_selected,
            "last_analyzed": self.last_analyzed,
            "last_usage": self.last_usage,
            "last_usage_error": self.last_usage_error,
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

    def _load_pending(
        self,
        limit: int,
    ) -> tuple[list[object], list[dict[str, object]]]:
        with SessionLocal() as session:
            articles = get_pending_news_for_ai(session, limit=limit)
            topic_map = get_news_topics_for_articles(
                session,
                [article.id for article in articles],
            )
            payloads: list[dict[str, object]] = []
            for article in articles:
                payloads.append(
                    {
                        "article_id": article.id,
                        "title": article.title,
                        "topics": topic_map.get(article.id, []),
                        "published_at": self._iso_or_none(article.published_at),
                        "provider": article.provider,
                        "domain": article.domain,
                        "language": article.language,
                        "source_country": article.source_country,
                    }
                )
            return articles, payloads

    def _record_last_usage(self, article_count: int) -> None:
        usage: OpenAIUsage | None = getattr(self.analyzer, "last_usage", None)
        if usage is None:
            return

        pricing, estimated_cost_usd = estimate_token_cost_usd(
            model=self.analyzer.model,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
        )
        self.last_usage = {
            "response_id": usage.response_id,
            "articles": article_count,
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "pricing_available": pricing is not None,
        }

        try:
            with SessionLocal() as session:
                save_ai_api_usage(
                    session,
                    provider=self.analyzer.source_name,
                    response_id=usage.response_id,
                    model=self.analyzer.model,
                    prompt_version=self.analyzer.prompt_version,
                    article_count=article_count,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    total_tokens=usage.total_tokens,
                    input_price_per_million=(
                        None if pricing is None else pricing.input_per_million
                    ),
                    cached_input_price_per_million=(
                        None if pricing is None else pricing.cached_input_per_million
                    ),
                    output_price_per_million=(
                        None if pricing is None else pricing.output_per_million
                    ),
                    pricing_source=None if pricing is None else pricing.source_label,
                    estimated_cost_usd=estimated_cost_usd,
                )
                session.commit()
        except Exception as exc:
            # Usage logging must not invalidate an otherwise valid AI analysis batch.
            self.last_usage_error = str(exc)

    def _persist(self, results: list[ArticleAIResult]) -> None:
        with SessionLocal() as session:
            for result in results:
                save_news_ai_analysis(
                    session,
                    article_id=result.article_id,
                    model=self.analyzer.model,
                    prompt_version=self.analyzer.prompt_version,
                    category=result.category,
                    event_type=result.event_type,
                    market_relevance=result.market_relevance,
                    sentiment=result.sentiment,
                    severity=result.severity,
                    confidence=result.confidence,
                    novelty=result.novelty,
                    time_horizon=result.time_horizon,
                    affected_assets=list(result.affected_assets),
                    kospi_impact=result.impact.kospi,
                    semiconductor_impact=result.impact.semiconductors,
                    nasdaq100_impact=result.impact.nasdaq100,
                    oil_impact=result.impact.oil,
                    rates_impact=result.impact.rates,
                    usdkrw_impact=result.impact.usdkrw,
                    rationale=result.rationale,
                )
            session.commit()

    @staticmethod
    def _validate_batch(
        requested_ids: list[int],
        results: list[ArticleAIResult],
    ) -> None:
        result_ids = [item.article_id for item in results]
        if len(result_ids) != len(set(result_ids)):
            raise RuntimeError("OpenAI analysis returned duplicate article_id values")
        if set(result_ids) != set(requested_ids):
            raise RuntimeError("OpenAI analysis article_id set does not match request")

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
