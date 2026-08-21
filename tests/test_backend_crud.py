import datetime

import pytest
from freezegun import freeze_time
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate as ManagedTemplateDataclass,
)
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateCreateInput,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateChangeUserNotFoundError,
    ManagedTemplateNotFoundError,
)

from vintasend_django_templates_manager.models import ManagedTemplate, ManagedTemplateStatusRecord

from .conftest import keys


def test_backend_declares_its_name(manager):
    assert manager.template_backend_name == "django"


class TestSerialization:
    def test_every_field_is_carried_onto_the_dataclass(self, manager, make_template):
        template = make_template(key="welcome", version=2, tenant="acme")

        serialized = manager.get_template("welcome", 2)

        assert isinstance(serialized, ManagedTemplateDataclass)
        assert serialized.id == template.pk
        assert serialized.name == template.name
        assert serialized.description == template.description
        assert serialized.key == "welcome"
        assert serialized.template_managed_backend == "django"
        assert serialized.body_template == template.body_template
        assert serialized.subject_template == template.subject_template
        assert serialized.preheader_template == template.preheader_template
        assert serialized.version == 2
        assert serialized.tenant == "acme"
        assert serialized.created == template.created
        assert serialized.updated == template.updated

    def test_status_is_rehydrated_into_the_enum(self, manager, make_template):
        make_template(key="welcome", status=ManagedTemplateStatus.ACTIVE.value)
        assert manager.get_template("welcome", 1).status is ManagedTemplateStatus.ACTIVE

    def test_nullable_template_parts_survive_as_none(self, manager, make_template):
        make_template(key="welcome", subject_template=None, preheader_template=None)
        serialized = manager.get_template("welcome", 1)
        assert serialized.subject_template is None
        assert serialized.preheader_template is None


class TestGetTemplate:
    def test_returns_the_matching_version(self, manager, make_template):
        make_template(key="welcome", version=3)
        assert manager.get_template("welcome", 3).version == 3

    def test_raises_when_the_version_does_not_match(self, manager, make_template):
        make_template(key="welcome", version=1)
        with pytest.raises(ManagedTemplateNotFoundError, match="version 99 does not exist"):
            manager.get_template("welcome", 99)

    def test_omitting_the_version_returns_the_latest(self, manager, make_template):
        make_template(key="welcome", version=3)
        assert manager.get_template("welcome").version == 3

    def test_raises_for_an_unknown_key(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError, match="'nope' does not exist"):
            manager.get_template("nope")


class TestCreateTemplate:
    def test_persists_a_new_template(self, manager, db):
        created = manager.create_template(
            ManagedTemplateCreateInput(
                name="Welcome",
                description="Sent on signup",
                key="welcome",
                template_managed_backend="django",
                template_body="Hello",
                template_subject="Hi there",
                template_preheader="Glad you are here",
                tenant=None,
            )
        )
        assert created.key == "welcome"
        assert created.name == "Welcome"
        assert created.description == "Sent on signup"
        assert created.template_managed_backend == "django"
        assert ManagedTemplate.objects.filter(key="welcome").exists()

    def test_maps_the_input_names_onto_the_model_fields(self, manager, db):
        """The input calls them template_body/template_subject/template_preheader; the model
        calls them body_template/subject_template/preheader_template."""
        created = manager.create_template(
            ManagedTemplateCreateInput(
                name="Welcome",
                description="Sent on signup",
                key="welcome",
                template_managed_backend="django",
                template_body="Hello",
                template_subject="Hi there",
                template_preheader="Glad you are here",
                tenant=None,
            )
        )

        assert created.body_template == "Hello"
        assert created.subject_template == "Hi there"
        assert created.preheader_template == "Glad you are here"

    def test_starts_at_version_one_as_a_draft(self, manager, db):
        created = manager.create_template(
            ManagedTemplateCreateInput(
                name="Welcome",
                description="Sent on signup",
                key="welcome",
                template_managed_backend="django",
                template_body="Hello",
                template_subject=None,
                template_preheader=None,
                tenant=None,
            )
        )

        assert created.version == 1
        assert created.status is ManagedTemplateStatus.DRAFT

    def test_keeps_the_tenant(self, manager, db):
        created = manager.create_template(
            ManagedTemplateCreateInput(
                name="Welcome",
                description="Sent on signup",
                key="welcome",
                template_managed_backend="django",
                template_body="Hello",
                template_subject=None,
                template_preheader=None,
                tenant="acme",
            )
        )
        assert created.tenant == "acme"

    def test_a_created_template_can_be_read_back(self, manager, db):
        manager.create_template(
            ManagedTemplateCreateInput(
                name="Welcome",
                description="Sent on signup",
                key="welcome",
                template_managed_backend="django",
                template_body="Hello",
                template_subject=None,
                template_preheader=None,
                tenant=None,
            )
        )
        assert manager.get_template("welcome").body_template == "Hello"


class TestUpdateTemplate:
    def test_bumps_the_version_and_applies_the_changes(self, manager, make_template):
        make_template(key="welcome", version=1, name="Old", description="Old description")

        updated = manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name="New",
                description="New description",
                template_body="New body",
                template_subject="New subject",
                template_preheader="New preheader",
            ),
        )

        assert updated.version == 2
        assert updated.name == "New"
        assert updated.description == "New description"
        assert updated.body_template == "New body"
        assert updated.subject_template == "New subject"
        assert updated.preheader_template == "New preheader"

    def test_inserts_a_row_and_leaves_the_previous_version_untouched(self, manager, make_template):
        previous = make_template(key="welcome", version=1, name="Old")

        manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name="New",
                description=None,
                template_body=None,
                template_subject=None,
                template_preheader=None,
            ),
        )

        previous.refresh_from_db()
        assert (previous.name, previous.version) == ("Old", 1)
        rows = ManagedTemplate.objects.filter(key="welcome").order_by("version")
        assert [(row.name, row.version) for row in rows] == [("Old", 1), ("New", 2)]

    def test_both_versions_stay_readable_by_number(self, manager, make_template):
        """The reason versions are rows: a notification holding v1 must keep rendering v1."""
        make_template(key="welcome", version=1, body_template="v1 body")

        manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name=None,
                description=None,
                template_body="v2 body",
                template_subject=None,
                template_preheader=None,
            ),
        )

        assert manager.get_template("welcome", 1).body_template == "v1 body"
        assert manager.get_template("welcome", 2).body_template == "v2 body"
        assert manager.get_template("welcome").version == 2

    def test_none_fields_keep_their_current_values(self, manager, make_template):
        make_template(key="welcome", name="Kept", description="Kept too", body_template="Body")

        updated = manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name=None,
                description=None,
                template_body=None,
                template_subject=None,
                template_preheader=None,
            ),
        )

        assert updated.name == "Kept"
        assert updated.description == "Kept too"
        assert updated.body_template == "Body"

    def test_the_new_version_starts_as_a_draft_while_the_live_one_stays_active(
        self, manager, make_template
    ):
        """A copy nobody has reviewed does not inherit "published".

        This is what lets a live template be revised safely: v1 goes on serving until v2 is
        activated deliberately.
        """
        live = make_template(key="welcome", status=ManagedTemplateStatus.ACTIVE.value)

        updated = manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name="New",
                description=None,
                template_body=None,
                template_subject=None,
                template_preheader=None,
            ),
        )

        assert updated.status is ManagedTemplateStatus.DRAFT
        live.refresh_from_db()
        assert live.status == ManagedTemplateStatus.ACTIVE.value

    def test_the_new_version_carries_the_previous_tags_when_none_are_given(
        self, manager, make_template, make_tag
    ):
        previous = make_template(key="welcome", version=1)
        previous.tags.set([make_tag("Onboarding")])

        updated = manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name=None,
                description=None,
                template_body=None,
                template_subject=None,
                template_preheader=None,
                tags=None,
            ),
        )

        assert [tag.slug for tag in updated.tags] == ["onboarding"]
        # The predecessor keeps its own -- the new row's tags are copies of the links, not a
        # move of them.
        assert [tag.slug for tag in previous.tags.all()] == ["onboarding"]

    def test_the_new_version_can_be_created_with_no_tags(self, manager, make_template, make_tag):
        previous = make_template(key="welcome", version=1)
        previous.tags.set([make_tag("Onboarding")])

        updated = manager.update_template(
            "welcome",
            ManagedTemplateUpdateInput(
                name=None,
                description=None,
                template_body=None,
                template_subject=None,
                template_preheader=None,
                tags=[],
            ),
        )

        assert updated.tags == []
        assert [tag.slug for tag in previous.tags.all()] == ["onboarding"]

    def test_raises_for_an_unknown_key(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError, match="does not exist"):
            manager.update_template(
                "nope",
                ManagedTemplateUpdateInput(
                    name="New",
                    description=None,
                    template_body=None,
                    template_subject=None,
                    template_preheader=None,
                ),
            )


class TestDeleteTemplate:
    def test_deletes_the_pinned_version(self, manager, make_template):
        make_template(key="welcome", version=2)
        manager.delete_template("welcome", 2)
        assert not ManagedTemplate.objects.filter(key="welcome").exists()

    def test_deletes_the_latest_when_no_version_is_given(self, manager, make_template):
        make_template(key="welcome", version=2)
        manager.delete_template("welcome")
        assert not ManagedTemplate.objects.filter(key="welcome").exists()

    def test_raises_for_an_unknown_key(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError, match="'nope' does not exist"):
            manager.delete_template("nope")

    def test_raises_for_an_unknown_version(self, manager, make_template):
        make_template(key="welcome", version=1)
        with pytest.raises(ManagedTemplateNotFoundError, match="version 5 does not exist"):
            manager.delete_template("welcome", 5)


class TestStatusUpdates:
    def test_moves_the_template_and_appends_to_the_history(self, manager, make_template):
        template = make_template(key="welcome", version=1)

        manager.create_template_status_update("welcome", 1, ManagedTemplateStatus.ACTIVE)

        template.refresh_from_db()
        assert template.status == ManagedTemplateStatus.ACTIVE.value
        record = ManagedTemplateStatusRecord.objects.get()
        assert record.template == template
        assert record.status == ManagedTemplateStatus.ACTIVE.value
        assert record.created_by is None

    def test_attributes_the_change_to_the_given_user(self, manager, make_template, editor):
        make_template(key="welcome", version=1)
        manager.create_template_status_update(
            "welcome", 1, ManagedTemplateStatus.ACTIVE, changed_by=str(editor.pk)
        )
        assert ManagedTemplateStatusRecord.objects.get().created_by == editor

    def test_raises_for_an_unknown_template(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError, match="'nope'"):
            manager.create_template_status_update("nope", 1, ManagedTemplateStatus.ACTIVE)

    def test_raises_for_an_unknown_version(self, manager, make_template):
        make_template(key="welcome", version=1)
        with pytest.raises(ManagedTemplateNotFoundError, match="version '9'"):
            manager.create_template_status_update("welcome", 9, ManagedTemplateStatus.ACTIVE)

    def test_an_unknown_changed_by_user_is_rejected(self, manager, make_template):
        make_template(key="welcome", version=1)

        with pytest.raises(ManagedTemplateChangeUserNotFoundError, match="123456"):
            manager.create_template_status_update(
                "welcome", 1, ManagedTemplateStatus.ACTIVE, changed_by="123456"
            )

    def test_a_malformed_changed_by_id_is_rejected(self, manager, make_template):
        """A pk the column cannot represent used to escape as a raw ValueError."""
        make_template(key="welcome", version=1)

        with pytest.raises(ManagedTemplateChangeUserNotFoundError, match="not-an-id"):
            manager.create_template_status_update(
                "welcome", 1, ManagedTemplateStatus.ACTIVE, changed_by="not-an-id"
            )

    def test_a_rejected_author_leaves_no_status_change(self, manager, make_template):
        template = make_template(key="welcome", version=1)

        with pytest.raises(ManagedTemplateChangeUserNotFoundError):
            manager.create_template_status_update(
                "welcome", 1, ManagedTemplateStatus.ACTIVE, changed_by="123456"
            )

        template.refresh_from_db()
        assert template.status == ManagedTemplateStatus.DRAFT.value
        assert ManagedTemplateStatusRecord.objects.count() == 0


class TestStatusHistory:
    def test_returns_entries_for_the_pinned_version(self, manager, make_template):
        make_template(key="welcome", version=1)
        manager.create_template_status_update("welcome", 1, ManagedTemplateStatus.ACTIVE)

        history = manager.get_template_status_history("welcome", 1)

        assert len(history) == 1
        entry = history[0]
        assert entry.template_key == "welcome"
        assert entry.version == 1
        assert entry.status is ManagedTemplateStatus.ACTIVE
        assert entry.created_by is None
        assert entry.tenant is None

    def test_records_the_author_as_a_string_id(self, manager, make_template, editor):
        make_template(key="welcome", version=1)
        manager.create_template_status_update(
            "welcome", 1, ManagedTemplateStatus.ACTIVE, changed_by=str(editor.pk)
        )
        assert manager.get_template_status_history("welcome", 1)[0].created_by == str(editor.pk)

    def test_pinned_version_returns_newest_first(self, manager, make_template):
        template = make_template(key="welcome", version=1)
        for moment, status in [
            ("2024-01-01 10:00:00", ManagedTemplateStatus.DRAFT),
            ("2024-01-02 10:00:00", ManagedTemplateStatus.ACTIVE),
            ("2024-01-03 10:00:00", ManagedTemplateStatus.ARCHIVED),
        ]:
            with freeze_time(moment):
                ManagedTemplateStatusRecord.objects.create(template=template, status=status.value)

        history = manager.get_template_status_history("welcome", 1)

        assert [entry.status for entry in history] == [
            ManagedTemplateStatus.ARCHIVED,
            ManagedTemplateStatus.ACTIVE,
            ManagedTemplateStatus.DRAFT,
        ]
        assert history[0].created == datetime.datetime(2024, 1, 3, 10, tzinfo=datetime.timezone.utc)

    def test_falls_back_to_the_latest_version(self, manager, make_template):
        make_template(key="welcome", version=1)
        manager.create_template_status_update("welcome", 1, ManagedTemplateStatus.ACTIVE)

        history = manager.get_template_status_history("welcome")

        assert [entry.status for entry in history] == [ManagedTemplateStatus.ACTIVE]

    def test_omitting_the_version_reads_the_whole_keys_trail(self, manager, make_template):
        """Every version keeps its own records, and the key's history is all of them.

        Reading only the latest version's would hide what happened to the versions still
        rendering for notifications sent before the newest one existed.
        """
        first = make_template(key="welcome", version=1)
        second = make_template(key="welcome", version=2)
        with freeze_time("2024-01-01 10:00:00"):
            ManagedTemplateStatusRecord.objects.create(
                template=first, status=ManagedTemplateStatus.ACTIVE.value
            )
        with freeze_time("2024-01-02 10:00:00"):
            ManagedTemplateStatusRecord.objects.create(
                template=second, status=ManagedTemplateStatus.DRAFT.value
            )

        history = manager.get_template_status_history("welcome")

        assert [(entry.version, entry.status) for entry in history] == [
            (2, ManagedTemplateStatus.DRAFT),
            (1, ManagedTemplateStatus.ACTIVE),
        ]

    def test_a_pinned_version_reads_only_its_own_records(self, manager, make_template):
        first = make_template(key="welcome", version=1)
        second = make_template(key="welcome", version=2)
        ManagedTemplateStatusRecord.objects.create(
            template=first, status=ManagedTemplateStatus.ACTIVE.value
        )
        ManagedTemplateStatusRecord.objects.create(
            template=second, status=ManagedTemplateStatus.DRAFT.value
        )

        history = manager.get_template_status_history("welcome", 1)

        assert [(entry.version, entry.status) for entry in history] == [
            (1, ManagedTemplateStatus.ACTIVE)
        ]

    def test_is_empty_before_any_status_change(self, manager, make_template):
        make_template(key="welcome", version=1)
        assert manager.get_template_status_history("welcome", 1) == []

    def test_carries_the_tenant(self, manager, make_template):
        template = make_template(key="welcome", version=1, tenant="acme")
        ManagedTemplateStatusRecord.objects.create(
            template=template, status=ManagedTemplateStatus.ACTIVE.value, tenant="acme"
        )
        assert manager.get_template_status_history("welcome", 1)[0].tenant == "acme"

    def test_raises_for_an_unknown_key(self, manager, db):
        with pytest.raises(ManagedTemplateNotFoundError, match="'nope' does not exist"):
            manager.get_template_status_history("nope")

    def test_raises_for_an_unknown_version(self, manager, make_template):
        make_template(key="welcome", version=1)
        with pytest.raises(ManagedTemplateNotFoundError, match="version '9'"):
            manager.get_template_status_history("welcome", 9)


class TestListing:
    def test_get_all_templates_returns_every_row(self, manager, templates):
        assert sorted(keys(manager.get_all_templates())) == ["alpha", "beta", "gamma"]

    def test_get_all_templates_is_empty_without_rows(self, manager, db):
        assert list(manager.get_all_templates()) == []

    def test_get_templates_by_status_matches_one_status(self, manager, templates):
        results = manager.get_templates_by_status([ManagedTemplateStatus.ACTIVE])
        assert keys(results) == ["beta"]

    def test_get_templates_by_status_matches_several(self, manager, templates):
        results = manager.get_templates_by_status(
            [ManagedTemplateStatus.ACTIVE, ManagedTemplateStatus.ARCHIVED]
        )
        assert sorted(keys(results)) == ["beta", "gamma"]

    def test_get_templates_by_status_is_empty_when_nothing_matches(self, manager, templates):
        assert list(manager.get_templates_by_status([ManagedTemplateStatus.INACTIVE])) == []


class TestPagination:
    def test_slices_the_requested_page(self, manager, templates):
        assert len(keys(manager.get_paginated_templates(page=1, page_size=2))) == 2
        assert len(keys(manager.get_paginated_templates(page=2, page_size=2))) == 1

    def test_pages_do_not_overlap(self, manager, templates):
        first = keys(manager.get_paginated_templates(page=1, page_size=2))
        second = keys(manager.get_paginated_templates(page=2, page_size=2))
        assert set(first).isdisjoint(second)
        assert sorted(first + second) == ["alpha", "beta", "gamma"]

    def test_a_page_past_the_end_is_empty(self, manager, templates):
        assert keys(manager.get_paginated_templates(page=9, page_size=2)) == []

    def test_a_single_page_holds_everything(self, manager, templates):
        assert len(keys(manager.get_paginated_templates(page=1, page_size=50))) == 3
