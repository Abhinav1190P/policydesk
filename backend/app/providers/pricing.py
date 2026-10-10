PRICES_PER_1K = {
    "us.amazon.nova-micro-v1:0": (0.000035, 0.00014),
    "us.amazon.nova-lite-v1:0": (0.00006, 0.00024),
    "us.amazon.nova-pro-v1:0": (0.0008, 0.0032),
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": (0.0008, 0.004),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (0.001, 0.005),
    "us.meta.llama3-3-70b-instruct-v1:0": (0.00072, 0.00072),
    "openai/gpt-oss-20b": (0.000075, 0.0003),
    "amazon.titan-embed-text-v2:0": (0.00002, 0.0),
    "embed-english-v3.0": (0.0001, 0.0),
}

FREE_BACKENDS = {"stub", "local", "none"}


def cost_usd(backend: str, model: str, input_tokens: int, output_tokens: int) -> float:
    if backend in FREE_BACKENDS:
        return 0.0
    pin, pout = PRICES_PER_1K.get(model, (0.0, 0.0))
    return round(input_tokens / 1000 * pin + output_tokens / 1000 * pout, 8)
