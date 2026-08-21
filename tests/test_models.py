from django.core.management import call_command
from django.db.utils import IntegrityError

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus, ManagedTemplateTagStatus

from vintasend_django_templates_manager.contants import (
    ManagedTemplateStatusChoices,
    ManagedTemplateTagStatusChoices,
)
from vintasend_django_templates_manager.models import (
    ManagedTemplateStatusRecord,
    ManagedTemplateTag,
)


def test_system_checks_pass():
    """Guards the ``related_name`` split between the two ``created_by`` FKs: when they collide
    Django reports fields.E304/E305 and every admin page 500s."""
    call_command("check")


def test_makemigrations_has_no_pending_changes(db):
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def test_template_str_is_the_name(make_template):
    assert str(make_template(name="Password reset")) == "Password reset"


def test_status_record_str_identifies_template_version_and_status(make_template):
    template = make_template(key="welcome", version=4)
    record = ManagedTemplateStatusRecord.objects.create(
        template=template, status=ManagedTemplateStatus.ACTIVE.value
    )
    assert str(record) == "welcome v4: active"


def test_status_choices_mirror_the_shared_enum():
    assert [c.value for c in ManagedTemplateStatusChoices] == [
        status.value for status in ManagedTemplateStatus
    ]


def test_created_by_reverse_accessors_are_distinct(make_template, editor):
    template = make_template(created_by=editor)
    record = ManagedTemplateStatusRecord.objects.create(
        template=template, status=ManagedTemplateStatus.ACTIVE.value, created_by=editor
    )
    assert list(editor.created_templates.all()) == [template]
    assert list(editor.modified_template_status.all()) == [record]


def test_history_is_the_reverse_accessor_for_status_records(make_template):
    template = make_template()
    record = ManagedTemplateStatusRecord.objects.create(
        template=template, status=ManagedTemplateStatus.ACTIVE.value
    )
    assert list(template.history.all()) == [record]


def test_deleting_a_template_cascades_to_its_history(make_template):
    template = make_template()
    ManagedTemplateStatusRecord.objects.create(
        template=template, status=ManagedTemplateStatus.ACTIVE.value
    )
    template.delete()
    assert ManagedTemplateStatusRecord.objects.count() == 0


def test_key_is_unique_so_versions_cannot_coexist(make_template):
    """``key`` is ``unique=True`` while the model also carries a ``version`` column, so a key
    only ever has one row. ``update_template`` bumps that row in place rather than appending a
    new version, and ``get_latest_version`` can never have more than one row to choose from."""
    make_template(key="welcome", version=1)
    with pytest.raises(IntegrityError):
        make_template(key="welcome", version=2)


def test_tag_str_is_the_text(make_tag):
    assert str(make_tag("Black Friday")) == "Black Friday"


def test_tag_status_choices_mirror_the_shared_enum():
    assert [c.value for c in ManagedTemplateTagStatusChoices] == [
        status.value for status in ManagedTemplateTagStatus
    ]


def test_a_new_tag_defaults_to_active(db):
    tag = ManagedTemplateTag.objects.create(text="Onboarding", slug="onboarding")

    assert tag.status == ManagedTemplateTagStatus.ACTIVE.value


def test_tags_and_templates_are_many_to_many(make_template, make_tag):
    onboarding = make_tag("Onboarding")
    billing = make_tag("Billing")
    welcome = make_template(key="welcome")
    receipt = make_template(key="receipt")

    welcome.tags.set([onboarding, billing])
    receipt.tags.set([onboarding])

    assert sorted(t.slug for t in welcome.tags.all()) == ["billing", "onboarding"]
    assert sorted(t.key for t in onboarding.templates.all()) == ["receipt", "welcome"]


def test_deleting_a_template_leaves_its_tags_standing(make_template, make_tag):
    tag = make_tag("Onboarding")
    template = make_template()
    template.tags.set([tag])

    template.delete()

    assert ManagedTemplateTag.objects.filter(pk=tag.pk).exists()


def test_deleting_a_tag_leaves_its_templates_standing(make_template, make_tag):
    tag = make_tag("Onboarding")
    template = make_template()
    template.tags.set([tag])

    tag.delete()

    template.refresh_from_db()
    assert list(template.tags.all()) == []


def test_created_by_reverse_accessor_for_tags_is_distinct(make_tag, editor):
    tag = make_tag("Onboarding", created_by=editor)

    assert list(editor.created_template_tags.all()) == [tag]
