import math
import re
from dataclasses import dataclass

from app.providers.text import sentences

_heading = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Chunk:
    ord: int
    content: str
    context: str | None
    heading: str | None
    token_count: int


@dataclass
class Section:
    heading: str | None
    text: str


def words(text: str) -> list[str]:
    return text.split()


def token_count(text: str) -> int:
    return max(1, math.ceil(len(words(text)) * 1.3))


def split_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    path: list[str] = []
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(" > ".join(path) or None, body))
        buf.clear()

    for line in text.splitlines():
        m = _heading.match(line.strip())
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[: level - 1] + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    if not sections and text.strip():
        sections.append(Section(None, text.strip()))
    return sections


def _windows(ws: list[str], size: int, overlap: int) -> list[list[str]]:
    if not ws:
        return []
    step = max(1, size - overlap)
    out = []
    i = 0
    while i < len(ws):
        out.append(ws[i : i + size])
        if i + size >= len(ws):
            break
        i += step
    return out


def fixed_chunks(text: str, size: int = 200, overlap: int = 40) -> list[Chunk]:
    out: list[Chunk] = []
    for sec in split_sections(text):
        for w in _windows(words(sec.text), size, overlap):
            body = " ".join(w)
            out.append(Chunk(len(out), body, None, sec.heading, token_count(body)))
    return out


def hierarchical_chunks(text: str, parent_size: int = 600, child_size: int = 120, child_overlap: int = 20) -> list[Chunk]:
    out: list[Chunk] = []
    for sec in split_sections(text):
        for parent in _windows(words(sec.text), parent_size, 0):
            parent_text = " ".join(parent)
            if sec.heading:
                parent_text = f"{sec.heading}\n{parent_text}"
            for child in _windows(parent, child_size, child_overlap):
                body = " ".join(child)
                out.append(Chunk(len(out), body, parent_text, sec.heading, token_count(body)))
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 1.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def semantic_chunks(text: str, embedder, buffer: int = 1, breakpoint_pct: float = 90, max_words: int = 220) -> tuple[list[Chunk], int]:
    out: list[Chunk] = []
    tokens = 0
    for sec in split_sections(text):
        sents = sentences(sec.text)
        if not sents:
            continue
        if len(sents) < 3:
            body = " ".join(sents)
            out.append(Chunk(len(out), body, None, sec.heading, token_count(body)))
            continue
        windows = [" ".join(sents[max(0, i - buffer) : i + buffer + 1]) for i in range(len(sents))]
        vecs, t = await embedder.embed(windows, kind="document")
        tokens += t
        dists = [1 - _cosine(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
        threshold = _percentile(dists, breakpoint_pct)
        current: list[str] = []
        for i, s in enumerate(sents):
            current.append(s)
            n_words = sum(len(x.split()) for x in current)
            boundary = i < len(dists) and dists[i] >= threshold
            if boundary or n_words >= max_words or i == len(sents) - 1:
                body = " ".join(current)
                out.append(Chunk(len(out), body, None, sec.heading, token_count(body)))
                current = []
    return out, tokens


async def chunk_text(text: str, strategy: str, embedder=None) -> tuple[list[Chunk], int]:
    if strategy == "fixed":
        return fixed_chunks(text), 0
    if strategy == "hierarchical":
        return hierarchical_chunks(text), 0
    if strategy == "semantic":
        if embedder is None:
            raise ValueError("semantic chunking needs an embedder")
        return await semantic_chunks(text, embedder)
    raise ValueError(f"unknown chunk strategy {strategy}")
