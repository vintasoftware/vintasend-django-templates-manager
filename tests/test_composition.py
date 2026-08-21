"""Composition against real rows: the backend resolving bases, and the admin checking them.

The tag language itself is vintasend-managed-templates' to test. What matters here is that
``DjangoTemplateManager`` is a resolver the composer can actually work through -- versions
included -- and that the admin refuses to save a template that would fail at send time.
"""

from django.contrib.admin.sites import site

import pytest
from vintasend_managed_templates.composition import TemplateComposer
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import ManagedTemplateUpdateInput
from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionCycleError,
    ManagedTemplateCompositionReferenceError,
    ManagedTemplateCompositionSyntaxError,
)

from vintasend_django_templates_manager.admin import ManagedTemplateAdmin, ManagedTemplateAdminForm
from vintasend_django_templates_manager.composition import template_is_abstract
from vintasend_django_templates_manager.models import ManagedTemplate


BASE_BODY = "<html><body>{% managed_block header %}Acme{% managed_endblock %}"
BASE_BODY += "{% managed_children %}</body></html>"


@pytest.fixture
def composer(manager) -> TemplateComposer:
    return TemplateComposer.from_backend(manager)


@pytest.fixture
def template_admin():
    return ManagedTemplateAdmin(ManagedTemplate, site)


def make_update_input(**overrides) -> ManagedTemplateUpdateInput:
    fields = {
        "name": None,
        "description": None,
        "template_body": None,
        "template_subject": None,
        "template_preheader": None,
    }
    return ManagedTemplateUpdateInput(**{**fields, **overrides})


def form_data(**overrides):
    data = {
        "name": "Welcome email",
        "key": "welcome",
        "version": 1,
        "template_managed_backend": "django",
        "description": "Sent on signup",
        "subject_template": "Welcome aboard",
        "preheader_template": "Glad you are here",
        "body_template": "Hello {{ name }}",
        "status": ManagedTemplateStatus.DRAFT.value,
        "tenant": "",
    }
    return {**data, **overrides}


# ----------------------------------------------------------------------
# Composing through the backend
# ----------------------------------------------------------------------


def test_a_stored_template_composes_against_a_stored_base(manager, composer, make_template):
    make_template(key="base-email", body_template=BASE_BODY)
    make_template(
        key="welcome",
        body_template='{% managed_extends "base-email" %}<p>Hi</p>',
    )

    composed = composer.compose(manager.get_template("welcome"))

    assert composed.body_template == "<html><body>Acme<p>Hi</p></body></html>"


def test_a_child_overrides_a_block_of_its_stored_base(manager, composer, make_template):
    make_template(key="base-email", body_template=BASE_BODY)
    make_template(
        key="welcome",
        body_template=(
            '{% managed_extends "base-email" %}'
            "{% managed_block header %}Welcome{% managed_endblock %}<p>Hi</p>"
        ),
    )

    composed = composer.compose(manager.get_template("welcome"))

    assert composed.body_template == "<html><body>Welcome<p>Hi</p></body></html>"


def test_an_include_pulls_in_another_row(manager, composer, make_template):
    make_template(key="footer", body_template="<footer>bye</footer>")
    make_template(key="page", body_template='body{% managed_include "footer" %}')

    composed = composer.compose(manager.get_template("page"))

    assert composed.body_template == "body<footer>bye</footer>"


def test_an_unpinned_base_composes_against_its_latest_version(manager, composer, make_template):
    make_template(key="base-email", body_template="v1:{% managed_children %}", version=1)
    make_template(key="base-email", body_template="v2:{% managed_children %}", version=2)
    make_template(key="welcome", body_template='{% managed_extends "base-email" %}Hi')

    assert composer.compose(manager.get_template("welcome")).body_template == "v2:Hi"


def test_a_pinned_base_composes_against_that_version(manager, composer, make_template):
    make_template(key="base-email", body_template="v1:{% managed_children %}", version=1)
    make_template(key="base-email", body_template="v2:{% managed_children %}", version=2)
    make_template(key="welcome", body_template='{% managed_extends "base-email" version=1 %}Hi')

    assert composer.compose(manager.get_template("welcome")).body_template == "v1:Hi"


def test_a_base_that_was_never_created_is_reported_as_missing(manager, composer, make_template):
    make_template(key="welcome", body_template='{% managed_extends "nowhere" %}Hi')

    with pytest.raises(ManagedTemplateCompositionReferenceError, match="nowhere"):
        composer.compose(manager.get_template("welcome"))


def test_rows_that_extend_each_other_are_a_cycle(manager, composer, make_template):
    make_template(key="first", body_template='{% managed_extends "second" %}')
    make_template(key="second", body_template='{% managed_extends "first" %}')

    with pytest.raises(ManagedTemplateCompositionCycleError):
        composer.compose(manager.get_template("first"))


def test_every_field_composes_against_the_same_field_of_the_base(manager, composer, make_template):
    make_template(
        key="base-email",
        body_template=BASE_BODY,
        subject_template="[Acme] {% managed_children %}",
        preheader_template="pre: {% managed_children %}",
    )
    make_template(
        key="welcome",
        body_template='{% managed_extends "base-email" %}<p>Hi</p>',
        subject_template='{% managed_extends "base-email" %}Welcome',
        preheader_template='{% managed_extends "base-email" %}now',
    )

    composed = composer.compose(manager.get_template("welcome"))

    assert composed.subject_template == "[Acme] Welcome"
    assert composed.preheader_template == "pre: now"


# ----------------------------------------------------------------------
# The admin refuses what will not compose
# ----------------------------------------------------------------------


def test_a_sound_template_saves(db, make_template):
    make_template(key="base-email", body_template=BASE_BODY)
    form = ManagedTemplateAdminForm(
        data=form_data(body_template='{% managed_extends "base-email" %}<p>Hi</p>')
    )

    assert form.is_valid(), form.errors


def test_a_template_with_no_composition_in_it_saves(db):
    assert ManagedTemplateAdminForm(data=form_data()).is_valid()


def test_extending_a_base_that_does_not_exist_is_a_form_error(db):
    form = ManagedTemplateAdminForm(
        data=form_data(body_template='{% managed_extends "nowhere" %}Hi')
    )

    assert not form.is_valid()
    assert "nowhere" in form.errors["body_template"][0]


def test_a_malformed_tag_is_a_form_error(db):
    form = ManagedTemplateAdminForm(data=form_data(body_template="{% managed_block a %}Hi"))

    assert not form.is_valid()
    assert "never closed" in form.errors["body_template"][0]


def test_the_error_lands_on_the_field_that_carries_it(db):
    form = ManagedTemplateAdminForm(
        data=form_data(subject_template='{% managed_extends "nowhere" %}Hi')
    )

    assert not form.is_valid()
    assert "subject_template" in form.errors
    assert "body_template" not in form.errors


def test_a_base_that_leads_back_to_the_row_being_edited_is_a_cycle(db, make_template):
    make_template(key="welcome", body_template="the stored, now stale, body")
    make_template(key="base-email", body_template='{% managed_extends "welcome" %}')
    template = ManagedTemplate.objects.get(key="welcome")
    form = ManagedTemplateAdminForm(
        data=form_data(body_template='{% managed_extends "base-email" %}new'),
        instance=template,
    )

    assert not form.is_valid()
    assert "loops" in form.errors["body_template"][0]


def test_the_check_can_be_turned_off(db):
    class Unchecked(ManagedTemplateAdminForm):
        validate_composition = False

    assert Unchecked(data=form_data(body_template='{% managed_extends "nowhere" %}Hi')).is_valid()


def test_a_form_with_no_body_at_all_is_left_to_the_field_validators(db):
    form = ManagedTemplateAdminForm(data=form_data(body_template=""))

    assert not form.is_valid()
    assert "body_template" in form.errors


# ----------------------------------------------------------------------
# What the admin shows
# ----------------------------------------------------------------------


def test_the_change_form_previews_what_the_engine_will_receive(template_admin, make_template):
    make_template(key="base-email", body_template=BASE_BODY)
    template = make_template(
        key="welcome", body_template='{% managed_extends "base-email" %}<p>Hi</p>'
    )

    preview = template_admin.composed_body(template)

    assert "&lt;html&gt;&lt;body&gt;Acme&lt;p&gt;Hi&lt;/p&gt;" in preview


def test_the_preview_reports_a_broken_template_instead_of_raising(template_admin, make_template):
    template = make_template(key="welcome", body_template='{% managed_extends "nowhere" %}Hi')

    preview = template_admin.composed_body(template)

    assert "errorlist" in preview
    assert "nowhere" in preview


def test_there_is_nothing_to_preview_before_a_template_is_saved(template_admin):
    assert "Save the template" in str(template_admin.composed_body(None))


def test_the_changelist_marks_which_templates_are_bases(template_admin, make_template):
    base = make_template(key="base-email", body_template=BASE_BODY)
    welcome = make_template(
        key="welcome", body_template='{% managed_extends "base-email" %}<p>Hi</p>'
    )

    assert base.is_abstract is True
    assert welcome.is_abstract is False
    assert "is_abstract" in template_admin.list_display
    assert "is_abstract" in template_admin.list_filter


# ----------------------------------------------------------------------
# The denormalized flag
# ----------------------------------------------------------------------


def test_saving_derives_the_flag_from_the_source(make_template):
    assert make_template(key="base", body_template="{% managed_children %}").is_abstract is True
    assert make_template(key="plain", body_template="<p>hi</p>").is_abstract is False


def test_the_flag_is_derived_from_any_of_the_three_sources(make_template):
    template = make_template(
        key="base-subject", body_template="plain", subject_template="[{% managed_children %}]"
    )

    assert template.is_abstract is True


def test_editing_a_template_brings_the_flag_with_it(make_template):
    template = make_template(key="base", body_template="{% managed_children %}")

    template.body_template = "<p>no hole any more</p>"
    template.save()
    template.refresh_from_db()

    assert template.is_abstract is False


def test_a_narrowed_save_that_touches_a_source_still_updates_the_flag(make_template):
    template = make_template(key="plain", body_template="<p>hi</p>")

    template.body_template = "{% managed_children %}"
    template.save(update_fields=["body_template"])
    template.refresh_from_db()

    assert template.is_abstract is True


def test_a_narrowed_save_that_touches_no_source_leaves_the_flag_alone(make_template):
    template = make_template(key="base", body_template="{% managed_children %}")

    template.status = ManagedTemplateStatus.ACTIVE.value
    template.save(update_fields=["status"])
    template.refresh_from_db()

    assert template.is_abstract is True


def test_a_template_nobody_can_parse_saves_as_concrete(make_template):
    assert make_template(key="broken", body_template="{% managed_block a %}Hi").is_abstract is False


def test_the_backend_serializes_the_stored_flag(manager, make_template):
    make_template(key="base", body_template="{% managed_children %}")

    assert manager.get_template("base").is_abstract is True


def test_a_new_version_re_derives_the_flag(manager, make_template):
    make_template(key="base", body_template="{% managed_children %}")

    new_version = manager.update_template("base", make_update_input(template_body="<p>hi</p>"))

    assert new_version.is_abstract is False


def test_the_filter_finds_the_bases(manager, make_template):
    make_template(key="base", body_template="{% managed_children %}")
    make_template(key="welcome", body_template="<p>hi</p>")

    abstract = manager.get_filtered_templates({"is_abstract": True})
    sendable = manager.get_filtered_templates({"is_abstract": False})

    assert [template.key for template in abstract] == ["base"]
    assert [template.key for template in sendable] == ["welcome"]


def test_the_filter_can_be_negated(manager, make_template):
    make_template(key="base", body_template="{% managed_children %}")
    make_template(key="welcome", body_template="<p>hi</p>")

    results = manager.get_filtered_templates({"not": {"is_abstract": True}})

    assert [template.key for template in results] == ["welcome"]


def test_the_queryset_splits_bases_from_sendable_templates(make_template):
    make_template(key="base", body_template="{% managed_children %}")
    make_template(key="welcome", body_template="<p>hi</p>")

    assert [t.key for t in ManagedTemplate.objects.abstract()] == ["base"]
    assert [t.key for t in ManagedTemplate.objects.sendable()] == ["welcome"]


# ----------------------------------------------------------------------
# The check, for when the flag cannot be trusted
# ----------------------------------------------------------------------


def test_the_check_recomputes_rather_than_reading_the_flag(make_template):
    template = make_template(key="welcome", body_template="<p>hi</p>")
    template.body_template = "{% managed_children %}"

    assert template.is_abstract is False
    assert template_is_abstract(template) is True


def test_the_check_reads_any_of_the_three_sources(make_template):
    template = make_template(
        key="base-subject", body_template="plain", subject_template="[{% managed_children %}]"
    )

    assert template_is_abstract(template)


def test_the_check_reports_a_malformed_tag(make_template):
    template = make_template(key="broken", body_template="{% managed_endblock %}")

    with pytest.raises(ManagedTemplateCompositionSyntaxError):
        template_is_abstract(template)


def test_the_check_can_be_told_to_swallow_one_instead(make_template):
    template = make_template(key="broken", body_template="{% managed_endblock %}")

    assert template_is_abstract(template, strict=False) is False
