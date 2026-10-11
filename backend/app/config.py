from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PolicyDesk"
    environment: str = "development"
    instance_id: str = "local"
    cors_origins: str = "http://localhost:5173"

    database_url: str = "postgresql://policydesk:policydesk@localhost:5432/policydesk"
    db_pool_min: int = 2
    db_pool_max: int = 10
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    run_worker_in_process: bool = False
    worker_prefetch: int = 4
    ingest_max_attempts: int = 3
    ingest_retry_delay_ms: int = 2000

    jwt_secret: str = "change-me-in-production-please-32b"
    jwt_ttl_minutes: int = 720
    internal_tools_secret: str = "change-me-internal-tools-secret"
    internal_tools_max_skew_s: int = 300

    ai_backend: Literal["stub", "local", "groq", "bedrock"] = "stub"
    embed_backend: Literal["stub", "local", "cohere", "bedrock"] = "stub"
    retriever: Literal["pgvector", "bedrock_kb"] = "pgvector"
    agent_mode: Literal["local", "bedrock_agent"] = "local"
    fallback_backend: Literal["none", "stub", "local", "groq", "bedrock"] = "none"
    embed_dim: int = 1024

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_embed_model: str = "mxbai-embed-large"

    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    groq_url: str = "https://api.groq.com/openai/v1"

    cohere_api_key: str = ""
    cohere_embed_model: str = "embed-english-v3.0"
    cohere_calls_per_minute: int = 35
    groq_calls_per_minute: int = 25

    aws_region: str = "us-east-1"
    bedrock_model_id: str = "us.amazon.nova-lite-v1:0"
    bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
    bedrock_kb_id: str = ""
    bedrock_kb_data_source_id: str = ""
    bedrock_kb_search_type: Literal["SEMANTIC", "HYBRID", "DEFAULT"] = "DEFAULT"
    bedrock_guardrail_id: str = ""
    bedrock_guardrail_version: str = "DRAFT"
    bedrock_agent_id: str = ""
    bedrock_agent_alias_id: str = ""
    s3_bucket: str = ""
    s3_prefix: str = "tenants"

    stub_latency_ms: int = 0
    stub_embed_latency_ms: int = 0
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 700
    request_timeout_s: float = 60.0
    provider_max_retries: int = 2
    aws_enabled: bool = True
    aws_daily_usd_limit: float = 0.50
    circuit_max_failures: int = 5
    circuit_window_s: int = 120
    circuit_cooldown_s: int = 600
    max_context_chars: int = 8000
    max_question_tokens: int = 500

    chunk_strategy: Literal["fixed", "hierarchical", "semantic"] = "hierarchical"
    retrieval_k: int = 5
    min_relevance: float = 0.12
    history_turns: int = 6

    cache_ttl_s: int = 3600
    rate_limit_per_minute: int = 20
    rate_limit_burst: int = 10
    tenant_rate_limit_per_minute: int = 300
    login_attempts_per_minute: int = 10
    daily_token_budget: int = 200000
    global_daily_token_budget: int = 300000
    ip_rate_limit_per_minute: int = 30
    agent_session_ttl_s: int = 3600
    max_upload_mb: int = 5

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
