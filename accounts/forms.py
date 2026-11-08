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
