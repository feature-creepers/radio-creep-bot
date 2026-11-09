"""Normalisation de texte pour comparer titres et artistes."""
from __future__ import annotations

import re
import unicodedata

_FEAT = re.compile(r"\s*[\(\[](feat\.?|ft\.?|with)\s[^\)\]]*[\)\]]", re.I)
_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_NON_WORD = re.compile(r"[^\w]+")


def norm(text: str | None) -> str:
    """Minuscules, sans accents ni ponctuation, espaces simples."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NON_WORD.sub(" ", text.casefold())
    return " ".join(text.split())


def norm_title(text: str | None) -> str:
    """Comme norm() mais ignore « (feat. X) », « (Remastered) », etc."""
    if not text:
        return ""
    return norm(_PAREN.sub("", _FEAT.sub("", text))) or norm(text)


def same_title(a: str | None, b: str | None) -> bool:
    return bool(a and b) and (norm(a) == norm(b) or norm_title(a) == norm_title(b))


def artist_matches(wanted: str | None, actual: str | None) -> bool:
    """Vrai si l'un des artistes est contenu dans l'autre (gère « A, B & C »)."""
    w, a = norm(wanted), norm(actual)
    if not w or not a:
        return True  # pas d'info : on ne bloque pas
    return w in a or a in w


def fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "?:??"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"
