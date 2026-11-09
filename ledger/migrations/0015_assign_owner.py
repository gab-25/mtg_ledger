"""Give the data of the single-user ledger to its owner: the first superuser.

The CardTrader token saved in the database becomes that user's own token; an install that only set
CARDTRADER_API_TOKEN in the environment gets that value copied in, since the variable is no longer read.
"""

import os

from django.conf import settings
from django.db import migrations

PERSONAL = ("CollectionItem", "Order", "ScanSession", "SyncLog")


def assign_owner(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    SyncService = apps.get_model("ledger", "SyncService")
    models = [apps.get_model("ledger", name) for name in PERSONAL]
    if not SyncService.objects.exists() and not any(m.objects.exists() for m in models):
        return
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    if owner is None:
        raise RuntimeError(
            "Existing ledger data needs an owner: create a superuser first (manage.py createsuperuser), "
            "then migrate again."
        )
    for model in models:
        model.objects.filter(owner__isnull=True).update(owner=owner)
    SyncService.objects.filter(user__isnull=True).update(user=owner)
    env_token = os.environ.get("CARDTRADER_API_TOKEN", "").strip()
    if env_token:
        SyncService.objects.filter(user=owner, name="CardTrader", api_token="").update(api_token=env_token)


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0014_owners"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(assign_owner, migrations.RunPython.noop),
    ]
