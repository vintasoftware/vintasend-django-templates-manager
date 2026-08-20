from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from vintasend_managed_templates.constants import ManagedTemplateStatus

from .contants import ManagedTemplateStatusChoices
from .models import ManagedTemplate, ManagedTemplateStatusRecord


if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


def _acting_user(request: HttpRequest) -> "AbstractBaseUser | None":
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
            "tenant",
            "created_by",
        )


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
        "tenant",
        "created_by",
        "updated",
    )
    list_display_links = ("name", "key")
    list_filter = ("status", "template_managed_backend", "tenant", "created")
    list_select_related = ("created_by",)
    search_fields = ("key", "name", "description")
    search_help_text = _("Search by template key, name or description.")
    ordering = ("key", "-version")
    date_hierarchy = "created"
    raw_id_fields = ("created_by",)

    fieldsets = (
        (None, {"fields": ("name", "key", "version", "template_managed_backend", "description")}),
        (_("Content"), {"fields": ("subject_template", "preheader_template", "body_template")}),
        (_("Lifecycle"), {"fields": ("status", "tenant")}),
        (_("Audit"), {"fields": ("created", "created_by", "updated")}),
    )

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        readonly: tuple[str, ...] = ("created", "updated")
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
