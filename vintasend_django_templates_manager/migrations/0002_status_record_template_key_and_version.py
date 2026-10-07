from django.db import migrations, models


class Migration(migrations.Migration):
    """Give each status record its own copy of its version's key and number.

    Nullable for now: ``0003`` fills them in for the records that already exist, and ``0004``
    makes them required. Three migrations rather than one so the data copy never shares a
    transaction with a schema change on the same table, which PostgreSQL can refuse while
    deferred foreign-key checks are still pending.
    """

    dependencies = [
        ("vintasend_django_templates_manager", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="managedtemplatestatusrecord",
            name="template_key",
            field=models.CharField(max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="managedtemplatestatusrecord",
            name="version",
            field=models.PositiveIntegerField(null=True),
        ),
    ]
