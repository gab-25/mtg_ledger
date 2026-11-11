from django import forms

from . import catalog
from .models import Blueprint, Card, CollectionItem, Condition, Language, Listing


def get_or_create_card(name, set_code, collector_number="", **defaults):
    """The printing (set + collector number) when the number is known, else the legacy name + set row."""
    if collector_number:
        card, _ = Card.objects.get_or_create(
            set_code=set_code, collector_number=collector_number, defaults={"name": name, **defaults}
        )
    else:
        card, _ = Card.objects.get_or_create(name=name, set_code=set_code, collector_number="", defaults=defaults)
    return card


PRODUCT_CONDITION = Condition.NEAR_MINT  # stored on products, which have no condition on CardTrader


class CatalogItemForm(forms.Form):
    """Adds copies of a catalog product (card, sealed or accessory) to the owner's collection."""

    blueprint = forms.IntegerField(label="CardTrader blueprint", min_value=1)
    condition = forms.ChoiceField(choices=Condition.choices, initial=Condition.NEAR_MINT, required=False)
    language = forms.ChoiceField(choices=Language.choices, initial=Language.EN)
    foil = forms.BooleanField(required=False)
    quantity = forms.IntegerField(min_value=1, initial=1)
    price = forms.DecimalField(label="Unit price", max_digits=10, decimal_places=2, min_value=0)

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner

    def clean_blueprint(self):
        bp = Blueprint.objects.select_related("expansion", "category").filter(ct_id=self.cleaned_data["blueprint"]).first()
        if bp is None:
            raise forms.ValidationError("Unknown catalog product.")
        return bp

    def save(self):
        """(item, merged): an identical stack of the owner's gets the copies, keeping its price."""
        data = self.cleaned_data
        bp = data["blueprint"]
        condition, language, foil = data["condition"] or Condition.NEAR_MINT, data["language"], data["foil"]
        if bp.is_product:
            condition, foil = PRODUCT_CONDITION, False
            if not bp.accepts("mtg_language"):
                language = Language.EN
        card = catalog.card_for_blueprint(bp)
        item, created = CollectionItem.objects.get_or_create(
            owner=self.owner, card=card, condition=condition, language=language, foil=foil,
            defaults={"quantity": data["quantity"], "price": data["price"]},
        )
        if not created:
            item.quantity += data["quantity"]
            item.save(update_fields=["quantity", "updated_at"])
        return item, not created


class ItemForm(forms.ModelForm):
    """Edits the copies of a collection item; the product they are copies of never changes."""

    class Meta:
        model = CollectionItem
        fields = ["condition", "language", "foil", "quantity", "price"]

    def save(self, commit=True):
        if self.instance.card.is_product:
            self.instance.condition, self.instance.foil = PRODUCT_CONDITION, False
        return super().save(commit=commit)


class ListingForm(forms.ModelForm):
    class Meta:
        model = Listing
        fields = ["quantity", "price", "market_price", "status"]

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        item = self.instance.item  # views always build the form with Listing(item=...)
        if qty > item.quantity:
            copies = "copy" if item.quantity == 1 else "copies"
            raise forms.ValidationError(f"You only own {item.quantity} {copies}.")
        if qty < 1:
            raise forms.ValidationError("List at least one copy.")
        return qty


class CsvImportForm(forms.Form):
    file = forms.FileField(label="CSV file")



class ShipForm(forms.Form):
    tracking_code = forms.CharField(max_length=120, required=False)
