from django.contrib.admin.sites import site
from django.urls import reverse

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus, ManagedTemplateTagStatus

from vintasend_django_templates_manager.admin import (
    ManagedTemplateAdmin,
    ManagedTemplateAdminForm,
    ManagedTemplateStatusRecordInline,
    ManagedTemplateTagAdmin,
    ManagedTemplateTagAdminForm,
    _acting_user,
)
from vintasend_django_templates_manager.models import (
    ManagedTemplate,
    ManagedTemplateStatusRecord,
    ManagedTemplateTag,
)


@pytest.fixture
def template_admin():
    return ManagedTemplateAdmin(ManagedTemplate, site)


@pytest.fixture
def history_admin():
    return site._registry[ManagedTemplateStatusRecord]


@pytest.fixture
def request_from(rf, admin_user):
    def _build():
        request = rf.post("/")
        request.user = admin_user
        return request

    return _build


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


class TestRegistration:
    def test_both_models_are_registered(self):
        assert ManagedTemplate in site._registry
        assert ManagedTemplateStatusRecord in site._registry

    def test_history_is_inlined_on_the_template_page(self, template_admin):
        assert ManagedTemplateStatusRecordInline in template_admin.inlines


class TestActingUser:
    def test_returns_the_logged_in_user(self, rf, admin_user):
        request = rf.get("/")
        request.user = admin_user
        assert _acting_user(request) == admin_user

    def test_returns_none_when_anonymous(self, rf):
        from django.contrib.auth.models import AnonymousUser

        request = rf.get("/")
        request.user = AnonymousUser()
        assert _acting_user(request) is None


class TestStatusField:
    def test_renders_as_a_select_over_the_shared_enum(self, template_admin, request_from):
        field = template_admin.get_form(request_from()).base_fields["status"]
        assert [choice[0] for choice in field.choices] == [
            status.value for status in ManagedTemplateStatus
        ]

    def test_rejects_a_status_outside_the_enum(self, template_admin, request_from, db):
        form = template_admin.get_form(request_from())(data=form_data(status="bogus"))
        assert not form.is_valid()
        assert "status" in form.errors

    def test_the_add_form_starts_as_a_draft_at_version_one(self, template_admin, rf, admin_user):
        request = rf.get("/")
        request.user = admin_user
        initial = template_admin.get_changeform_initial_data(request)
        assert initial["version"] == "1"
        assert initial["status"] == ManagedTemplateStatus.DRAFT.value


class TestIdentityFieldsAreLocked:
    def test_key_and_version_are_editable_on_add(self, template_admin, request_from):
        assert set(template_admin.get_readonly_fields(request_from())) == {"created", "updated"}
        base_fields = template_admin.get_form(request_from()).base_fields
        assert "key" in base_fields
        assert "version" in base_fields

    def test_key_and_version_are_locked_on_change(
        self, template_admin, request_from, make_template
    ):
        template = make_template()
        readonly = set(template_admin.get_readonly_fields(request_from(), template))
        assert readonly == {"created", "updated", "key", "version"}
        base_fields = template_admin.get_form(request_from(), template).base_fields
        assert "key" not in base_fields
        assert "version" not in base_fields


class TestSaveModelWritesHistory:
    def test_creating_records_the_initial_status(self, template_admin, request_from, db):
        request = request_from()
        form_class = template_admin.get_form(request)
        form = form_class(data=form_data(status=ManagedTemplateStatus.ACTIVE.value))
        assert form.is_valid(), form.errors

        obj = form.save(commit=False)
        template_admin.save_model(request, obj, form, change=False)

        record = obj.history.get()
        assert record.status == ManagedTemplateStatus.ACTIVE.value
        assert record.created_by_id == request.user.pk

    def test_creating_attributes_the_template_to_the_editor(self, template_admin, request_from, db):
        request = request_from()
        form = template_admin.get_form(request)(data=form_data())
        assert form.is_valid(), form.errors

        obj = form.save(commit=False)
        template_admin.save_model(request, obj, form, change=False)

        assert obj.created_by_id == request.user.pk

    def test_an_explicit_author_is_not_overwritten(self, template_admin, request_from, editor, db):
        request = request_from()
        form = template_admin.get_form(request)(data=form_data(created_by=editor.pk))
        assert form.is_valid(), form.errors

        obj = form.save(commit=False)
        template_admin.save_model(request, obj, form, change=False)

        assert obj.created_by_id == editor.pk

    def test_changing_the_status_appends_one_record(
        self, template_admin, request_from, make_template
    ):
        template = make_template(status=ManagedTemplateStatus.DRAFT.value)
        request = request_from()
        form_class = template_admin.get_form(request, template)
        form = form_class(
            data=form_data(status=ManagedTemplateStatus.ACTIVE.value), instance=template
        )
        assert form.is_valid(), form.errors

        template_admin.save_model(request, form.save(commit=False), form, change=True)

        assert [r.status for r in template.history.all()] == [ManagedTemplateStatus.ACTIVE.value]
        template.refresh_from_db()
        assert template.status == ManagedTemplateStatus.ACTIVE.value

    def test_editing_other_fields_leaves_the_history_alone(
        self, template_admin, request_from, make_template
    ):
        template = make_template(status=ManagedTemplateStatus.DRAFT.value)
        request = request_from()
        form_class = template_admin.get_form(request, template)
        form = form_class(data=form_data(name="Renamed"), instance=template)
        assert form.is_valid(), form.errors

        template_admin.save_model(request, form.save(commit=False), form, change=True)

        assert template.history.count() == 0
        template.refresh_from_db()
        assert template.name == "Renamed"

    def test_the_history_row_inherits_the_tenant(self, template_admin, request_from, db):
        request = request_from()
        form = template_admin.get_form(request)(data=form_data(tenant="acme"))
        assert form.is_valid(), form.errors

        obj = form.save(commit=False)
        template_admin.save_model(request, obj, form, change=False)

        assert obj.history.get().tenant == "acme"

    def test_a_failed_save_leaves_no_orphan_history(
        self, template_admin, request_from, db, monkeypatch
    ):
        """save_model wraps both writes in one transaction, so a failure partway cannot leave a
        status record pointing at a template that was never committed."""
        request = request_from()
        form = template_admin.get_form(request)(data=form_data())
        assert form.is_valid(), form.errors
        obj = form.save(commit=False)

        def boom(*args, **kwargs):
            raise RuntimeError("history write failed")

        monkeypatch.setattr(ManagedTemplateStatusRecord.objects, "create", boom)

        with pytest.raises(RuntimeError):
            template_admin.save_model(request, obj, form, change=False)

        assert ManagedTemplate.objects.count() == 0
        assert ManagedTemplateStatusRecord.objects.count() == 0


class TestHistoryIsReadOnly:
    def test_the_inline_cannot_be_added_to(self, request_from):
        inline = ManagedTemplateStatusRecordInline(ManagedTemplate, site)
        assert inline.has_add_permission(request_from()) is False

    def test_the_inline_shows_every_field_read_only(self):
        inline = ManagedTemplateStatusRecordInline(ManagedTemplate, site)
        assert set(inline.readonly_fields) == set(inline.fields)

    def test_the_inline_preloads_the_author(self, request_from, make_template, editor):
        template = make_template()
        ManagedTemplateStatusRecord.objects.create(
            template=template, status=ManagedTemplateStatus.ACTIVE.value, created_by=editor
        )
        inline = ManagedTemplateStatusRecordInline(ManagedTemplate, site)
        queryset = inline.get_queryset(request_from())
        assert queryset.query.select_related == {"created_by": {}}

    @pytest.mark.parametrize("permission", ["add", "change", "delete"])
    def test_the_audit_admin_forbids_writes(self, history_admin, request_from, permission):
        check = getattr(history_admin, f"has_{permission}_permission")
        assert check(request_from()) is False

    def test_the_audit_admin_still_allows_viewing(self, history_admin, request_from):
        assert history_admin.has_view_permission(request_from()) is True


class TestAdminPagesRender:
    @pytest.mark.parametrize("route", ["changelist", "add"])
    def test_template_pages(self, admin_client, make_template, route):
        make_template()
        url = reverse(f"admin:vintasend_django_templates_manager_managedtemplate_{route}")
        assert admin_client.get(url).status_code == 200

    def test_the_change_page_shows_the_history_inline(self, admin_client, make_template):
        template = make_template()
        ManagedTemplateStatusRecord.objects.create(
            template=template, status=ManagedTemplateStatus.ACTIVE.value
        )
        url = reverse(
            "admin:vintasend_django_templates_manager_managedtemplate_change", args=[template.pk]
        )
        response = admin_client.get(url)
        assert response.status_code == 200
        assert b"status history" in response.content.lower()

    def test_searching_templates(self, admin_client, make_template):
        make_template(key="welcome", name="Welcome email")
        make_template(key="reset", name="Password reset")
        url = reverse("admin:vintasend_django_templates_manager_managedtemplate_changelist")
        response = admin_client.get(url, {"q": "Password"})
        assert response.status_code == 200
        assert response.context["cl"].result_count == 1

    def test_filtering_templates_by_status(self, admin_client, templates):
        url = reverse("admin:vintasend_django_templates_manager_managedtemplate_changelist")
        response = admin_client.get(url, {"status__exact": ManagedTemplateStatus.ACTIVE.value})
        assert response.status_code == 200
        assert response.context["cl"].result_count == 1

    def test_the_audit_changelist_renders(self, admin_client, make_template):
        template = make_template()
        ManagedTemplateStatusRecord.objects.create(
            template=template, status=ManagedTemplateStatus.ACTIVE.value
        )
        url = reverse(
            "admin:vintasend_django_templates_manager_managedtemplatestatusrecord_changelist"
        )
        assert admin_client.get(url).status_code == 200

    def test_adding_through_the_admin_creates_the_template_and_its_history(self, admin_client, db):
        url = reverse("admin:vintasend_django_templates_manager_managedtemplate_add")
        response = admin_client.post(
            url, form_data(**{"history-TOTAL_FORMS": "0", "history-INITIAL_FORMS": "0"})
        )
        assert response.status_code == 302
        template = ManagedTemplate.objects.get(key="welcome")
        assert template.history.count() == 1


# ----------------------------------------------------------------------
# Tag admin
# ----------------------------------------------------------------------


@pytest.fixture
def tag_admin():
    return ManagedTemplateTagAdmin(ManagedTemplateTag, site)


def tag_form_data(**overrides):
    data = {
        "text": "Black Friday",
        "status": ManagedTemplateTagStatus.ACTIVE.value,
        "tenant": "",
    }
    return {**data, **overrides}


def test_the_tag_admin_is_registered():
    assert isinstance(site._registry[ManagedTemplateTag], ManagedTemplateTagAdmin)


def test_saving_a_tag_derives_its_slug_from_the_text(db):
    form = ManagedTemplateTagAdminForm(data=tag_form_data())

    assert form.is_valid(), form.errors
    assert form.save().slug == "black-friday"


def test_the_slug_is_not_an_editable_field(tag_admin):
    """It is the tag's identity and the library owns how it is produced."""
    assert "slug" not in ManagedTemplateTagAdminForm.Meta.fields
    assert "slug" in tag_admin.readonly_fields


def test_a_tag_whose_text_cannot_be_slugified_is_rejected(db):
    form = ManagedTemplateTagAdminForm(data=tag_form_data(text="!!!"))

    assert not form.is_valid()
    assert "text" in form.errors


def test_saving_a_tag_onto_a_taken_slug_gets_a_numeric_suffix(db, make_tag):
    make_tag("Black Friday")

    form = ManagedTemplateTagAdminForm(data=tag_form_data(text="black friday"))

    assert form.is_valid(), form.errors
    assert form.save().slug == "black-friday-2"


def test_renaming_a_tag_through_the_admin_keeps_its_own_slug(db, make_tag):
    tag = make_tag("Black Friday")

    form = ManagedTemplateTagAdminForm(data=tag_form_data(text="BLACK FRIDAY"), instance=tag)

    assert form.is_valid(), form.errors
    assert form.save().slug == "black-friday"


def test_a_tags_text_is_trimmed_on_save(db):
    form = ManagedTemplateTagAdminForm(data=tag_form_data(text="  Black   Friday  "))

    assert form.is_valid(), form.errors
    assert form.save().text == "Black Friday"


def test_creating_a_tag_records_the_admin_who_made_it(tag_admin, request_from, admin_user):
    request = request_from()
    form = ManagedTemplateTagAdminForm(data=tag_form_data())
    assert form.is_valid(), form.errors
    tag = form.save(commit=False)

    tag_admin.save_model(request, tag, form, change=False)

    tag.refresh_from_db()
    assert tag.created_by_id == admin_user.pk


def test_the_template_count_reports_how_many_versions_carry_the_tag(
    tag_admin, request_from, make_tag, make_template
):
    tag = make_tag("Onboarding")
    for index in range(2):
        template = make_template(key=f"key-{index}")
        template.tags.set([tag])

    row = tag_admin.get_queryset(request_from()).get(pk=tag.pk)

    assert tag_admin.template_count(row) == 2


def test_the_archive_action_retires_tags_without_touching_their_templates(
    tag_admin, request_from, make_tag, make_template
):
    tag = make_tag("Onboarding")
    template = make_template()
    template.tags.set([tag])

    tag_admin.archive_tags(request_from(), ManagedTemplateTag.objects.all())

    tag.refresh_from_db()
    assert tag.status == ManagedTemplateTagStatus.ARCHIVED.value
    assert list(template.tags.all()) == [tag]


def test_the_restore_action_puts_a_tag_back_on_offer(tag_admin, request_from, make_tag):
    tag = make_tag("Onboarding", status=ManagedTemplateTagStatus.ARCHIVED.value)

    tag_admin.restore_tags(request_from(), ManagedTemplateTag.objects.all())

    tag.refresh_from_db()
    assert tag.status == ManagedTemplateTagStatus.ACTIVE.value


def test_the_template_admin_lists_a_templates_tags(template_admin, make_template, make_tag):
    template = make_template()
    template.tags.set([make_tag("Onboarding"), make_tag("Billing")])

    assert template_admin.tag_list(template) == "Billing, Onboarding"


def test_the_template_changelist_does_not_query_tags_per_row(
    template_admin, request_from, make_template, make_tag, django_assert_num_queries
):
    tag = make_tag("Onboarding")
    for index in range(3):
        template = make_template(key=f"key-{index}")
        template.tags.set([tag])

    queryset = template_admin.get_queryset(request_from())

    with django_assert_num_queries(2):
        [template_admin.tag_list(row) for row in queryset]


def test_a_template_can_be_tagged_through_the_admin_form(db, make_tag):
    tag = make_tag("Onboarding")

    form = ManagedTemplateAdminForm(data=form_data(tags=[tag.pk]))

    assert form.is_valid(), form.errors
    assert list(form.save().tags.all()) == [tag]
