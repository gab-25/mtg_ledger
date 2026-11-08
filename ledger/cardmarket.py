"""Cardmarket catalog and price guide (public daily files) plus the Scryfall bridge."""


def suggested_price(card, foil):
    """Cardmarket trend to suggest when listing a copy of card (foil trend for foil copies), or None."""
    product = card.cm_product
    if product is None:
        return None
    return product.trend_foil if foil else product.trend
