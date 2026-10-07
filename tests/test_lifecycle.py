"""The two lifecycle rules: which version a send renders, and which versions may be deleted.

``get_active_template`` answers the send path natively (the newest ACTIVE version), and
``delete_template`` refuses any version that was ever published. Status history is never
deleted: a record keeps its own key and version, so it is still readable after its version is
gone.
"""

from django.db import IntegrityError

import pytest
from vintasend.services.dataclasses import Notification
from vintasend.services.notification_template_renderers.base import BaseNotificationTemplateRenderer
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    TemplatedEmail,
)
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import (
    ManagedTemplateDeletionNotAllowedError,
    ManagedTemplateNoActiveVersionError,
    ManagedTemplateNotFoundError,
)
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from vintasend_django_templates_manager.django_templates_manager import DjangoTemplateManager
from vintasend_django_templates_manager.models import ManagedTemplate, ManagedTemplateStatusRecord


ACTIVE = ManagedTemplateStatus.ACTIVE
INACTIVE = ManagedTemplateStatus.INACTIVE
ARCHIVED = ManagedTemplateStatus.ARCHIVED
DRAFT = ManagedTemplateStatus.DRAFT

PUBLISHED_STATUSES = [
    pytest.param([ACTIVE], id="active"),
    pytest.param([ACTIVE, INACTIVE], id="inactive"),
    pytest.param([ARCHIVED], id="archived"),
]


class SourceRenderer(BaseNotificationTemplateRenderer):
    """Hands the stored body back as the rendered body."""

    def render(self, notification, context):
        raise AssertionError("the managed renderer must not call render")

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        return TemplatedEmail(subject="", body=template_content.body_template)


def _move(manager, key, version, statuses, changed_by=None):
    for status in statuses:
        manager.create_template_status_update(key, version, status, changed_by=changed_by)


# ----------------------------------------------------------------------
# get_active_template
# ----------------------------------------------------------------------


class TestGetActiveTemplate:
    def test_is_implemented_natively(self):
        assert "get_active_template" in vars(DjangoTemplateManager)

    def test_skips_a_newer_draft(self, manager, make_template):
        make_template(key="welcome", version=1, status=ACTIVE.value)
        make_template(key="welcome", version=2, status=DRAFT.value)

        assert manager.get_active_template("welcome").version == 1

    def test_the_highest_numbered_active_version_wins(self, manager, make_template):
        for version in (2, 10, 3):
            make_template(key="welcome", version=version, status=ACTIVE.value)
        make_template(key="welcome", version=11, status=INACTIVE.value)

        # Compared as numbers: 10 beats 2 and 3, which a string comparison would get wrong.
        assert manager.get_active_template("welcome").version == 10

    def test_does_not_answer_with_another_keys_version(self, manager, make_template):
        make_template(key="welcome", version=1, status=DRAFT.value)
        make_template(key="welcome-back", version=5, status=ACTIVE.value)

        with pytest.raises(ManagedTemplateNoActiveVersionError):
            manager.get_active_template("welcome")

    def test_a_key_with_only_drafts_or_retired_versions_has_no_active_version(
        self, manager, make_template
    ):
        make_template(key="welcome", version=1, status=ARCHIVED.value)
        make_template(key="welcome", version=2, status=DRAFT.value)

        with pytest.raises(ManagedTemplateNoActiveVersionError, match="'welcome'"):
            manager.get_active_template("welcome")

    def test_a_missing_key_raises_the_plain_not_found_error(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError) as raised:
            manager.get_active_template("nowhere")

        assert type(raised.value) is ManagedTemplateNotFoundError


class TestSendsThroughTheRenderer:
    """End to end: the library's renderer over this backend."""

    @pytest.fixture
    def renderer(self, manager):
        return ManagedTemplateEmailRenderer(manager, SourceRenderer())

    @pytest.fixture
    def notification(self):
        return Notification(
            id=1,
            user_id=1,
            notification_type="EMAIL",
            title="Welcome",
            body_template="welcome",
            context_name="ctx",
            context_kwargs={},
            send_after=None,
            subject_template="",
            preheader_template="",
            status="PENDING_SEND",
        )

    def test_an_unpinned_send_renders_the_active_version_not_the_draft(
        self, renderer, notification, make_template
    ):
        make_template(key="welcome", version=1, status=ACTIVE.value, body_template="v1")
        make_template(key="welcome", version=2, status=DRAFT.value, body_template="v2 draft")

        email = renderer.render(notification, {})

        assert email.body == "v1"
        assert email.template_version == 1
        assert renderer.get_latest_template_version("welcome") == 1


# ----------------------------------------------------------------------
# delete_template
# ----------------------------------------------------------------------


class TestDeletionRule:
    def test_a_never_published_draft_can_be_deleted(self, manager, make_template):
        make_template(key="welcome", version=1)

        manager.delete_template("welcome", 1)

        assert not ManagedTemplate.objects.filter(key="welcome").exists()

    @pytest.mark.parametrize("statuses", PUBLISHED_STATUSES)
    def test_a_published_version_is_refused_by_number(self, manager, make_template, statuses):
        make_template(key="welcome", version=1)
        _move(manager, "welcome", 1, statuses)

        with pytest.raises(ManagedTemplateDeletionNotAllowedError, match="archive"):
            manager.delete_template("welcome", 1)

        assert ManagedTemplate.objects.filter(key="welcome", version=1).exists()

    @pytest.mark.parametrize("statuses", PUBLISHED_STATUSES)
    def test_a_published_latest_version_is_refused_with_no_version(
        self, manager, make_template, statuses
    ):
        make_template(key="welcome", version=1)
        make_template(key="welcome", version=2)
        _move(manager, "welcome", 2, statuses)

        with pytest.raises(ManagedTemplateDeletionNotAllowedError):
            manager.delete_template("welcome")

        assert ManagedTemplate.objects.filter(key="welcome").count() == 2

    def test_a_draft_that_was_active_before_is_refused(self, manager, make_template):
        make_template(key="welcome", version=1)
        _move(manager, "welcome", 1, [ACTIVE, DRAFT])
        assert ManagedTemplate.objects.get(key="welcome").status == DRAFT.value

        with pytest.raises(ManagedTemplateDeletionNotAllowedError):
            manager.delete_template("welcome", 1)

    def test_a_published_version_is_deleted_when_the_backend_is_told_to_allow_it(
        self, make_template
    ):
        manager = DjangoTemplateManager(allow_deleting_published_versions=True)
        make_template(key="welcome", version=1)
        _move(manager, "welcome", 1, [ACTIVE])

        manager.delete_template("welcome", 1)

        assert not ManagedTemplate.objects.filter(key="welcome").exists()

    def test_the_option_is_off_by_default(self, manager):
        assert manager.allow_deleting_published_versions is False


class TestHistorySurvivesDeletion:
    def test_history_survives_a_permitted_hard_delete_of_a_published_version(
        self, make_template, editor
    ):
        manager = DjangoTemplateManager(allow_deleting_published_versions=True)
        make_template(key="welcome", version=1)
        _move(manager, "welcome", 1, [ACTIVE], changed_by=str(editor.pk))

        manager.delete_template("welcome", 1)

        history = manager.get_template_status_history("welcome", 1)
        assert [(e.template_key, e.version, e.status, e.created_by) for e in history] == [
            ("welcome", 1, ACTIVE, str(editor.pk))
        ]
        record = ManagedTemplateStatusRecord.objects.get()
        assert record.template is None

    def test_history_of_a_deleted_draft_stays_readable_across_the_key(self, manager, make_template):
        make_template(key="welcome", version=1)
        make_template(key="welcome", version=2)
        _move(manager, "welcome", 1, [ACTIVE])
        _move(manager, "welcome", 2, [DRAFT])

        manager.delete_template("welcome", 2)

        assert [(e.version, e.status) for e in manager.get_template_status_history("welcome")] == [
            (2, DRAFT),
            (1, ACTIVE),
        ]

    def test_a_key_whose_versions_are_all_gone_still_has_its_history(self, make_template):
        manager = DjangoTemplateManager(allow_deleting_published_versions=True)
        make_template(key="welcome", version=1)
        _move(manager, "welcome", 1, [ACTIVE])

        manager.delete_template("welcome", 1)

        assert [e.status for e in manager.get_template_status_history("welcome")] == [ACTIVE]
        with pytest.raises(ManagedTemplateNotFoundError):
            manager.get_template("welcome")

    def test_a_key_with_neither_versions_nor_history_is_still_not_found(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError):
            manager.get_template_status_history("nowhere")
        with pytest.raises(ManagedTemplateNotFoundError):
            manager.get_template_status_history("nowhere", 1)

    def test_deleting_the_row_directly_keeps_the_history_too(self, manager, make_template):
        """The admin, a shell or a data migration delete through the ORM, not the manager."""
        template = make_template(key="welcome", version=1)
        _move(manager, "welcome", 1, [ACTIVE])

        template.delete()

        assert ManagedTemplateStatusRecord.objects.count() == 1
        assert [e.status for e in manager.get_template_status_history("welcome", 1)] == [ACTIVE]

    def test_a_record_written_with_only_a_template_learns_its_key_and_version(self, make_template):
        template = make_template(key="welcome", version=3)

        record = ManagedTemplateStatusRecord.objects.create(template=template, status=ACTIVE.value)

        record.refresh_from_db()
        assert (record.template_key, record.version) == ("welcome", 3)
        assert str(record) == "welcome v3: active"


# ----------------------------------------------------------------------
# Through the service
# ----------------------------------------------------------------------


class TestThroughTheService:
    @pytest.fixture
    def service(self, manager):
        return ManagedTemplateService(
            manager, ManagedTemplateEmailRenderer(manager, SourceRenderer())
        )

    def test_the_service_refuses_a_published_version(self, service, make_template):
        make_template(key="welcome", version=1)
        service.activate("welcome", 1)

        with pytest.raises(ManagedTemplateDeletionNotAllowedError):
            service.delete_template("welcome")

    def test_the_service_deletes_a_never_published_draft(self, service, make_template):
        make_template(key="welcome", version=1)
        service.activate("welcome", 1)
        make_template(key="welcome", version=2)

        service.delete_template("welcome")

        assert list(ManagedTemplate.objects.values_list("version", flat=True)) == [1]

    def test_the_service_reads_the_active_template_natively(self, service, make_template):
        make_template(key="welcome", version=1, status=ACTIVE.value)
        make_template(key="welcome", version=2, status=DRAFT.value)

        assert service.get_active_template("welcome").version == 1
        assert service.get_template("welcome").version == 2


# ----------------------------------------------------------------------
# Version numbers are never reused
# ----------------------------------------------------------------------


def _create_input(key="welcome"):
    from vintasend_managed_templates.dataclasses import ManagedTemplateCreateInput

    return ManagedTemplateCreateInput(
        name="Welcome",
        description="",
        key=key,
        template_managed_backend="django",
        template_body="Hi",
        template_subject=None,
        template_preheader=None,
        tenant=None,
    )


def _update_input():
    from vintasend_managed_templates.dataclasses import ManagedTemplateUpdateInput

    return ManagedTemplateUpdateInput(
        name=None,
        description=None,
        template_body="next",
        template_subject=None,
        template_preheader=None,
    )


class TestVersionNumbersAreNeverReused:
    def test_a_recreated_key_does_not_inherit_a_deleted_versions_history(self, manager, db):
        manager.create_template(_create_input())
        _move(manager, "welcome", 1, [DRAFT], changed_by=None)
        manager.delete_template("welcome", 1)

        recreated = manager.create_template(_create_input())

        assert recreated.version == 2
        assert manager.get_template_status_history("welcome", 2) == []
        assert [e.version for e in manager.get_template_status_history("welcome")] == [1]

    def test_a_new_version_skips_the_number_of_a_deleted_published_one(self, db):
        manager = DjangoTemplateManager(allow_deleting_published_versions=True)
        manager.create_template(_create_input())
        manager.update_template("welcome", _update_input())
        manager.update_template("welcome", _update_input())
        _move(manager, "welcome", 3, [ACTIVE, ARCHIVED])
        manager.delete_template("welcome", 3)

        drafted = manager.update_template("welcome", _update_input())

        assert drafted.version == 4
        assert manager.get_template_status_history("welcome", 4) == []
        strict = DjangoTemplateManager()
        strict.delete_template("welcome", 4)  # a fresh draft, so the rule allows it

    def test_creating_a_key_that_already_exists_is_refused(self, manager, db):
        manager.create_template(_create_input())

        with pytest.raises(IntegrityError, match="'welcome' already exists"):
            manager.create_template(_create_input())

        assert list(ManagedTemplate.objects.values_list("version", flat=True)) == [1]

    def test_a_key_whose_first_version_was_deleted_still_exists(self, manager, db):
        """Creating it again must not hand out v1 a second time -- it would inherit v1's history."""
        manager.create_template(_create_input())
        _move(manager, "welcome", 1, [DRAFT], changed_by=None)
        manager.update_template("welcome", _update_input())
        manager.delete_template("welcome", 1)

        with pytest.raises(IntegrityError, match="'welcome' already exists"):
            manager.create_template(_create_input())

        assert list(ManagedTemplate.objects.values_list("version", flat=True)) == [2]
        assert [e.version for e in manager.get_template_status_history("welcome")] == [1]

    def test_numbering_is_unchanged_when_nothing_was_deleted(self, manager, db):
        manager.create_template(_create_input())

        assert manager.update_template("welcome", _update_input()).version == 2
