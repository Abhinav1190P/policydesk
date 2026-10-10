import io

from pypdf import PdfReader

ALLOWED = {
    "application/pdf": "pdf",
    "text/markdown": "md",
    "text/plain": "txt",
    "text/x-markdown": "md",
}


def kind_for(filename: str, mime: str | None) -> str | None:
    name = filename.lower()
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".md", ".markdown")):
        return "md"
    if name.endswith(".txt"):
        return "txt"
    return ALLOWED.get((mime or "").split(";")[0].strip())


def extract_text(data: bytes, kind: str) -> str:
    if kind == "pdf":
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages).strip()
    return data.decode("utf-8", errors="replace").strip()
