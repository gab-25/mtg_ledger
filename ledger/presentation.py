"""Design tokens that depend on data (color pips, status badges)."""

from .models import CollectionItem, Color, Listing

PIP_COLORS = {
    Color.WHITE: "#F3EBD3",
    Color.BLUE: "#6FA8DC",
    Color.BLACK: "#8B8197",
    Color.RED: "#E07A5F",
    Color.GREEN: "#6BAF7B",
    Color.COLORLESS: "#B8B3A9",
    Color.MULTI: "#D4B062",
}

# Colors offered as filter pills in the collection toolbar.
COLOR_FILTERS = [(c.value, c.label) for c in (Color.WHITE, Color.BLUE, Color.BLACK, Color.RED, Color.GREEN)]

_ITEM_BADGES = {
    CollectionItem.Status.COLLECTION: "neutral",
    CollectionItem.Status.FOR_SALE: "gold",
    CollectionItem.Status.PENDING: "pending",
}

_LISTING_BADGES = {
    Listing.Status.PUBLISHED: "live",
    Listing.Status.TO_PUBLISH: "pending",
    Listing.Status.SOLD: "neutral",
}


def pip(color):
    return PIP_COLORS.get(color, PIP_COLORS[Color.COLORLESS])


def item_badge(status):
    return {"label": CollectionItem.Status(status).label, "tone": _ITEM_BADGES[status]}


def listing_badge(status):
    return {"label": Listing.Status(status).label, "tone": _LISTING_BADGES[status]}
