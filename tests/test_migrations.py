"""The history-preserving migrations run forward and roll back.

Driven through ``MigrationExecutor`` against the test database, so these need a real
transaction (``transaction=True``) and leave the schema at the latest migration when done.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest


APP = "vintasend_django_templates_manager"
BEFORE = [(APP, "0001_initial")]
AFTER = [(APP, "0004_keep_status_history_when_a_version_is_deleted")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def at_initial(transactional_db):
    """The schema as 0001 left it; brought back to the latest migration afterwards."""
    apps = _migrate(BEFORE)
    yield apps
    _migrate(AFTER)


def _old_template_with_history(apps, key="welcome", version=1, statuses=("active",)):
    template = apps.get_model(APP, "ManagedTemplate").objects.create(
        name="Welcome",
        description="",
        key=key,
        template_managed_backend="django",
        body_template="Hi",
        version=version,
        status=statuses[-1],
    )
    record_model = apps.get_model(APP, "ManagedTemplateStatusRecord")
    for status in statuses:
        record_model.objects.create(template=template, status=status)
    return template


def test_migrating_forward_copies_each_records_key_and_version(at_initial):
    _old_template_with_history(at_initial, "welcome", 1, ("active", "inactive"))
    _old_template_with_history(at_initial, "receipt", 4, ("active",))

    apps = _migrate(AFTER)

    records = apps.get_model(APP, "ManagedTemplateStatusRecord").objects.order_by("id")
    assert [(r.template_key, r.version, r.status) for r in records] == [
        ("welcome", 1, "active"),
        ("welcome", 1, "inactive"),
        ("receipt", 4, "active"),
    ]


def test_after_migrating_history_outlives_its_version(at_initial):
    _old_template_with_history(at_initial)
    apps = _migrate(AFTER)

    apps.get_model(APP, "ManagedTemplate").objects.get(key="welcome").delete()

    record = apps.get_model(APP, "ManagedTemplateStatusRecord").objects.get()
    assert record.template_id is None
    assert (record.template_key, record.version) == ("welcome", 1)


def test_rolling_back_restores_the_previous_schema(at_initial):
    _old_template_with_history(at_initial)
    _migrate(AFTER)

    apps = _migrate(BEFORE)

    record_model = apps.get_model(APP, "ManagedTemplateStatusRecord")
    assert {field.name for field in record_model._meta.get_fields()} >= {"template", "status"}
    assert "template_key" not in {field.name for field in record_model._meta.get_fields()}
    record = record_model.objects.get()
    assert record.template.key == "welcome"


def test_rolling_back_stops_rather_than_deleting_orphaned_history(at_initial):
    _old_template_with_history(at_initial)
    apps = _migrate(AFTER)
    apps.get_model(APP, "ManagedTemplate").objects.get(key="welcome").delete()

    with pytest.raises(RuntimeError, match="1 status history record"):
        _migrate(BEFORE)

    # Nothing was rolled back, and the orphaned record is still there.
    apps = _migrate(AFTER)
    assert apps.get_model(APP, "ManagedTemplateStatusRecord").objects.count() == 1


def test_the_data_step_is_idempotent(at_initial):
    _old_template_with_history(at_initial)
    _migrate([(APP, "0003_copy_template_key_and_version_onto_status_records")])

    executor = MigrationExecutor(connection)
    migration = executor.loader.get_migration(
        APP, "0003_copy_template_key_and_version_onto_status_records"
    )
    state = executor.loader.project_state(
        [(APP, "0003_copy_template_key_and_version_onto_status_records")]
    )
    migration.operations[0].code(state.apps, None)

    record = state.apps.get_model(APP, "ManagedTemplateStatusRecord").objects.get()
    assert (record.template_key, record.version) == ("welcome", 1)
