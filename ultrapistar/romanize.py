"""Romanized alternates for non-Latin lyric text.

Singing along needs a script the singer can read — a chart in kanji is
useless to someone who can't read it, and a romaji-only chart loses
the native lyric for those who can. ``romanize_chart`` fills each
``Note.alt`` with a romanized rendering of ``Note.text`` so players
can offer native / romanized as a per-song choice.

The converters are optional, lazily-imported dependencies — the Pi
serves charts but never generates them, so missing packages just mean
``romanize_chart`` leaves those notes' ``alt`` empty.

    pip install pykakasi        # Japanese (pure python, offline)
    pip install hangul-romanize # Korean (Revised/academic romanization)

Cyrillic uses a built-in table (no dependency). Chinese and other
scripts are skipped — a wrong romanization is worse than none.
"""
from __future__ import annotations

import re

_HIRA = "぀-ゟ゠"
_KATA = "ァ-ヿｦ-ﾟ"
_HANGUL = "가-힯"
_CYR = "Ѐ-ӿ"
_NONLATIN = re.compile(f"[{_HIRA}{_KATA}{_HANGUL}{_CYR}一-鿿]")


def script_of(text: str) -> str:
    """Dominant non-latin script in a lyric string: jp | kr | cyr | zh | latin."""
    if re.search(f"[{_HIRA}{_KATA}]", text):
        return "jp"
    if re.search(f"[{_HANGUL}]", text):
        return "kr"
    if re.search(f"[{_CYR}]", text):
        return "cyr"
    if re.search(r"[一-鿿]", text):       # kanji w/o kana → likely zh
        return "zh"
    return "latin"


_CYR_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "'", "ы": "y", "ь": "'", "э": "e", "ю": "yu", "я": "ya",
}


def _cyr(s: str) -> str:
    return "".join(
        _CYR_MAP.get(c.lower(), c).upper() if c.isupper() and c.lower() in _CYR_MAP
        else _CYR_MAP.get(c, c) for c in s)


_kakasi = None
def _jp(s: str) -> str:
    global _kakasi
    if _kakasi is None:
        import pykakasi
        _kakasi = pykakasi.kakasi()
    # hepburn per segment, space-separated — one note may hold a phrase
    return " ".join(item["hepburn"] for item in _kakasi.convert(s)
                    if item["hepburn"])


_transliter = None
def _kr(s: str) -> str:
    global _transliter
    if _transliter is None:
        from hangul_romanize import Transliter
        from hangul_romanize.rule import academic
        _transliter = Transliter(academic)
    return _transliter.translit(s)


def romanize_text(text: str, hint: str = "") -> str:
    """Romanized rendering of a lyric chunk, or '' when unsupported/latin.

    Mixed strings transliterate per-script run so e.g. an english hook
    inside a korean line comes back readable end-to-end. A kanji run is
    ambiguous alone (``script_of`` → zh), so it inherits ``hint`` — the
    document's dominant script.
    """
    if not _NONLATIN.search(text):
        return ""
    doc = script_of(text)
    if doc in ("latin", "zh"):
        doc = hint or doc
    if doc not in ("jp", "kr", "cyr"):
        return ""
    # split latin vs non-latin runs; each non-latin run romanizes with
    # the document script (kanji inherits jp/kr context)
    out, run, run_latin = [], "", None
    for ch in text:
        latin = not _NONLATIN.search(ch)
        if run_latin is not None and latin != run_latin:
            out.append(run if run_latin else _romanize_run(run, doc))
            run = ""
        run += ch
        run_latin = latin
    if run:
        out.append(run if run_latin else _romanize_run(run, doc))
    return " ".join(x.strip() for x in out if x.strip())


def _romanize_run(run: str, kind: str) -> str:
    if kind == "latin" or kind is None:
        return run
    try:
        if kind == "jp":
            return _jp(run)
        if kind == "kr":
            return _kr(run)
        if kind == "cyr":
            return _cyr(run)
    except ImportError:
        return ""
    return ""                      # zh and friends — not supported


def romanize_chart(chart) -> bool:
    """Fill ``Note.alt`` for every non-Latin note. Returns True when any
    alternate was written (callers then know the chart is dual-script)."""
    doc = script_of(" ".join(
        n.text for notes in chart.tracks.values() for n in notes))
    hint = doc if doc in ("jp", "kr", "cyr") else ""
    any_alt = False
    for notes in chart.tracks.values():
        for n in notes:
            if n.alt:
                continue
            alt = romanize_text(n.text, hint=hint)
            if alt and alt != n.text:
                n.alt = alt
                any_alt = True
    if any_alt:
        chart.headers.setdefault("ALTSCRIPT", "romaji")
    return any_alt
