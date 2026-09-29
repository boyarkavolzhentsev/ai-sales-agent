"""Transparent lexical helpers shared by retrieval and the knowledge gate.

Nothing here is semantic. Terms are case-folded, diacritic-free alphanumeric words
(the same splitting as the FTS5 ``unicode61 remove_diacritics 2`` tokenizer), minus a
small English stop-word list. There is no stemming: "price" and "prices" are different
terms, which errs toward under-coverage and therefore toward escalation.
"""

import math
import re
import unicodedata
from collections.abc import Iterable

from app.core.models.base import CoreModel

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Kept deliberately small; only words that carry no subject matter.
STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be but by can could did do does for from had has have how i if in
    is it its me my of on or our please that the their them there these they this to us was
    we were what when where which who why will with would you your""".split()
)

# A question is covered by a text when at least this fraction of its terms occur in it.
COVERAGE_THRESHOLD = 0.5
# Below that, overlap counts as partial only if it is more than incidental: at least this
# many distinct terms and this fraction. A single shared word is treated as incidental.
PARTIAL_MIN_TERMS = 2
PARTIAL_MIN_FRACTION = 0.25


def _fold(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokens(text: str) -> list[str]:
    return [_fold(match) for match in _WORD.findall(text)]


def content_terms(text: str) -> tuple[str, ...]:
    """Distinct non-stop-word terms in first-occurrence order."""
    seen: dict[str, None] = {}
    for token in tokens(text):
        if token not in STOPWORDS:
            seen.setdefault(token, None)
    return tuple(seen)


def fts_match_expression(terms: Iterable[str]) -> str:
    """An FTS5 OR-query in which every term is a quoted string, so no input can inject
    FTS5 operators or column filters."""
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms]
    if not quoted:
        raise ValueError("at least one term is required")
    return " OR ".join(quoted)


class Coverage(CoreModel):
    matched: tuple[str, ...]
    total: int

    @property
    def fraction(self) -> float:
        return len(self.matched) / self.total if self.total else 0.0

    @property
    def covers(self) -> bool:
        return self.total > 0 and self.fraction >= COVERAGE_THRESHOLD

    @property
    def is_partial(self) -> bool:
        return (
            not self.covers
            and len(self.matched) >= PARTIAL_MIN_TERMS
            and self.fraction >= PARTIAL_MIN_FRACTION
        )


def coverage(question_terms: tuple[str, ...], text: str) -> Coverage:
    present = set(tokens(text))
    return Coverage(matched=tuple(t for t in question_terms if t in present), total=len(question_terms))


BM25_K1 = 1.2
BM25_B = 0.75
SCORE_DECIMALS = 9


class EligibleCorpus:
    """Term statistics over the eligible corpus only: every chunk of the usable source
    versions in the query's allowed domains. Ineligible chunks are never added, so they
    cannot influence any score.

    ``score`` is Okapi BM25 over the question's distinct terms:
        sum over terms t present in the chunk of
            idf(t) * tf * (K1 + 1) / (tf + K1 * (1 - B + B * len / avg_len))
        idf(t) = ln(1 + (N - n_t + 0.5) / (n_t + 0.5))
    where tf is t's count in the chunk, len the chunk's term count, N the number of
    eligible chunks, n_t how many contain t, and avg_len their mean length. Higher means
    more lexical relevance; 0 means no question term occurs. It is a relative lexical
    measure within one query and eligible corpus, not a probability or a similarity.
    Scores are rounded to SCORE_DECIMALS so equal inputs give bit-identical results.
    """

    def __init__(self, texts: Iterable[str]) -> None:
        self._document_frequency: dict[str, int] = {}
        total_length = 0
        self._size = 0
        for text in texts:
            terms = tokens(text)
            self._size += 1
            total_length += len(terms)
            for term in set(terms):
                self._document_frequency[term] = self._document_frequency.get(term, 0) + 1
        self._average_length = total_length / self._size if self._size else 0.0

    @property
    def size(self) -> int:
        return self._size

    def idf(self, term: str) -> float:
        containing = self._document_frequency.get(term, 0)
        return math.log(1 + (self._size - containing + 0.5) / (containing + 0.5))

    def score(self, question_terms: tuple[str, ...], text: str) -> float:
        terms = tokens(text)
        if not terms or self._average_length == 0:
            return 0.0
        counts: dict[str, int] = {}
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
        norm = BM25_K1 * (1 - BM25_B + BM25_B * len(terms) / self._average_length)
        total = 0.0
        for term in question_terms:  # fixed order: deterministic summation
            tf = counts.get(term, 0)
            if tf:
                total += self.idf(term) * tf * (BM25_K1 + 1) / (tf + norm)
        return round(total, SCORE_DECIMALS)
