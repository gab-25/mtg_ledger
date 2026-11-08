from django.db import migrations


def remove_openrouter_service(apps, schema_editor):
    """OpenRouter is configured only through OPENROUTER_API_KEY: drop the key once saved from the profile."""
    apps.get_model("ledger", "SyncService").objects.filter(name="OpenRouter").delete()


class Migration(migrations.Migration):
    dependencies = [("ledger", "0009_cardmarket")]

    operations = [migrations.RunPython(remove_openrouter_service, migrations.RunPython.noop)]
