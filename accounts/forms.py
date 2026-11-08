from django import forms
from django.contrib.auth import get_user_model


class ProfileForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "email"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if email:
            taken = get_user_model().objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
            if taken.exists():
                raise forms.ValidationError("This email is already used by another account.")
        return email


class CardTraderTokenForm(forms.Form):
    token = forms.CharField(
        label="CardTrader API token",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Found in your CardTrader profile settings, under API. It is checked with CardTrader before saving.",
    )


class OpenRouterKeyForm(forms.Form):
    key = forms.CharField(
        label="OpenRouter API key",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Used to recognise cards from the camera. It is checked with OpenRouter before saving.",
    )
