from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from news.types import NewsCandidate

from .models import NewsArticle, NewsArticleTopic


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


@dataclass(frozen=True)
class SaveNewsResult:
    article: NewsArticle
    article_created: bool
    topic_link_created: bool


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise ValueError("news URL must include a hostname")

    port = parts.port
    netloc = hostname
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"

    kept_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in TRACKING_QUERY_KEYS:
            continue
        kept_query.append((key, value))

    kept_query.sort()
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, urlencode(kept_query, doseq=True), ""))


def news_fingerprint(url: str) -> str:
    return sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def save_news_candidate(
    session: Session,
    *,
    candidate: NewsCandidate,
    topic: str,
    provider: str,
) -> SaveNewsResult:
    normalized_topic = topic.strip()
    normalized_provider = provider.strip()
    title = candidate.title.strip()
    if not normalized_topic:
        raise ValueError("topic is required")
    if not normalized_provider:
        raise ValueError("provider is required")
    if not title:
        raise ValueError("news title is required")

    canonical_url = canonicalize_url(candidate.url)
    fingerprint = news_fingerprint(canonical_url)
    article = session.scalar(
        select(NewsArticle).where(NewsArticle.fingerprint == fingerprint).limit(1)
    )

    article_created = False
    if article is None:
        article = NewsArticle(
            fingerprint=fingerprint,
            published_at=_as_utc(candidate.published_at),
            collected_at=datetime.now(timezone.utc),
            provider=normalized_provider,
            title=title,
            url=canonical_url,
            domain=(candidate.domain or "").strip() or None,
            language=(candidate.language or "").strip() or None,
            source_country=(candidate.source_country or "").strip() or None,
            social_image=(candidate.social_image or "").strip() or None,
            ai_status="pending",
        )
        session.add(article)
        session.flush()
        article_created = True
    else:
        # Keep the newest useful metadata without resetting AI processing state.
        if article.published_at is None and candidate.published_at is not None:
            article.published_at = _as_utc(candidate.published_at)
        if not article.domain and candidate.domain:
            article.domain = candidate.domain.strip() or None
        if not article.language and candidate.language:
            article.language = candidate.language.strip() or None
        if not article.source_country and candidate.source_country:
            article.source_country = candidate.source_country.strip() or None
        if not article.social_image and candidate.social_image:
            article.social_image = candidate.social_image.strip() or None

    topic_link = session.scalar(
        select(NewsArticleTopic)
        .where(
            NewsArticleTopic.article_id == article.id,
            NewsArticleTopic.topic == normalized_topic,
        )
        .limit(1)
    )
    topic_link_created = False
    if topic_link is None:
        session.add(NewsArticleTopic(article_id=article.id, topic=normalized_topic))
        # SessionLocal uses autoflush=False, so make the new link visible to
        # subsequent duplicate checks within the same collection transaction.
        session.flush()
        topic_link_created = True

    return SaveNewsResult(
        article=article,
        article_created=article_created,
        topic_link_created=topic_link_created,
    )


def get_latest_news(
    session: Session,
    *,
    limit: int,
    topic: str | None = None,
) -> list[NewsArticle]:
    statement = select(NewsArticle)
    if topic is not None:
        statement = statement.join(
            NewsArticleTopic,
            NewsArticleTopic.article_id == NewsArticle.id,
        ).where(NewsArticleTopic.topic == topic)

    statement = statement.order_by(
        NewsArticle.published_at.desc(),
        NewsArticle.id.desc(),
    ).limit(limit)
    return list(session.scalars(statement).all())


def get_news_topics_for_articles(
    session: Session,
    article_ids: list[int],
) -> dict[int, list[str]]:
    if not article_ids:
        return {}

    statement = (
        select(NewsArticleTopic)
        .where(NewsArticleTopic.article_id.in_(article_ids))
        .order_by(NewsArticleTopic.article_id, NewsArticleTopic.topic)
    )
    result: dict[int, list[str]] = {article_id: [] for article_id in article_ids}
    for row in session.scalars(statement):
        result.setdefault(row.article_id, []).append(row.topic)
    return result
