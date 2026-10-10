import re

STOPWORDS = set(
    "a an the and or but if then else of to in on at by for with about from as is are was were be been being "
    "do does did can could should would will may might must shall i me my we our you your he she it its they them "
    "their this that these those what which who whom how when where why there here not no yes any all some much many "
    "per than so such into over under up down out off again more most other own same just also only very".split()
)

_token = re.compile(r"[a-z0-9]+")
_sentence = re.compile(r"(?<=[.!?])\s+|\n+")


def stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "s", "ed"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def terms(text: str) -> list[str]:
    return [stem(t) for t in _token.findall(text.lower()) if t not in STOPWORDS]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _sentence.split(text) if len(s.strip()) > 2]
