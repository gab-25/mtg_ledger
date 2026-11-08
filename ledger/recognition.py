"""Turn what the vision model read on a card into a catalog printing (Blueprint)."""

from dataclasses import dataclass, field

from django.core.cache import cache
from django.db.models import Case, IntegerField, Q, Value, When

from . import catalog
from .models import Blueprint

MAX_CANDIDATES = 5


@dataclass
class Recognition:
    """What the model read. language: a Ledger code or "" (unreadable/unsupported); color: a Color code or ""."""

    name: str = ""
    set_code: str = ""
    collector_number: str = ""
    language: str = ""
    foil: bool = False
    type_line: str = ""
    color: str = ""


@dataclass
class Match:
    """blueprint is set only for a unique, certain match. reason: ok | ambiguous | not_found | language."""

    blueprint: Blueprint | None
    candidates: list = field(default_factory=list)
    reason: str = "not_found"


EXPANSIONS_SYNC_KEY = "ledger:expansions-synced"
EXPANSIONS_SYNC_EVERY = 3600  # seconds: unknown set codes refresh the expansion list at most this often


def _expansion_for_code(client, code):
    found = catalog.expansion_by_code(code)
    if found is None and cache.add(EXPANSIONS_SYNC_KEY, True, EXPANSIONS_SYNC_EVERY):
        catalog.sync_expansions(client)
        found = catalog.expansion_by_code(code)
    return found


def _scope(client, rec, expansion):
    """(blueprints to search, certain_allowed). Matches outside a known expansion are never certain."""
    code = rec.set_code.strip().lower()
    if expansion is not None and (not code or code == expansion.code):
        catalog.ensure_blueprints(client, expansion)
        return Blueprint.objects.filter(expansion=expansion), True
    if code:
        # A printed code wins over the locked expansion: boosters hold bonus-sheet and List cards.
        found = _expansion_for_code(client, code)
        if found is not None:
            catalog.ensure_blueprints(client, found)
            return Blueprint.objects.filter(expansion=found), True
    if expansion is not None:
        catalog.ensure_blueprints(client, expansion)
        return Blueprint.objects.filter(expansion=expansion), False
    # No usable set code: search what is already in the local catalog, and let the owner confirm.
    return Blueprint.objects.all(), False


def _by_name(qs, name):
    name = name.strip()
    if not name:
        return qs.none()
    return qs.filter(Q(name__iexact=name) | Q(name__istartswith=f"{name} // "))


def _ordered(qs):
    """Base version first, then by collector number."""
    return list(
        qs.select_related("expansion")
        .annotate(alt=Case(When(version="", then=Value(0)), default=Value(1), output_field=IntegerField()))
        .order_by("alt", "ct_id")[:MAX_CANDIDATES]
    )


def _same_name(bp, name):
    a, b = bp.name.casefold(), name.strip().casefold()
    return a == b or a.split(" // ")[0] == b


def match(client, rec, expansion):
    scope, certain_allowed = _scope(client, rec, expansion)
    number = catalog.normalize_number(rec.collector_number)
    certain = None

    if number:
        hits = list(scope.filter(collector_number=number).select_related("expansion")[:2])
        if len(hits) == 1 and (not rec.name or _same_name(hits[0], rec.name)):
            certain = hits[0]
        elif hits:
            ids = {h.pk for h in hits} | set(_by_name(scope, rec.name).values_list("pk", flat=True))
            return Match(None, _ordered(Blueprint.objects.filter(pk__in=ids)), "ambiguous")

    if certain is None:
        named = _ordered(_by_name(scope, rec.name))
        if not named:
            return Match(None, [], "not_found")
        if len(named) > 1:
            return Match(None, named, "ambiguous")
        certain = named[0]

    if not certain_allowed:
        return Match(None, [certain], "ambiguous")
    if not rec.language:
        return Match(None, [certain], "language")
    return Match(certain, [certain], "ok")
