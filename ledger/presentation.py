"""Design tokens that depend on data (color pips, status badges)."""

from .models import CollectionItem, Color, Listing, Order

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


_S = Order.State
# Shipments page tabs: key -> (label, states shown)
ORDER_TABS = {
    "to_ship": ("To ship", [_S.PAID]),
    "ct_zero": ("CT Zero pending", [_S.HUB_PENDING]),
    "shipped": ("Shipped", [_S.SENT]),
    "completed": ("Completed", [_S.ARRIVED, _S.DONE]),
    "issues": ("Issues", [_S.REQUEST_FOR_CANCEL, _S.CANCELED, _S.LOST]),
}

_ORDER_TONES = {
    _S.PAID: "pending",
    _S.HUB_PENDING: "gold",
    _S.SENT: "live",
    _S.ARRIVED: "live",
    _S.DONE: "neutral",
    _S.CLOSED: "neutral",
    _S.REQUEST_FOR_CANCEL: "danger",
    _S.CANCELED: "danger",
    _S.LOST: "danger",
}


def order_badge(state):
    try:
        label = Order.State(state).label
    except ValueError:
        label = state.replace("_", " ").capitalize()
    return {"label": label, "tone": _ORDER_TONES.get(state, "neutral")}
