from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin
from django.db import transaction
from django.db.models import Count
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from vintasend_managed_templates.composition import TEMPLATE_FIELDS
from vintasend_managed_templates.constants import ManagedTemplateStatus, ManagedTemplateTagStatus
from vintasend_managed_templates.exceptions import ManagedTemplateCompositionError
from vintasend_managed_templates.tags import next_available_slug, slugify_tag

from .composition import backend_composer
from .contants import ManagedTemplateStatusChoices, ManagedTemplateTagStatusChoices
from .models import ManagedTemplate, ManagedTemplateStatusRecord, ManagedTemplateTag


if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser


def _acting_user(request: HttpRequest) -> "AbstractBaseUser | AnonymousUser | None":
    """The logged-in admin user, or None when the request is unauthenticated."""
    user = request.user
    return user if user.is_authenticated else None


class ManagedTemplateStatusRecordInline(admin.TabularInline):
    """Read-only audit trail shown on the template page.

    Records are written by ``DjangoTemplateManager.create_template_status_update`` and by
    ``ManagedTemplateAdmin.save_model``, never typed in by hand.
    """

    model = ManagedTemplateStatusRecord
    extra = 0
    can_delete = False
    fields = ("status", "created", "created_by", "tenant")
    readonly_fields = fields
    ordering = ("-created",)
    verbose_name = _("status change")
    verbose_name_plural = _("status history")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).select_related("created_by")


class ManagedTemplateAdminForm(forms.ModelForm):
    # ``ManagedTemplate.status`` is a plain CharField on the model so the storage layer stays
    # decoupled from the enum. The admin still renders it as a select.
    status = forms.ChoiceField(choices=ManagedTemplateStatusChoices.choices, label=_("status"))

    # Composition is resolved before the template engine ever sees a template, so a base that
    # does not exist or a block left open is this app's error to report -- Django's engine
    # will never complain about either, and the first sign of trouble would otherwise be a
    # notification that failed to send. Checking here turns that into a form error, on the
    # field that carries it. Set False on a subclass to save a template whose base has not
    # been written yet.
    validate_composition = True

    class Meta:
        model = ManagedTemplate
        # Listed explicitly rather than "__all__" so a new model field cannot silently become
        # editable in the admin (and to satisfy flake8-django's DJ007).
        fields = (
            "name",
            "key",
            "version",
            "template_managed_backend",
            "description",
            "subject_template",
            "preheader_template",
            "body_template",
            "status",
            "tags",
            "tenant",
            "created_by",
        )

    def clean(self) -> dict[str, Any]:
        """Assemble each source the way rendering will, and report what does not.

        The key and version of the row being edited are handed to the composer so a chain of
        bases that leads back here is reported as the loop it is, rather than quietly
        composing against the stored -- and by now stale -- copy of this very row.
        """
        cleaned_data: dict[str, Any] = super().clean() or {}
        if not self.validate_composition:
            return cleaned_data

        composer = backend_composer()
        # On a change form ``key`` and ``version`` are read-only, so they arrive on the
        # instance rather than in the cleaned data.
        key = cleaned_data.get("key") or self.instance.key or None
        version = cleaned_data.get("version") or self.instance.version or None

        for field in TEMPLATE_FIELDS:
            source = cleaned_data.get(field)
            if not source:
                continue
            try:
                composer.compose_source(source, field=field, key=key, version=version)
            except ManagedTemplateCompositionError as error:
                self.add_error(field, str(error))

        return cleaned_data


@admin.register(ManagedTemplate)
class ManagedTemplateAdmin(admin.ModelAdmin):
    form = ManagedTemplateAdminForm
    inlines = (ManagedTemplateStatusRecordInline,)

    list_display = (
        "name",
        "key",
        "version",
        "template_managed_backend",
        "status",
        "is_abstract",
        "tag_list",
        "tenant",
        "created_by",
        "updated",
    )
    list_display_links = ("name", "key")
    list_filter = (
        "status",
        "is_abstract",
        "template_managed_backend",
        "tags",
        "tenant",
        "created",
    )
    list_select_related = ("created_by",)
    filter_horizontal = ("tags",)
    search_fields = ("key", "name", "description", "tags__text", "tags__slug")
    search_help_text = _("Search by template key, name, description or tag.")
    ordering = ("key", "-version")
    date_hierarchy = "created"
    raw_id_fields = ("created_by",)

    fieldsets = (
        (None, {"fields": ("name", "key", "version", "template_managed_backend", "description")}),
        (
            _("Content"),
            {
                "fields": (
                    "subject_template",
                    "preheader_template",
                    "body_template",
                    "composed_body",
                ),
                "description": _(
                    'A template may build on another with {% managed_extends "key" %}, fill '
                    "its {% managed_children %} hole, override its {% managed_block name %} "
                    'regions, and splice in a fragment with {% managed_include "key" %}. All '
                    "of it is resolved before the template engine runs, so the engine's own "
                    "tags are left alone."
                ),
            },
        ),
        (_("Lifecycle"), {"fields": ("status", "tags", "tenant")}),
        (_("Audit"), {"fields": ("created", "created_by", "updated")}),
    )

    def get_queryset(self, request: HttpRequest):
        # ``tag_list`` reads every row's tags, so without this the changelist runs one query
        # per row. Searching across ``tags__*`` also joins, hence the distinct().
        return super().get_queryset(request).prefetch_related("tags").distinct()

    @admin.display(description=_("tags"))
    def tag_list(self, obj: ManagedTemplate) -> str:
        return ", ".join(tag.text for tag in obj.tags.all())

    @admin.display(description=_("composed body"))
    def composed_body(self, obj: "ManagedTemplate | None" = None) -> str:
        """The body as the template engine will receive it, every ``managed_*`` tag resolved.

        The point of showing it is that the stored body is only half the template: what
        actually goes out is this. It resolves against whatever the referenced templates are
        *now*, so it is a preview of the next send rather than a record of the last one.
        """
        if obj is None or obj.pk is None:
            return str(_("Save the template to see what it composes to."))

        try:
            composed = backend_composer().compose_source(
                obj.body_template,
                field="body_template",
                key=obj.key,
                version=obj.version,
            )
        except ManagedTemplateCompositionError as error:
            return format_html('<ul class="errorlist"><li>{}</li></ul>', str(error))

        return format_html('<pre style="white-space: pre-wrap">{}</pre>', composed)

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        # ``composed_body`` is a rendered preview, not a field: it is only ever read-only.
        readonly: tuple[str, ...] = ("created", "updated", "composed_body")
        if obj is not None:
            # ``key`` + ``version`` are the template's identity: ``get_template`` and
            # ``get_latest_version`` look rows up by them, and ``update_template`` owns the
            # version bump. Editing them in place would detach a row from its own history.
            readonly += ("key", "version")
        return readonly

    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, str | list[str]]:
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("version", "1")
        initial.setdefault("status", ManagedTemplateStatus.DRAFT.value)
        return initial

    def save_model(
        self, request: HttpRequest, obj: ManagedTemplate, form: forms.ModelForm, change: bool
    ) -> None:
        user = _acting_user(request)
        if not change and obj.created_by_id is None:
            obj.created_by_id = user.pk if user else None

        # Mirror ``create_template_status_update``: the current status and its history entry are
        # written together, so a status set through the admin is not missing from the trail.
        status_changed = not change or "status" in form.changed_data
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if status_changed:
                ManagedTemplateStatusRecord.objects.create(
                    template=obj,
                    status=obj.status,
                    created_by_id=user.pk if user else None,
                    tenant=obj.tenant,
                )


@admin.register(ManagedTemplateStatusRecord)
class ManagedTemplateStatusRecordAdmin(admin.ModelAdmin):
    """Browsable, non-editable view over the status audit trail."""

    list_display = ("template", "status", "created", "created_by", "tenant")
    list_filter = ("status", "created", "tenant")
    list_select_related = ("template", "created_by")
    search_fields = ("template__key", "template__name")
    search_help_text = _("Search by template key or name.")
    ordering = ("-created",)
    date_hierarchy = "created"
    raw_id_fields = ("template", "created_by")
    readonly_fields = ("template", "status", "created", "created_by", "tenant")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ManagedTemplateTagAdminForm(forms.ModelForm):
    """Keeps ``slug`` derived from ``text`` instead of typed in.

    The slug is the tag's identity -- what filters match and URLs carry -- and
    ``DjangoTemplateManager`` owns how it is produced. Letting it be edited here would let the
    admin mint a slug the library would never generate, so it is computed on save and the
    field is not offered.
    """

    status = forms.ChoiceField(choices=ManagedTemplateTagStatusChoices.choices, label=_("status"))

    class Meta:
        model = ManagedTemplateTag
        fields = ("text", "status", "tenant", "created_by")

    def clean_text(self) -> str:
        text = " ".join(self.cleaned_data["text"].split())
        if not slugify_tag(text):
            raise forms.ValidationError(
                _("This text has no characters that can be turned into a slug.")
            )
        return text

    def save(self, commit: bool = True) -> ManagedTemplateTag:
        tag: ManagedTemplateTag = super().save(commit=False)
        tag.slug = next_available_slug(
            slugify_tag(tag.text),
            lambda candidate: (
                ManagedTemplateTag.objects.filter(slug=candidate).exclude(pk=tag.pk).exists()
            ),
        )
        if commit:
            tag.save()
        return tag


@admin.register(ManagedTemplateTag)
class ManagedTemplateTagAdmin(admin.ModelAdmin):
    form = ManagedTemplateTagAdminForm

    list_display = ("text", "slug", "status", "template_count", "tenant", "updated")
    list_display_links = ("text", "slug")
    list_filter = ("status", "tenant", "created")
    list_select_related = ("created_by",)
    search_fields = ("text", "slug")
    search_help_text = _("Search by tag text or slug.")
    ordering = ("text",)
    raw_id_fields = ("created_by",)
    readonly_fields = ("slug", "created", "updated")
    actions = ("archive_tags", "restore_tags")

    fieldsets = (
        (None, {"fields": ("text", "slug", "status", "tenant")}),
        (_("Audit"), {"fields": ("created", "created_by", "updated")}),
    )

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).annotate(_templates=Count("templates"))

    @admin.display(description=_("templates"), ordering="_templates")
    def template_count(self, obj: ManagedTemplateTag) -> int:
        """How many template versions carry this tag -- what a delete would strip the label from."""
        return obj._templates  # type: ignore[attr-defined]

    def save_model(
        self, request: HttpRequest, obj: ManagedTemplateTag, form: forms.ModelForm, change: bool
    ) -> None:
        user = _acting_user(request)
        if not change and obj.created_by_id is None:
            obj.created_by_id = user.pk if user else None
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Archive selected tags"))
    def archive_tags(self, request: HttpRequest, queryset: Any) -> None:
        """Retire tags from the pickers. The templates carrying them keep them."""
        queryset.update(status=ManagedTemplateTagStatus.ARCHIVED.value)

    @admin.action(description=_("Restore selected tags"))
    def restore_tags(self, request: HttpRequest, queryset: Any) -> None:
        queryset.update(status=ManagedTemplateTagStatus.ACTIVE.value)
