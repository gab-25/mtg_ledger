from django import forms

from .catalog import normalize_number
from .models import Card, CollectionItem, Color, Listing


def get_or_create_card(name, set_code, collector_number="", **defaults):
    """The printing (set + collector number) when the number is known, else the legacy name + set row."""
    if collector_number:
        card, _ = Card.objects.get_or_create(
            set_code=set_code, collector_number=collector_number, defaults={"name": name, **defaults}
        )
    else:
        card, _ = Card.objects.get_or_create(name=name, set_code=set_code, collector_number="", defaults=defaults)
    return card


class CardItemForm(forms.ModelForm):
    """Adds or edits a collection item together with the card printing it refers to."""

    name = forms.CharField(max_length=200)
    set_code = forms.CharField(label="Set", max_length=10)
    collector_number = forms.CharField(label="Collector number", max_length=12, required=False)
    type_line = forms.CharField(label="Type", max_length=200, required=False)
    color = forms.ChoiceField(choices=Color.choices, initial=Color.COLORLESS)

    field_order = ["name", "set_code", "collector_number", "type_line", "color", "condition", "language", "foil", "quantity", "price"]

    class Meta:
        model = CollectionItem
        fields = ["condition", "language", "foil", "quantity", "price"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            card = self.instance.card
            self.initial.update(
                name=card.name, set_code=card.set_code, collector_number=card.collector_number,
                type_line=card.type_line, color=card.color,
            )

    def clean_set_code(self):
        return self.cleaned_data["set_code"].strip().upper()

    def clean_collector_number(self):
        return normalize_number(self.cleaned_data["collector_number"])

    def save(self, commit=True):
        data = self.cleaned_data
        card = get_or_create_card(
            data["name"].strip(), data["set_code"], data["collector_number"],
            type_line=data["type_line"], color=data["color"],
        )
        changed = False
        for attr in ("type_line", "color"):
            if data[attr] and getattr(card, attr) != data[attr]:
                setattr(card, attr, data[attr])
                changed = True
        if changed:
            card.save()
        self.instance.card = card
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
