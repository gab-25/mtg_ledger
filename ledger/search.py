"""Catalog search: the words of ?q matched against the start of the words of a blueprint's name, set codes and
collector numbers, accents, case and punctuation ignored, results ordered by relevance."""

import re
import unicodedata

from django.db.models import Case, CharField, Func, IntegerField, Q, Value, When

from .catalog import normalize_number
from .models import CARD_CATEGORY_IDS, Expansion

_NUMBER = re.compile(r"\d+[a-z]?")


class SearchText(Func):
    """The name as the search sees it: SQL function ledger_search_text (migration 0020)."""

    function = "ledger_search_text"
    output_field = CharField()


def normalize(text):
    """Python twin of ledger_search_text: lower case, no accents or apostrophes, other punctuation as one space."""
    text = unicodedata.normalize("NFKD", text.lower().replace("æ", "ae"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['’]", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _word_condition(word, codes):
    """A query word matches the start of a word of the name, an existing set code or, when the query names a set,
    a collector number. Words hold only [a-z0-9], so they are safe inside a regex."""
    cond = Q(st__regex=rf"(^| ){word}")
    if word in codes:
        cond |= Q(expansion__code=word)
    if codes and _NUMBER.fullmatch(word):
        cond |= Q(collector_number=normalize_number(word))
    return cond


def _tie_breaks():
    """Cards (and blueprints not yet categorised) before sealed products, then newest expansion first."""
    is_product = Case(
        When(Q(category__isnull=True) | Q(category__ct_id__in=CARD_CATEGORY_IDS), then=Value(0)),
        default=Value(1), output_field=IntegerField(),
    )
    return [is_product, "-expansion__ct_id", "name", "ct_id"]


def search_blueprints(queryset, q):
    """The blueprints of the queryset matching every word of q, most relevant first."""
    words = normalize(q).split()
    phrase = " ".join(words)
    codes = set(Expansion.objects.filter(code__in=words).values_list("code", flat=True))
    rank = Case(
        When(st=phrase, then=Value(0)), When(st__startswith=phrase, then=Value(1)),
        default=Value(2), output_field=IntegerField(),
    )
    return (queryset.alias(st=SearchText("name"))
            .filter(*(_word_condition(w, codes) for w in words))
            .order_by(rank, *_tie_breaks()))
