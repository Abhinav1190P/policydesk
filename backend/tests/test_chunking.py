from app.providers.stub import StubEmbedder
from app.rag.chunking import fixed_chunks, hierarchical_chunks, semantic_chunks, split_sections

DOC = """# Leave Policy

Intro line about the policy.

## Annual leave

""" + " ".join(f"word{i}" for i in range(450)) + """

## Sick leave

Employees get 12 days of sick leave. A certificate is needed after 2 days. Sick leave does not carry forward.
"""


def test_split_sections_tracks_heading_path():
    secs = split_sections(DOC)
    assert [s.heading for s in secs] == ["Leave Policy", "Leave Policy > Annual leave", "Leave Policy > Sick leave"]


def test_split_sections_plain_text_without_headings():
    secs = split_sections("just some text\nwith lines")
    assert len(secs) == 1 and secs[0].heading is None


def test_fixed_chunks_overlap_and_order():
    chunks = fixed_chunks(DOC, size=200, overlap=40)
    annual = [c for c in chunks if c.heading and c.heading.endswith("Annual leave")]
    assert len(annual) == 3
    first, second = annual[0].content.split(), annual[1].content.split()
    assert first[-40:] == second[:40]
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    assert all(c.context is None for c in chunks)


def test_hierarchical_children_carry_parent_context():
    chunks = hierarchical_chunks(DOC, parent_size=300, child_size=100, child_overlap=20)
    annual = [c for c in chunks if c.heading and c.heading.endswith("Annual leave")]
    parents = {c.context for c in annual}
    assert len(parents) == 2
    for c in annual:
        assert c.content.split()[0] in c.context
        assert c.context.startswith("Leave Policy > Annual leave\n")
        assert len(c.content.split()) <= 100


def test_fixed_chunks_never_loop_when_overlap_too_big():
    chunks = fixed_chunks("a b c d e f g h", size=3, overlap=5)
    assert len(chunks) == 6 and chunks[-1].content == "f g h"


async def test_semantic_chunks_split_on_topic_shift():
    text = (
        "Annual leave is 24 days. Annual leave accrues monthly. Annual leave needs approval. "
        "Laptops are encrypted by IT. Laptops are replaced every 3 years. Laptops need the VPN."
    )
    chunks, tokens = await semantic_chunks(text, StubEmbedder(), breakpoint_pct=80)
    assert tokens > 0
    assert len(chunks) >= 2
    assert "leave" in chunks[0].content and "Laptops" not in chunks[0].content
