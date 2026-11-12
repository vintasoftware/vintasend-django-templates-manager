"""The service, the renderer and this backend assembled the way an application assembles them.

Previews resolve a base named without ``version=`` to its newest version, drafts included; a
send resolves it to its newest ACTIVE version. Publishing can render the version first, and a
failing check must leave the rows exactly as they were: no status change and no
``ManagedTemplateStatusRecord``. These run over the ORM because "nothing was written" is only
worth asserting against a store that could have written something.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from vintasend.services.dataclasses import Notification
from vintasend.services.notification_template_renderers.base import BaseNotificationTemplateRenderer
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    TemplatedEmail,
)
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateCreateInput,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionReferenceError,
    ManagedTemplateRenderCheckError,
)
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import (
    ManagedTemplateService,
    RenderCheck,
)

from vintasend_django_templates_manager.models import ManagedTemplate, ManagedTemplateStatusRecord


class FormatRenderer(BaseNotificationTemplateRenderer):
    """Fills ``{name}`` placeholders from the context, raising ``KeyError`` for a missing one.

    Enough to prove the composed source reached the engine, and to make a render fail on
    demand by leaving a placeholder's value out of the context.
    """

    def render(self, notification, context):
        raise AssertionError("the managed renderer must not call render")

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        return TemplatedEmail(
            subject=(template_content.subject_template or "").format(**context),
            body=template_content.body_template.format(**context),
        )


def create_input(key: str, body: str) -> ManagedTemplateCreateInput:
    return ManagedTemplateCreateInput(
        name=key,
        description="",
        key=key,
        template_managed_backend="django",
        template_body=body,
        template_subject="Hello {name}",
        template_preheader=None,
        tenant=None,
    )


def update_input(body: str) -> ManagedTemplateUpdateInput:
    return ManagedTemplateUpdateInput(
        name=None,
        description=None,
        template_body=body,
        template_subject=None,
        template_preheader=None,
    )


def notification(key: str) -> Notification:
    return Notification(
        id=1,
        user_id=1,
        notification_type="EMAIL",
        title="Welcome",
        body_template=key,
        context_name="ctx",
        context_kwargs={},
        send_after=None,
        subject_template="",
        preheader_template="",
        status="PENDING_SEND",
    )


def rows() -> list[dict]:
    """Every template row and every status record, as stored."""
    return [
        *ManagedTemplate.objects.order_by("key", "version").values(),
        *ManagedTemplateStatusRecord.objects.order_by("pk").values(),
    ]


def writes(queries: CaptureQueriesContext) -> list[str]:
    return [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


@pytest.fixture
def renderer(manager) -> ManagedTemplateEmailRenderer:
    return ManagedTemplateEmailRenderer(manager, FormatRenderer())


@pytest.fixture
def service(manager, renderer) -> ManagedTemplateService:
    return ManagedTemplateService(manager, renderer)


@pytest.fixture
def draft_base(db, service):
    """``welcome`` v1 (DRAFT) extending ``base``, which has never been published."""
    service.create_template(create_input("base", "[{% managed_children %}]"))
    service.create_template(create_input("welcome", '{% managed_extends "base" %}Hi {name}'))


class TestPreviewsAndSends:
    def test_previews_draft_parts_and_sends_published_ones_with_no_composer_configured(
        self, db, service
    ):
        service.create_template(create_input("base", "v1[{% managed_children %}]"))
        service.activate("base")
        service.update_template("base", update_input("v2[{% managed_children %}]"))
        service.create_template(create_input("welcome", '{% managed_extends "base" %}Hi {name}'))
        service.activate("welcome")
        welcome = service.get_template("welcome")

        preview = service.render_standalone(welcome, {"name": "Ana"}, parts="latest")
        as_sent = service.render_standalone(welcome, {"name": "Ana"}, parts="published")
        sent = service.render(notification("welcome"), {"name": "Ana"})

        assert preview.body == "v2[Hi Ana]"
        assert as_sent.body == "v1[Hi Ana]"
        assert sent.body == "v1[Hi Ana]"
        assert sent.template_version == 1

    def test_a_send_whose_base_has_nothing_published_fails(self, draft_base, service, renderer):
        service.activate("welcome")

        with pytest.raises(ManagedTemplateCompositionReferenceError, match="no active version"):
            renderer.render(notification("welcome"), {"name": "Ana"})

    def test_the_service_composer_still_reads_the_draft_base(self, draft_base, service):
        assert service.get_composed_template("welcome").body_template == "[Hi {name}]"


class TestRenderCheck:
    def test_a_passing_check_activates_with_its_history_record(self, db, service, editor):
        service.create_template(create_input("welcome", "Hi {name}"))

        activated = service.activate(
            "welcome", 1, str(editor.pk), check_render=RenderCheck(context={"name": "Ana"})
        )

        assert activated.status is ManagedTemplateStatus.ACTIVE
        record = ManagedTemplateStatusRecord.objects.get(template_key="welcome", version=1)
        assert record.status == ManagedTemplateStatus.ACTIVE.value
        assert record.created_by == editor

    def test_set_status_to_active_takes_the_check_too(self, db, service):
        service.create_template(create_input("welcome", "Hi {name}"))

        activated = service.set_status(
            "welcome",
            ManagedTemplateStatus.ACTIVE,
            1,
            check_render=RenderCheck(context={"name": "Ana"}),
        )

        assert activated.status is ManagedTemplateStatus.ACTIVE

    def test_a_draft_base_fails_the_check_and_nothing_is_written(self, draft_base, service, editor):
        before = rows()

        with CaptureQueriesContext(connection) as queries:
            with pytest.raises(ManagedTemplateRenderCheckError) as raised:
                service.activate(
                    "welcome", 1, str(editor.pk), check_render=RenderCheck(context={"name": "A"})
                )

        assert isinstance(raised.value.__cause__, ManagedTemplateCompositionReferenceError)
        assert queries.captured_queries  # the reads were captured, so the next line means it
        assert writes(queries) == []
        assert rows() == before
        assert ManagedTemplateStatusRecord.objects.count() == 0
        assert service.get_template("welcome", 1).status is ManagedTemplateStatus.DRAFT

    def test_a_render_error_fails_the_check_and_nothing_is_written(self, db, service):
        service.create_template(create_input("welcome", "Hi {name}"))
        before = rows()

        with CaptureQueriesContext(connection) as queries:
            with pytest.raises(ManagedTemplateRenderCheckError) as raised:
                service.set_status(
                    "welcome",
                    ManagedTemplateStatus.ACTIVE,
                    1,
                    check_render=RenderCheck(context={"secret": "s3"}),
                )

        assert isinstance(raised.value.__cause__, KeyError)
        assert "s3" not in str(raised.value)
        assert (raised.value.template_key, raised.value.version) == ("welcome", 1)
        assert queries.captured_queries  # the reads were captured, so the next line means it
        assert writes(queries) == []
        assert rows() == before

    @pytest.mark.parametrize(
        "status",
        [
            ManagedTemplateStatus.DRAFT,
            ManagedTemplateStatus.INACTIVE,
            ManagedTemplateStatus.ARCHIVED,
        ],
    )
    def test_the_check_with_another_status_is_refused_before_any_query(self, db, service, status):
        service.create_template(create_input("welcome", "Hi {name}"))
        before = rows()

        with CaptureQueriesContext(connection) as queries:
            with pytest.raises(ValueError, match="check_render applies only when activating"):
                service.set_status("welcome", status, 1, check_render=RenderCheck(context={}))

        assert queries.captured_queries == []
        assert rows() == before

    def test_publishing_the_base_lets_the_check_pass(self, draft_base, service):
        service.activate("base")

        activated = service.activate("welcome", check_render=RenderCheck(context={"name": "A"}))

        assert activated.status is ManagedTemplateStatus.ACTIVE


class TestCurrentTemplateVersion:
    def test_reports_the_version_new_emails_get_skipping_drafts_and_bases(
        self, db, service, renderer
    ):
        service.create_template(create_input("base", "[{% managed_children %}]"))
        service.activate("base")
        service.create_template(create_input("welcome", "Hi {name}"))
        service.activate("welcome")
        service.update_template("welcome", update_input("Hi again {name}"))
        service.create_template(create_input("draft-only", "Hi {name}"))

        for key, expected in [("welcome", 1), ("base", None), ("draft-only", None), ("x", None)]:
            assert service.get_current_template_version(key) == expected
            # What vintasend pins with answers the same.
            assert renderer.get_latest_template_version(key) == expected
