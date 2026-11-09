"""Who personal ledger data belongs to."""

from django.contrib.auth import get_user_model


def default_owner():
    """The first superuser (lowest pk), or None: the owner of the data from before users were separated."""
    return get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
