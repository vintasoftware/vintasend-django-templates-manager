from django.db import migrations
from django.db.models import OuterRef, Subquery


def copy_key_and_version(apps, schema_editor):
    """Copy each record's version key and number from the version it belongs to.

    Only records still missing them are touched, so running it again changes nothing. Before
    ``0004`` every record has a version -- the foreign key is still ``CASCADE`` and not null --
    so every record gets both values.
    """
    record_model = apps.get_model("vintasend_django_templates_manager", "ManagedTemplateStatusRecord")
    template_model = apps.get_model("vintasend_django_templates_manager", "ManagedTemplate")
    template = template_model.objects.filter(pk=OuterRef("template_id"))
    record_model.objects.filter(template_key__isnull=True).update(
        template_key=Subquery(template.values("key")[:1]),
        version=Subquery(template.values("version")[:1]),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("vintasend_django_templates_manager", "0002_status_record_template_key_and_version"),
    ]

    operations = [
        # Rolling back needs no data step: reversing 0002 drops both columns.
        migrations.RunPython(copy_key_and_version, migrations.RunPython.noop),
    ]
