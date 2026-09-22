from dataclasses import dataclass


@dataclass(frozen=True)
class NewsTopic:
    key: str
    label: str
    query: str


NEWS_TOPICS: tuple[NewsTopic, ...] = (
    NewsTopic(
        key="geopolitics",
        label="Iran / Hormuz / Middle East",
        query='(iran OR hormuz OR oman OR uae OR "middle east")',
    ),
    NewsTopic(
        key="us_policy",
        label="Trump / tariffs / sanctions / trade",
        query='("donald trump" OR trump) (tariff OR tariffs OR sanctions OR trade OR iran)',
    ),
    NewsTopic(
        key="fed_rates",
        label="Fed / Treasury yields",
        query='("federal reserve" OR fomc OR "treasury yield" OR "bond yield")',
    ),
    NewsTopic(
        key="semiconductors",
        label="AI / semiconductors",
        query='(nvidia OR micron OR "sk hynix" OR "samsung electronics" OR semiconductor OR "ai chip")',
    ),
    NewsTopic(
        key="energy",
        label="Oil / OPEC / crude",
        query='("crude oil" OR wti OR brent OR opec OR "opec+")',
    ),
    NewsTopic(
        key="korea_market",
        label="Korea market / KOSPI",
        query='(kospi OR "south korea stocks" OR "samsung electronics" OR "sk hynix")',
    ),
)

NEWS_TOPIC_KEYS = frozenset(topic.key for topic in NEWS_TOPICS)
