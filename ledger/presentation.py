"""Design tokens that depend on data (status badges)."""

from .models import CollectionItem, Listing, Order

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
