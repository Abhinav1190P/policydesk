from app.config import get_settings
from app.providers.bedrock import BedrockLLM, TitanEmbedder
from app.providers.openai_compat import CohereEmbedder, OllamaEmbedder, OpenAICompatLLM
from app.providers.stub import StubEmbedder, StubLLM

_llms: dict[str, object] = {}
_embedder = None


def get_llm(backend: str | None = None):
    s = get_settings()
    name = backend or s.ai_backend
    if name not in _llms:
        if name == "stub":
            _llms[name] = StubLLM()
        elif name == "local":
            _llms[name] = OpenAICompatLLM("local", f"{s.ollama_url.rstrip('/')}/v1", s.ollama_model)
        elif name == "groq":
            if not s.groq_api_key:
                raise RuntimeError("GROQ_API_KEY is not set")
            _llms[name] = OpenAICompatLLM("groq", s.groq_url, s.groq_model, s.groq_api_key, per_minute=s.groq_calls_per_minute)
        elif name == "bedrock":
            _llms[name] = BedrockLLM()
        else:
            raise RuntimeError(f"unknown AI backend {name}")
    return _llms[name]


def fallback_llm():
    s = get_settings()
    if s.fallback_backend in ("none", s.ai_backend):
        return None
    try:
        return get_llm(s.fallback_backend)
    except RuntimeError:
        return None


def get_embedder():
    global _embedder
    if _embedder is None:
        s = get_settings()
        if s.embed_backend == "stub":
            _embedder = StubEmbedder(s.embed_dim)
        elif s.embed_backend == "local":
            _embedder = OllamaEmbedder(s.ollama_url, s.ollama_embed_model, s.embed_dim)
        elif s.embed_backend == "cohere":
            _embedder = CohereEmbedder(s.cohere_api_key, s.cohere_embed_model, s.embed_dim, per_minute=s.cohere_calls_per_minute)
        elif s.embed_backend == "bedrock":
            _embedder = TitanEmbedder(dim=s.embed_dim)
    return _embedder


def set_llm(name: str, llm) -> None:
    _llms[name] = llm


def reset() -> None:
    global _embedder
    _llms.clear()
    _embedder = None
