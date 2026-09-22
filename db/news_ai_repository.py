import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AIAPIUsage, NewsAIAnalysis, NewsArticle


def get_pending_news_for_ai(
    session: Session,
    *,
    limit: int,
) -> list[NewsArticle]:
    statement = (
        select(NewsArticle)
        .where(NewsArticle.ai_status == "pending")
        .order_by(
            NewsArticle.published_at.desc(),
            NewsArticle.id.desc(),
        )
        .limit(limit)
    )
    return list(session.scalars(statement).all())


def save_news_ai_analysis(
    session: Session,
    *,
    article_id: int,
    model: str,
    prompt_version: str,
    category: str,
    event_type: str,
    market_relevance: float,
    sentiment: float,
    severity: float,
    confidence: float,
    novelty: float,
    time_horizon: str,
    affected_assets: list[str],
    kospi_impact: float,
    semiconductor_impact: float,
    nasdaq100_impact: float,
    oil_impact: float,
    rates_impact: float,
    usdkrw_impact: float,
    rationale: str,
) -> NewsAIAnalysis:
    article = session.get(NewsArticle, article_id)
    if article is None:
        raise ValueError(f"news article not found: {article_id}")

    analysis = session.scalar(
        select(NewsAIAnalysis)
        .where(NewsAIAnalysis.article_id == article_id)
        .limit(1)
    )
    values = {
        "model": model,
        "prompt_version": prompt_version,
        "category": category,
        "event_type": event_type.strip()[:120] or "other",
        "market_relevance": market_relevance,
        "sentiment": sentiment,
        "severity": severity,
        "confidence": confidence,
        "novelty": novelty,
        "time_horizon": time_horizon,
        "affected_assets_json": json.dumps(
            affected_assets,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "kospi_impact": kospi_impact,
        "semiconductor_impact": semiconductor_impact,
        "nasdaq100_impact": nasdaq100_impact,
        "oil_impact": oil_impact,
        "rates_impact": rates_impact,
        "usdkrw_impact": usdkrw_impact,
        "rationale": rationale.strip()[:1000],
    }

    if analysis is None:
        analysis = NewsAIAnalysis(article_id=article_id, **values)
        session.add(analysis)
    else:
        for key, value in values.items():
            setattr(analysis, key, value)

    article.ai_status = "done"
    session.flush()
    return analysis


def get_latest_news_ai_analyses(
    session: Session,
    *,
    limit: int,
    category: str | None = None,
) -> list[tuple[NewsAIAnalysis, NewsArticle]]:
    statement = (
        select(NewsAIAnalysis, NewsArticle)
        .join(NewsArticle, NewsArticle.id == NewsAIAnalysis.article_id)
    )
    if category is not None:
        statement = statement.where(NewsAIAnalysis.category == category)

    statement = statement.order_by(
        NewsAIAnalysis.analyzed_at.desc(),
        NewsAIAnalysis.id.desc(),
    ).limit(limit)
    return [(analysis, article) for analysis, article in session.execute(statement).all()]


def get_news_ai_counts(session: Session) -> dict[str, object]:
    status_rows = session.execute(
        select(NewsArticle.ai_status, func.count(NewsArticle.id)).group_by(
            NewsArticle.ai_status
        )
    ).all()
    article_status = {status: int(count) for status, count in status_rows}
    analysis_rows = session.scalar(select(func.count(NewsAIAnalysis.id))) or 0
    return {
        "article_status": article_status,
        "analysis_rows": int(analysis_rows),
    }


def decode_affected_assets(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def save_ai_api_usage(
    session: Session,
    *,
    provider: str,
    response_id: str | None,
    model: str,
    prompt_version: str,
    article_count: int,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    total_tokens: int,
    input_price_per_million: float | None,
    cached_input_price_per_million: float | None,
    output_price_per_million: float | None,
    pricing_source: str | None,
    estimated_cost_usd: float | None,
) -> AIAPIUsage:
    row = AIAPIUsage(
        provider=provider,
        response_id=response_id,
        model=model,
        prompt_version=prompt_version,
        article_count=max(0, int(article_count)),
        input_tokens=max(0, int(input_tokens)),
        cached_input_tokens=max(0, int(cached_input_tokens)),
        output_tokens=max(0, int(output_tokens)),
        reasoning_tokens=max(0, int(reasoning_tokens)),
        total_tokens=max(0, int(total_tokens)),
        input_price_per_million=input_price_per_million,
        cached_input_price_per_million=cached_input_price_per_million,
        output_price_per_million=output_price_per_million,
        pricing_source=pricing_source,
        estimated_cost_usd=estimated_cost_usd,
    )
    session.add(row)
    session.flush()
    return row


def get_ai_usage_summary(
    session: Session,
    *,
    timezone_name: str = "Asia/Seoul",
) -> dict[str, object]:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    rows = list(
        session.scalars(
            select(AIAPIUsage).order_by(
                AIAPIUsage.created_at.asc(),
                AIAPIUsage.id.asc(),
            )
        ).all()
    )
    local_tz = ZoneInfo(timezone_name)
    now_local = datetime.now(timezone.utc).astimezone(local_tz)

    def normalize_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def summarize(items: list[AIAPIUsage]) -> dict[str, object]:
        unpriced_calls = sum(
            1 for row in items if row.estimated_cost_usd is None
        )
        priced_costs = [
            float(row.estimated_cost_usd)
            for row in items
            if row.estimated_cost_usd is not None
        ]
        if not items:
            estimated_cost_usd: float | None = 0.0
        elif unpriced_calls > 0:
            estimated_cost_usd = None
        else:
            estimated_cost_usd = round(sum(priced_costs), 8)

        return {
            "calls": len(items),
            "articles": sum(int(row.article_count) for row in items),
            "input_tokens": sum(int(row.input_tokens) for row in items),
            "cached_input_tokens": sum(int(row.cached_input_tokens) for row in items),
            "output_tokens": sum(int(row.output_tokens) for row in items),
            "reasoning_tokens": sum(int(row.reasoning_tokens) for row in items),
            "total_tokens": sum(int(row.total_tokens) for row in items),
            "estimated_cost_usd": estimated_cost_usd,
            "unpriced_calls": unpriced_calls,
        }

    today_rows = []
    month_rows = []
    for row in rows:
        local_dt = normalize_utc(row.created_at).astimezone(local_tz)
        if local_dt.date() == now_local.date():
            today_rows.append(row)
        if (local_dt.year, local_dt.month) == (now_local.year, now_local.month):
            month_rows.append(row)

    return {
        "timezone": timezone_name,
        "today": summarize(today_rows),
        "month": summarize(month_rows),
        "all_time": summarize(rows),
    }
