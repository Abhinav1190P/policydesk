import re

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){9,11}\d(?!\d)")
CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
AADHAAR = re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")

MAX_QUESTION_CHARS = 2000


def mask_pii(text: str, allow: set[str] | None = None) -> tuple[str, bool]:
    allow = allow or set()
    changed = False

    def sub(pattern, label, t):
        nonlocal changed

        def repl(m):
            nonlocal changed
            if m.group(0) in allow:
                return m.group(0)
            changed = True
            return f"[{label}]"

        return pattern.sub(repl, t)

    out = sub(CARD, "CARD", text)
    out = sub(AADHAAR, "ID", out)
    out = sub(EMAIL, "EMAIL", out)
    out = sub(PHONE, "PHONE", out)
    return out, changed


def allowed_contacts(context: str) -> set[str]:
    return set(EMAIL.findall(context)) | set(m.group(0) for m in PHONE.finditer(context))
