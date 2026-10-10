from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class GenResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    guardrail: str | None = None


@dataclass
class StreamEvent:
    type: str
    text: str = ""
    usage: Usage | None = None
    guardrail: str | None = None


@dataclass
class Grounding:
    context: str
    question: str


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


class LLM(Protocol):
    backend: str
    model: str

    async def generate(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> GenResult: ...

    def stream(self, system: str, messages: list[dict], grounding: Grounding | None = None) -> AsyncIterator[StreamEvent]: ...

    async def chat_with_tools(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> GenResult: ...


class Embedder(Protocol):
    backend: str
    model: str
    dim: int

    async def embed(self, texts: list[str], kind: str = "document") -> tuple[list[list[float]], int]: ...


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
