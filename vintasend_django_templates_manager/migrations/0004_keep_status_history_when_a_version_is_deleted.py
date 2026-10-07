import django.db.models.deletion
from django.db import migrations, models


def refuse_to_roll_back_over_orphaned_history(apps, schema_editor):
    """Stop the rollback if any record has outlived its version.

    Rolling back makes ``template`` required and ``CASCADE`` again, which a record whose version
    was deleted cannot satisfy. Deleting those records here would delete the audit trail this
    migration exists to keep, so the rollback stops instead and names how many there are. To roll
    back anyway, export those records (``template`` is null; ``template_key`` and ``version`` say
    which version they were about), delete them deliberately, and run the rollback again.
    """
    record_model = apps.get_model("vintasend_django_templates_manager", "ManagedTemplateStatusRecord")
    orphaned = record_model.objects.filter(template__isnull=True).count()
    if orphaned:
        raise RuntimeError(
            f"Cannot roll back vintasend_django_templates_manager 0004: {orphaned} status "
            "history record(s) belong to template versions that have been deleted, and the "
            "previous schema cannot hold them. Export and delete those records "
            "(ManagedTemplateStatusRecord with template=None) first, then roll back again."
        )


class Migration(migrations.Migration):
    """Keep status history when its template version is deleted.

    ``template`` becomes nullable and ``SET_NULL``, and ``template_key`` / ``version`` become
    required now that ``0003`` has filled them in.
    """

    dependencies = [
        ("vintasend_django_templates_manager", "0003_copy_template_key_and_version_onto_status_records"),
    ]

    operations = [
        migrations.AlterField(
            model_name="managedtemplatestatusrecord",
            name="template_key",
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name="managedtemplatestatusrecord",
            name="version",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="managedtemplatestatusrecord",
            name="template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="history",
                to="vintasend_django_templates_manager.managedtemplate",
            ),
        ),
        migrations.AddIndex(
            model_name="managedtemplatestatusrecord",
            index=models.Index(
                fields=["template_key", "version"], name="vintasend_mt_history_key_ver"
            ),
        ),
        # Last, so that on rollback it runs first: before the foreign key is made required.
        migrations.RunPython(
            migrations.RunPython.noop, refuse_to_roll_back_over_orphaned_history
        ),
    ]
