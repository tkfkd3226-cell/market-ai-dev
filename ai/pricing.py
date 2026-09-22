from dataclasses import dataclass


@dataclass(frozen=True)
class TokenPricing:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    source_label: str


# Pricing snapshot used only for local cost estimation. Unknown models are intentionally
# left unpriced rather than guessed.
_MODEL_PRICING: dict[str, TokenPricing] = {
    "gpt-5.6-luna": TokenPricing(
        input_per_million=0.20,
        cached_input_per_million=0.02,
        output_per_million=1.20,
        source_label="openai-2026-08-19",
    ),
}


def get_token_pricing(model: str) -> TokenPricing | None:
    return _MODEL_PRICING.get(model.strip())


def estimate_token_cost_usd(
    *,
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> tuple[TokenPricing | None, float | None]:
    pricing = get_token_pricing(model)
    if pricing is None:
        return None, None

    cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
    uncached = max(0, int(input_tokens) - cached)
    output = max(0, int(output_tokens))

    cost = (
        uncached * pricing.input_per_million
        + cached * pricing.cached_input_per_million
        + output * pricing.output_per_million
    ) / 1_000_000
    return pricing, cost
