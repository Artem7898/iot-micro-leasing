"""Align Django's read-only state; SQLModel owns physical application tables."""

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("devices", "0002_usagesession_last_ping_at")]

    operations = [
        migrations.AlterModelOptions(name="device", options={"managed": False}),
        migrations.AlterModelOptions(name="usagesession", options={"managed": False}),
        migrations.AlterField(model_name="device", name="created_at", field=models.DateTimeField()),
        migrations.AlterField(
            model_name="usagesession",
            name="id",
            field=models.CharField(max_length=36, primary_key=True, serialize=False),
        ),
        migrations.RenameField(model_name="usagesession", old_name="device_id", new_name="device"),
        migrations.AddField(
            model_name="usagesession",
            name="created_at",
            field=models.DateTimeField(default=timezone.now),
            preserve_default=False,
        ),
    ]
