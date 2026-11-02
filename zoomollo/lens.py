"""Keyword lens: read résumés through the keywords of a search.

A search becomes keyword *groups*. Each group gets a colour; a candidate's
card shows one chip per group (filled + count when found, grey when not), and
the résumé is shown with every match highlighted in its group's colour.

Two ways to search:
  plain English  "tax manager with experience reviewing who lives in Texas or Wisconsin"
                 -> tax manager · reviewing · Texas | Wisconsin
  exact groups   "tax manager, review, Texas | Wisconsin"
                 (commas separate groups; "|" or " or " separate alternatives)

Words match their common forms (reviewing ~ review, reviewed, reviewer), and
states match their abbreviation too (Texas ~ TX).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from .discover import STATES

COLORS = 8  # CSS classes k0..k7

_FILLER = re.compile(
    r"\b(looking for|searching for|search for|find me|find|need|want|someone|somebody|a candidate|candidates?|"
    r"people|person|who (?:lives?|is|are|has|have|resides?|is based|are based)|living|lives|resides?|located|"
    r"based|currently|with|having|has|have|had|experience (?:in|with|doing)?|experienced|background in|"
    r"knowledge of|skills? in|skilled in|years? of|years?|yrs|plus|at least|minimum|a|an|the|of|that|which|"
    r"and|also|preferably|ideally|strong|solid|proven)\b", re.I)
_SPLITTERS = re.compile(r",|;|\band\b|\bwith\b|\bwho\b|\bthat\b|\bhaving\b|\bplus\b", re.I)
_STOP = {"in", "at", "on", "for", "to", "or", "and", "from", "near", "around", "experience", "years", "year"}


@dataclass
class Group:
    label: str                       # what the chip says
    terms: list[str]                 # alternatives as typed (for display)
    patterns: list[re.Pattern] = field(default_factory=list)
    kind: str = "keyword"            # keyword | location
    color: int = 0
    keys: list[tuple[str, bool]] = field(default_factory=list)  # quick pre-check: (text, case-sensitive)


# --- word forms -----------------------------------------------------------------------

def _stem(word: str) -> str:
    w = word.lower()
    for suf in ("ization", "isation", "ations", "ation", "ments", "ment", "ings", "ing", "ers", "er", "ies", "ied",
                "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: -len(suf)]
            if suf in ("ies", "ied"):
                base += "y"
            # "managing" -> "manag", "reviewed" -> "review"
            return base
    return w


def _word_rx(word: str) -> str:
    w = word.strip()
    if not w:
        return ""
    if len(w) <= 3 or not w.isalpha():  # CPA, SOX, 401k, C++: exact
        return re.escape(w)
    stem = _stem(w)
    stem_rx = re.escape(stem[:-1]) + "(?:" + re.escape(stem[-1]) + "|i)" if stem.endswith("y") else re.escape(stem)
    return stem_rx + r"[a-z]{0,6}"


def phrase_pattern(phrase: str) -> re.Pattern | None:
    words = [w for w in re.findall(r"[A-Za-z0-9+#&./']+", phrase) if w.lower() not in ("a", "an", "the")]
    if not words:
        return None
    parts = [_word_rx(w) for w in words]
    if len(parts) == 1:
        rx = r"(?<![A-Za-z0-9])" + parts[0] + r"(?![A-Za-z0-9])"
    else:
        # In order with up to two words between ("tax and audit manager"), or reversed ("Manager, Tax").
        gap = r"(?:\W+\w+){0,2}\W+"
        fwd = gap.join(parts)
        rev = r"\W+".join(reversed(parts)) if len(parts) == 2 else None
        rx = r"(?<![A-Za-z0-9])(?:" + fwd + (("|" + rev) if rev else "") + r")(?![A-Za-z0-9])"
    return re.compile(rx, re.I)


def _state_patterns(abbr: str) -> list[re.Pattern]:
    return [re.compile(r"(?<![A-Za-z])" + re.escape(STATES[abbr]) + r"(?![A-Za-z])", re.I),
            re.compile(r"(?<![A-Za-z])" + abbr + r"(?![A-Za-z])")]  # "TX" in capitals only


def _place_term(term: str) -> tuple[str, list[re.Pattern]] | None:
    """'Texas' / 'TX' / 'Austin, TX' / 'Austin' -> label + patterns, or None if not a place."""
    t = term.strip().strip(".")
    up = t.upper()
    for abbr, name in STATES.items():
        if t.lower() == name.lower() or (t == up and up == abbr):
            return name, _state_patterns(abbr)
    m = re.fullmatch(r"([A-Za-z .'-]+),\s*([A-Za-z]{2})", t)
    if m and m.group(2).upper() in STATES:
        return f"{m.group(1).strip()}, {m.group(2).upper()}", [phrase_pattern(m.group(1).strip())]
    return None


# --- parsing a search -----------------------------------------------------------------

def parse(query: str) -> list[Group]:
    q = query.strip()
    if not q:
        return []
    exact = ("|" in q) or ("," in q and not re.search(r"\b(who|with|lives|living|located|based)\b", q, re.I))
    groups = _parse_exact(q) if exact else _parse_natural(q)
    for i, g in enumerate(groups):
        g.color = i % COLORS
    return groups


def _group_from_terms(terms: list[str]) -> Group | None:
    terms = [t.strip() for t in terms if t.strip()]
    if not terms:
        return None
    places = [_place_term(t) for t in terms]
    if all(places):
        pats = [p for _, ps in places for p in ps]
        keys = []
        for lbl, _ in places:
            if lbl in STATES.values():
                keys += [(lbl.lower(), False), (next(a for a, n in STATES.items() if n == lbl), True)]
            else:
                keys.append((lbl.split(",")[0].strip().lower(), False))
        return Group(" | ".join(lbl for lbl, _ in places), [lbl for lbl, _ in places], pats, "location", keys=keys)
    pats = [p for p in (phrase_pattern(t) for t in terms) if p]
    return Group(" | ".join(terms), terms, pats, keys=[(_key(t), False) for t in terms]) if pats else None


def _key(term: str) -> str:
    """The longest word's stem, lower-cased: every match of the term contains it."""
    words = [w for w in re.findall(r"[A-Za-z0-9+#&./']+", term) if w.lower() not in ("a", "an", "the")]
    if not words:
        return ""
    w = max(words, key=len).lower()
    if len(w) <= 3 or not w.isalpha():
        return w
    st = _stem(w)
    return st[:-1] if st.endswith("y") else st


def _parse_exact(q: str) -> list[Group]:
    out = []
    for chunk in re.split(r"[,;\n]", q):
        alts = re.split(r"\||\bor\b", chunk, flags=re.I)
        g = _group_from_terms(alts)
        if g:
            out.append(g)
    return out


def _parse_natural(q: str) -> list[Group]:
    # 1. places ("in Texas or Wisconsin", "Austin, TX", "TX"), kept in the order typed
    places: list[tuple[int, str]] = []

    def take_city_state(m):
        places.append((m.start(), f"{m.group(1).strip()}, {m.group(2).upper()}"))
        return " " * len(m.group(0))
    q2 = re.sub(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?),\s*([A-Z]{2})\b", take_city_state, q)
    for abbr, name in sorted(STATES.items(), key=lambda kv: -len(kv[1])):
        rx = re.compile(r"(?<![A-Za-z])(" + re.escape(name) + r"|" + abbr + r")(?![A-Za-z])", re.I)

        def take(m, name=name, abbr=abbr):
            if len(m.group(1)) == 2 and m.group(1) != abbr:  # "in", "or", "me" in lower case aren't states
                return m.group(0)
            places.append((m.start(), name))
            return " " * len(m.group(0))
        q2 = rx.sub(take, q2)
    # 2. the rest becomes keyword chunks
    groups = []
    for part in _SPLITTERS.split(q2):
        alts = []
        for alt in re.split(r"\bor\b|/", part, flags=re.I):  # "technician or service tech"
            alt = _FILLER.sub(" ", alt)
            words = [w for w in re.findall(r"[A-Za-z0-9+#&.']+", alt) if w.lower() not in _STOP]
            if words:
                alts.append(" ".join(words))
        g = _group_from_terms(alts)
        if g:
            groups.append(g)
    if places:
        g = _group_from_terms(list(dict.fromkeys(name for _, name in sorted(places))))
        if g:
            groups.append(g)
    return groups


def describe(groups: list[Group]) -> str:
    """Groups written back in the exact syntax (editable)."""
    return ", ".join(" | ".join(g.terms) for g in groups)


# --- matching ------------------------------------------------------------------------

@dataclass
class Match:
    hits: list[int]                  # per group
    spans: list[tuple[int, int, int]]  # (start, end, group) in the text

    @property
    def groups_found(self) -> int:
        return sum(1 for h in self.hits if h)

    @property
    def total(self) -> int:
        return sum(self.hits)


def match(text: str, groups: list[Group]) -> Match:
    hits, spans = [], []
    for gi, g in enumerate(groups):
        n = 0
        for p in g.patterns:
            for m in p.finditer(text):
                spans.append((m.start(), m.end(), gi))
                n += 1
        hits.append(n)
    return Match(hits, spans)


def _combined(g: Group) -> re.Pattern:
    """One pattern per group (its alternatives OR'd together), built once."""
    if not hasattr(g, "_one"):
        cs = [p for p in g.patterns if not (p.flags & re.I)]   # case-sensitive ones (state abbreviations)
        ci = [p for p in g.patterns if p.flags & re.I]
        parts = ([f"(?i:{'|'.join(p.pattern for p in ci)})"] if ci else []) + [p.pattern for p in cs]
        g._one = re.compile("|".join(parts))
    return g._one


def counts(text: str, groups: list[Group], lower: str | None = None) -> Match:
    """Hits per group, without positions: fast, for ranking many résumés. A cheap
    substring check skips the pattern when the group's core text isn't there."""
    lower = text.lower() if lower is None else lower
    hits = []
    for g in groups:
        if g.keys and not any((k in text) if cs else (k in lower) for k, cs in g.keys if k):
            hits.append(0)
        else:
            hits.append(len(_combined(g).findall(text)))
    return Match(hits, [])


def fit_label(m: Match, groups: list[Group]) -> str:
    if not groups:
        return ""
    if m.groups_found == len(groups):
        return "strong fit"
    if m.groups_found * 2 >= len(groups):
        return "possible fit"
    return ""


def highlight(text: str, groups: list[Group], max_chars: int = 60000) -> str:
    """HTML of the text with every match wrapped in <mark class="k{colour}">."""
    text = text[:max_chars]
    spans = sorted(match(text, groups).spans, key=lambda s: (s[0], -(s[1] - s[0])))
    out, pos, n = [], 0, 0
    for start, end, gi in spans:
        if start < pos:
            continue  # overlaps an earlier match
        out.append(html.escape(text[pos:start]))
        out.append(f'<mark class="k{groups[gi].color}" id="m{n}">{html.escape(text[start:end])}</mark>')
        pos, n = end, n + 1
    out.append(html.escape(text[pos:]))
    return "".join(out)


def snippets(text: str, groups: list[Group], limit: int = 3, width: int = 70) -> list[str]:
    """Short highlighted extracts around the first matches of different groups."""
    m = match(text, groups)
    seen, out, last_end = set(), [], -1
    for start, end, gi in sorted(m.spans):
        if gi in seen or start < last_end:  # one per group, no overlapping extracts
            continue
        seen.add(gi)
        a, b = max(0, start - width, last_end), min(len(text), end + width)
        last_end = b
        piece = text[a:b].replace("\n", " ")
        out.append(("…" if a else "") + highlight(piece, groups) + ("…" if b < len(text) else ""))
        if len(out) >= limit:
            break
    return out
