from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AI_CATEGORY = Literal[
    "geopolitics",
    "us_policy",
    "fed_rates",
    "semiconductors",
    "energy",
    "korea_market",
    "company_specific",
    "macro",
    "other",
]

AI_ASSET = Literal[
    "KOSPI",
    "SEMICONDUCTORS",
    "KRX:005930",
    "KRX:000660",
    "NASDAQ:SKHY",
    "NASDAQ:NVDA",
    "NASDAQ:MU",
    "INDEX:SOX",
    "INDEX:KOSPI",
    "FUTURES:NQ",
    "FUTURES:KOSPI200",
    "FX:USDKRW",
    "COMMODITY:WTI",
    "COMMODITY:BRENT",
    "RATE:US10Y",
    "RATE:US30Y",
]

TIME_HORIZON = Literal["intraday", "1d", "multiday", "longer"]

AI_CATEGORIES = (
    "geopolitics",
    "us_policy",
    "fed_rates",
    "semiconductors",
    "energy",
    "korea_market",
    "company_specific",
    "macro",
    "other",
)


class NewsImpactVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kospi: float = Field(ge=-1.0, le=1.0)
    semiconductors: float = Field(ge=-1.0, le=1.0)
    nasdaq100: float = Field(ge=-1.0, le=1.0)
    oil: float = Field(ge=-1.0, le=1.0)
    rates: float = Field(ge=-1.0, le=1.0)
    usdkrw: float = Field(ge=-1.0, le=1.0)


class ArticleAIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: int
    category: AI_CATEGORY
    event_type: str
    market_relevance: float = Field(ge=0.0, le=1.0)
    sentiment: float = Field(ge=-1.0, le=1.0)
    severity: float = Field(ge=0.0, le=100.0)
    confidence: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    time_horizon: TIME_HORIZON
    affected_assets: list[AI_ASSET]
    impact: NewsImpactVector
    rationale: str


class NewsAnalysisBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analyses: list[ArticleAIResult]
