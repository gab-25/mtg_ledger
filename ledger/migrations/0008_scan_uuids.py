import uuid

from django.db import migrations, models


def fill_uuids(apps, schema_editor):
    for name in ("ScanSession", "ScanLine"):
        model = apps.get_model("ledger", name)
        for row in model.objects.filter(uuid__isnull=True).only("pk"):
            row.uuid = uuid.uuid4()
            row.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [("ledger", "0007_scan_capture")]

    operations = [
        migrations.AddField("scansession", "uuid", models.UUIDField(null=True, editable=False)),
        migrations.AddField("scanline", "uuid", models.UUIDField(null=True, editable=False)),
        migrations.RunPython(fill_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            "scansession", "uuid", models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
        ),
        migrations.AlterField(
            "scanline", "uuid", models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
        ),
    ]
