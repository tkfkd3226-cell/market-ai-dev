from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsCandidate:
    title: str
    url: str
    published_at: datetime | None
    domain: str | None = None
    language: str | None = None
    source_country: str | None = None
    social_image: str | None = None
