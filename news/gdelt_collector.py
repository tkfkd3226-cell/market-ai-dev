from datetime import datetime, timezone

import httpx

from .topics import NEWS_TOPICS, NewsTopic
from .types import NewsCandidate


class GDELTNewsCollector:
    source_name = "gdelt-doc-2"
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(
        self,
        *,
        timeout_seconds: int,
        lookback_hours: int,
        max_records_per_topic: int,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.lookback_hours = lookback_hours
        self.max_records_per_topic = max_records_per_topic
        self.topics = NEWS_TOPICS

    def fetch(self) -> tuple[list[tuple[str, NewsCandidate]], dict[str, str]]:
        items: list[tuple[str, NewsCandidate]] = []
        errors: dict[str, str] = {}

        headers = {
            "User-Agent": "market-ai-local/0.4 (+local research project)",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(float(self.timeout_seconds))

        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            for topic in self.topics:
                try:
                    topic_items = self._fetch_topic(client, topic)
                    items.extend((topic.key, item) for item in topic_items)
                except Exception as exc:
                    errors[topic.key] = str(exc)

        return items, errors

    def _fetch_topic(
        self,
        client: httpx.Client,
        topic: NewsTopic,
    ) -> list[NewsCandidate]:
        response = client.get(
            self.endpoint,
            params={
                "query": topic.query,
                "mode": "artlist",
                "format": "json",
                "sort": "datedesc",
                "timespan": f"{self.lookback_hours}h",
                "maxrecords": self.max_records_per_topic,
            },
        )
        response.raise_for_status()

        payload = response.json()
        raw_articles = payload.get("articles", [])
        if not isinstance(raw_articles, list):
            raise ValueError("GDELT response does not contain an article list")

        results: list[NewsCandidate] = []
        for raw in raw_articles:
            if not isinstance(raw, dict):
                continue

            title = self._clean_text(raw.get("title"))
            url = self._clean_text(raw.get("url"))
            if not title or not url:
                continue

            results.append(
                NewsCandidate(
                    title=title,
                    url=url,
                    published_at=self._parse_seen_date(raw.get("seendate")),
                    domain=self._clean_text(raw.get("domain")),
                    language=self._clean_text(raw.get("language")),
                    source_country=self._clean_text(raw.get("sourcecountry")),
                    social_image=self._clean_text(raw.get("socialimage")),
                )
            )

        return results

    @staticmethod
    def _clean_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_seen_date(value: object) -> datetime | None:
        text = GDELTNewsCollector._clean_text(value)
        if text is None:
            return None

        normalized = text.upper()
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y%m%dT%H%M%S"):
            try:
                return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
