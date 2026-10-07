"""Every ``select_for_update`` the backend issues must run inside ``transaction.atomic()``.

On PostgreSQL and MySQL, Django refuses to evaluate a ``select_for_update`` queryset in
autocommit mode and raises ``TransactionManagementError``. SQLite, which the suite runs on,
has no ``SELECT ... FOR UPDATE``, so Django skips that check and a lock taken outside a
transaction goes unnoticed -- until a host without ``ATOMIC_REQUESTS`` calls the backend.

These tests turn the check back on: the connection claims to support ``FOR UPDATE`` (which
arms Django's autocommit guard) while the clause itself renders as nothing, so SQLite still
receives valid SQL. They run with ``transaction=True`` so the test itself is not wrapped in a
transaction that would hide the bug.
"""

from unittest import mock

from django.db import connection

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateCreateInput,
    ManagedTemplateUpdateInput,
)

from vintasend_django_templates_manager.models import ManagedTemplate, ManagedTemplateStatusRecord


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def enforce_select_for_update():
    assert connection.get_autocommit(), "the test must run outside a transaction"
    with (
        mock.patch.object(connection.features, "has_select_for_update", True),
        mock.patch.object(connection.ops, "for_update_sql", return_value=""),
    ):
        yield


def test_status_update_locks_inside_a_transaction(manager, make_template):
    template = make_template(key="welcome", version=1)

    manager.create_template_status_update("welcome", 1, ManagedTemplateStatus.ACTIVE)

    template.refresh_from_db()
    assert template.status == ManagedTemplateStatus.ACTIVE.value
    assert ManagedTemplateStatusRecord.objects.get().template == template


def test_delete_of_a_pinned_version_locks_inside_a_transaction(manager, make_template):
    make_template(key="welcome", version=2)

    manager.delete_template("welcome", 2)

    assert not ManagedTemplate.objects.filter(key="welcome").exists()


def test_delete_of_the_latest_version_locks_inside_a_transaction(manager, make_template):
    make_template(key="welcome", version=2)

    manager.delete_template("welcome")

    assert not ManagedTemplate.objects.filter(key="welcome").exists()


def test_update_locks_inside_a_transaction(manager, make_template):
    make_template(key="welcome", version=1)

    updated = manager.update_template(
        "welcome",
        ManagedTemplateUpdateInput(
            name="Renamed",
            description=None,
            template_body=None,
            template_subject=None,
            template_preheader=None,
        ),
    )

    assert updated.version == 2


def test_tag_rename_locks_inside_a_transaction(manager, make_tag):
    make_tag("Blak Friday")

    renamed = manager.update_tag("blak-friday", "Black Friday")

    assert renamed.slug == "black-friday"


def test_recreating_a_key_whose_versions_were_deleted_locks_inside_a_transaction(
    manager, make_template
):
    """The key's surviving history is what two concurrent recreates share, so it is locked."""
    make_template(key="welcome", version=1)
    manager.create_template_status_update("welcome", 1, ManagedTemplateStatus.DRAFT)
    manager.delete_template("welcome", 1)

    recreated = manager.create_template(
        ManagedTemplateCreateInput(
            name="Welcome",
            description="",
            key="welcome",
            template_managed_backend="django",
            template_body="Hi",
            template_subject=None,
            template_preheader=None,
            tenant=None,
        )
    )

    assert recreated.version == 2
