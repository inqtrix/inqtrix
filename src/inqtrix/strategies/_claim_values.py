"""The values a claim states: dates, percentages and amounts.

A value belongs to what a claim says. "Artikel 50 gilt ab dem 2. August 2025"
and "Artikel 50 gilt ab dem 2. August 2026" make the same statement about
different dates, so the claim signature leaves the values out and the
consolidator splits a signature group by the claim's normalized values:
equal values support each other, and :func:`values_conflict` decides when
different values contradict each other.

Every other number stays in the signature as part of what the claim is
about: an article, a paragraph or a regulation number, a count. An
upper-case Roman numeral does too ("Kapitel II" against "Kapitel V").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from inqtrix.text import NEGATION_TOKENS, STOPWORDS, norm_match_token, number_spans, tokenize

_MONTHS: dict[str, int] = {
    "januar": 1, "jänner": 1, "january": 1, "jan": 1,
    "februar": 2, "feber": 2, "february": 2, "feb": 2,
    "märz": 3, "maerz": 3, "march": 3, "mär": 3, "mrz": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}
"""German and English month names and their usual abbreviations. A month is
only read from a capitalized word, so the English verb "may" stays a word."""

_MONTH_WORD = r"[A-ZÄÖÜ][a-zäöü]{2,8}"
_DAY_MONTH_YEAR = re.compile(
    rf"(?<![\d.])(\d{{1,2}})(?:\.\s*|\s+)({_MONTH_WORD})\.?\s+(\d{{4}})(?!\d)"
)
_MONTH_DAY_YEAR = re.compile(
    rf"\b({_MONTH_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)"
)
_NUMERIC_DATE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d|\.\d)")
_ISO_DATE = re.compile(r"(?<![\d\-])(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTH_YEAR = re.compile(rf"\b({_MONTH_WORD})\.?\s+(\d{{4}})(?!\d)")
_YEAR = re.compile(r"^(?:19\d{2}|20\d{2}|2100)$")
_RANGE_DASH = re.compile(r"\s*[–-]\s*")

_PERCENT_AFTER = re.compile(
    r"\s*(%|prozentpunkt\w*|percentage points?\b|pp\b|prozent\b|per\s?cent\b|percent\b)",
    re.IGNORECASE,
)
_SCALE_AFTER = re.compile(
    r"\s*(Tsd\.|Tausend\b|thousand\b|Mio\.?|Millionen\b|Million\b|millions?\b"
    r"|Mrd\.?|Milliarden\b|Milliarde\b|billions?\b|bn\b)"
)
_SCALES: dict[str, int] = {
    "tsd.": 10**3, "tausend": 10**3, "thousand": 10**3,
    "mio": 10**6, "mio.": 10**6, "million": 10**6, "millionen": 10**6, "millions": 10**6,
    "mrd": 10**9, "mrd.": 10**9, "milliarde": 10**9, "milliarden": 10**9,
    "billion": 10**9, "billions": 10**9, "bn": 10**9,
}
"""Scale words a reader takes as part of an amount. German words are
capitalized and English ones are not, which keeps the German "Billion"
(10^12) apart from the English "billion" (10^9); the German word is left
out on purpose."""

_CURRENCY_AFTER = re.compile(
    r"\s*(€|EUR\b|Euro\b|US-Dollar\b|USD\b|Dollar\b|\$|£|GBP\b|Pfund\b|CHF\b|Franken\b)"
)
_CURRENCY_BEFORE = re.compile(r"(€|EUR|USD|US\$|\$|£|GBP|CHF)\s*$")
_CURRENCIES: dict[str, str] = {
    "€": "EUR", "eur": "EUR", "euro": "EUR",
    "$": "USD", "us$": "USD", "usd": "USD", "dollar": "USD", "us-dollar": "USD",
    "£": "GBP", "gbp": "GBP", "pfund": "GBP",
    "chf": "CHF", "franken": "CHF",
}

_INEXACT_WORDS = frozenset({
    "rund", "etwa", "ca", "circa", "zirka", "ungefähr", "knapp", "fast", "über",
    "unter", "mindestens", "höchstens", "maximal", "bis", "about", "around",
    "approximately", "approx", "roughly", "nearly", "almost", "over", "under",
    "above", "below", "max", "min",
})
_INEXACT_PAIRS = frozenset({
    ("mehr", "als"), ("weniger", "als"), ("bis", "zu"), ("more", "than"),
    ("less", "than"), ("fewer", "than"), ("up", "to"), ("at", "least"), ("at", "most"),
})
"""Words directly before a percentage or an amount that make it approximate
or a bound ("rund 30 %", "bis zu 35 Mio. Euro"). Such a value never
contradicts another one; dates take no such marker ("bis 2. August 2026" is
a date like any other)."""

_WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+")
_TOKEN = re.compile(r"[a-zA-Z0-9äöüÄÖÜß]+")
_ROMAN = re.compile(r"[IVXLCDM]{1,4}")


@dataclass(frozen=True, slots=True)
class ClaimValue:
    """One value a claim states.

    ``kind`` is ``date``, ``percent``, ``points`` (percentage points) or
    ``amount``. ``value`` is normalized: ``2026-08-02``, ``2026-08`` or
    ``2026`` for a date, a plain decimal for a percentage, the decimal and
    the currency code for an amount (``35000000 EUR``). ``exact`` is
    ``False`` for an approximate value or a bound, and for a number whose
    separator a reader cannot settle without the language (``1,250``); then
    ``value`` keeps the number as written.
    """

    kind: str
    value: str
    exact: bool = True

    def label(self) -> str:
        return f"{self.kind}:{self.value}" + ("" if self.exact else "~")


@dataclass(frozen=True, slots=True)
class ClaimValues:
    """The values of one claim, its time scope and where they stand.

    ``scope`` holds the dates of a claim that also states a percentage or an
    amount: there the date says WHEN the other value holds ("steigt 2026 auf
    13,90 Euro"), so it is part of what the claim is about, not a value to
    compare. ``spans`` are the character spans of every value, with its unit,
    scale and currency, so the signature can leave them out.
    """

    values: tuple[ClaimValue, ...] = ()
    scope: tuple[ClaimValue, ...] = ()
    spans: tuple[tuple[int, int], ...] = ()

    @property
    def key(self) -> str:
        """The values as one canonical string; ``""`` without any value."""
        if not self.values and not self.scope:
            return ""
        values = "|".join(sorted(value.label() for value in self.values))
        scope = "|".join(sorted(value.label() for value in self.scope))
        return f"{values}#{scope}" if scope else values


def claim_values(text: str) -> ClaimValues:
    """The dates, percentages and amounts *text* states."""
    text = text or ""
    found: list[tuple[int, int, ClaimValue]] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= left or start >= right for left, right in taken)

    def add_date(start: int, end: int, year: str, month: int = 0, day: int = 0) -> None:
        if not free(start, end) or not _YEAR.match(year):
            return
        if month and not 1 <= month <= 12 or day and not 1 <= day <= 31:
            return
        value = year
        if month:
            value += f"-{month:02d}"
            if day:
                value += f"-{day:02d}"
        found.append((start, end, ClaimValue("date", value)))
        taken.append((start, end))

    for match in _DAY_MONTH_YEAR.finditer(text):
        month = _MONTHS.get(match.group(2).lower(), 0)
        if month:
            add_date(*match.span(), match.group(3), month, int(match.group(1)))
    for match in _MONTH_DAY_YEAR.finditer(text):
        month = _MONTHS.get(match.group(1).lower(), 0)
        if month:
            add_date(*match.span(), match.group(3), month, int(match.group(2)))
    for match in _NUMERIC_DATE.finditer(text):
        add_date(*match.span(), match.group(3), int(match.group(2)), int(match.group(1)))
    for match in _ISO_DATE.finditer(text):
        add_date(*match.span(), match.group(1), int(match.group(2)), int(match.group(3)))
    for match in _MONTH_YEAR.finditer(text):
        month = _MONTHS.get(match.group(1).lower(), 0)
        if month:
            add_date(*match.span(), match.group(2), month)

    for start, end in number_spans(text):
        if not free(start, end):
            continue
        found.extend(_number_values(text, start, end, taken))

    dated = [value for _, _, value in found if value.kind == "date"]
    others = [value for _, _, value in found if value.kind != "date"]
    return ClaimValues(
        values=tuple(others) if others else tuple(dated),
        scope=tuple(dated) if others else (),
        spans=tuple(sorted((start, end) for start, end, _ in found)),
    )


def _number_values(
    text: str,
    start: int,
    end: int,
    taken: list[tuple[int, int]],
) -> list[tuple[int, int, ClaimValue]]:
    """The values one number states: a percentage, an amount, a year, a
    range of either, or nothing when the number is part of what the claim is
    about (an article, a count)."""
    raw = text[start:end]
    parts = _RANGE_DASH.split(raw) if re.search(r"\d[–-]\d", raw) else [raw]
    if len(parts) > 2:
        return []
    tail = end
    percent = _PERCENT_AFTER.match(text, tail)
    scale = currency = None
    if percent:
        tail = percent.end()
        kind = "points" if percent.group(1).lower().startswith(("prozentpunkt", "percentage", "pp")) else "percent"
    else:
        scale = _SCALE_AFTER.match(text, tail)
        if scale:
            tail = scale.end()
        currency = _CURRENCY_AFTER.match(text, tail)
        if currency:
            tail = currency.end()
            if not scale:
                # "115 USD billion": the scale may follow the currency.
                scale = _SCALE_AFTER.match(text, tail)
                if scale:
                    tail = scale.end()
        kind = "amount"
    head = start
    before = _CURRENCY_BEFORE.search(text, 0, start) if not percent else None
    if before:
        head = before.start()
    if kind == "amount" and not (scale or currency or before):
        if all(_YEAR.match(part) for part in parts):
            spans = (start, end)
            taken.append(spans)
            return [(start, end, ClaimValue("date", part)) for part in parts]
        return []
    code = ""
    if kind == "amount":
        symbol = (currency.group(1) if currency else before.group(1) if before else "").lower()
        code = _CURRENCIES.get(symbol, "")
    factor = _SCALES.get(scale.group(1).lower(), 1) if scale else 1
    exact = not _marked_inexact(text, head)
    values = []
    for part in parts:
        number, ambiguous = _decimal(part)
        if number is None:
            # Kept as written, with its scale word: "1,250 Mio." and
            # "1,250" must not read as one value.
            value = part.strip() + (f" {scale.group(1)}" if scale else "")
        else:
            value = format((number * factor).normalize(), "f")
        if kind == "amount" and code:
            value = f"{value} {code}"
        values.append(ClaimValue(kind, value, exact and not ambiguous and number is not None))
    taken.append((head, tail))
    return [(head, tail, value) for value in values]


def _marked_inexact(text: str, head: int) -> bool:
    words = [word.lower() for word in _WORD.findall(text[max(0, head - 40):head])]
    if words and words[-1] in _INEXACT_WORDS:
        return True
    return len(words) >= 2 and (words[-2], words[-1]) in _INEXACT_PAIRS


def _decimal(raw: str) -> tuple[Decimal | None, bool]:
    """The number *raw* writes and whether its separator is ambiguous.

    Both separators: the last one is the decimal point (``1.500.000,50``,
    ``1,500,000.50``). A point before exactly three digits groups thousands
    (``13.000``); a comma before exactly three digits may group thousands
    (English ``1,250``) or start three decimals (German ``1,250``), so the
    number stays as written.
    """
    cleaned = re.sub(r"[    '’]", "", raw)
    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        pieces = cleaned.split(",")
        if len(pieces) > 2:
            cleaned = "".join(pieces)
        elif len(pieces[1]) == 3:
            return None, True
        else:
            cleaned = cleaned.replace(",", ".")
    elif "." in cleaned:
        pieces = cleaned.split(".")
        if len(pieces) > 2 or len(pieces[1]) == 3:
            cleaned = "".join(pieces)
    try:
        return Decimal(cleaned), False
    except InvalidOperation:
        return None, True


def signature_tokens(text: str) -> list[tuple[str, bool]]:
    """The lower-cased word tokens of *text* without its values, each with
    whether it is identity that no length filter may drop: a token with a
    digit ("50", "1a", "2024") or an upper-case Roman numeral ("II")."""
    values = claim_values(text)
    blanked = list(text or "")
    for start, end in values.spans:
        blanked[start:end] = " " * (end - start)
    tokens: list[tuple[str, bool]] = []
    for match in _TOKEN.finditer("".join(blanked)):
        token = match.group(0)
        identity = any(char.isdigit() for char in token) or bool(_ROMAN.fullmatch(token))
        tokens.append((token.lower(), identity))
    return tokens


def content_words(text: str) -> list[str]:
    """The words a claim in *text* is about, normalized for matching: its
    tokens without values (:func:`signature_tokens`), without identity, two
    characters or fewer, stopwords and negations."""
    return [
        norm_match_token(token)
        for token, is_identity in signature_tokens(text)
        if not is_identity
        and len(token) > 2
        and token not in STOPWORDS
        and token not in NEGATION_TOKENS
    ]


def values_conflict(first: ClaimValues, second: ClaimValues) -> bool:
    """Whether two claims of one signature contradict each other by value.

    Precision first: both have the same time scope, and for some kind each
    states exactly ONE exact value, of the same currency for an amount, and
    the two differ, a date only when neither contains the other ("2026"
    against "2026-08-02" is the same date, read coarser). A claim with
    several values of a kind (a start and an end, a range) contradicts
    nothing here; that is left to a reader of both.
    """
    if sorted(value.label() for value in first.scope) != sorted(
        value.label() for value in second.scope
    ):
        return False
    for kind in {value.kind for value in first.values} & {value.kind for value in second.values}:
        ours = [value for value in first.values if value.kind == kind]
        theirs = [value for value in second.values if value.kind == kind]
        if len(ours) != 1 or len(theirs) != 1:
            continue
        one, other = ours[0], theirs[0]
        if not (one.exact and other.exact) or one.value == other.value:
            continue
        if kind == "amount" and _currency(one) != _currency(other):
            continue
        if kind == "date" and (
            one.value.startswith(other.value) or other.value.startswith(one.value)
        ):
            continue
        return True
    return False


def _currency(value: ClaimValue) -> str:
    _, _, code = value.value.partition(" ")
    return code


def page_states_claim(claim_text: str, page_text: str) -> bool:
    """Whether a source's own text states a claim, as far as words can tell.

    *page_text* is text of the cited page itself: a snippet the search
    provider returned from the page, never the provider's own answer. The
    claim must have a key value (a date, a percentage, an amount or an
    identity number), every key value must stand in the page text with the
    same normalized value (a year does not stand for a day in it, a rounded
    figure not for another), and at least half of the claim's other content
    words must occur there too, two at least when the claim has two. A claim
    without a key value is never checked this way: shared words do not show
    that a page states a sentence.
    """
    if not (page_text or "").strip():
        return False
    claim = claim_values(claim_text)
    tokens = signature_tokens(claim_text)
    stated = {(value.kind, value.value) for value in (*claim.values, *claim.scope)}
    identity = {norm_match_token(token) for token, is_identity in tokens if is_identity}
    if not stated and not identity:
        return False
    page = claim_values(page_text)
    if not stated <= {(value.kind, value.value) for value in (*page.values, *page.scope)}:
        return False
    page_words = {norm_match_token(token) for token in tokenize(page_text)}
    if not identity <= page_words:
        return False
    content = content_words(claim_text)
    if not content:
        return True
    needed = min(len(content), max(2, (len(content) + 1) // 2))
    return sum(1 for token in content if token in page_words) >= needed
