from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models

from .contants import ManagedTemplateStatusChoices
from .managers import ManagedTemplateManager


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


class ManagedTemplate(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    key = models.CharField(max_length=255, unique=True)
    template_managed_backend = models.CharField(max_length=255, db_index=True)
    body_template = models.TextField()
    # NULL is meaningful on the fields below, not interchangeable with "": the
    # vintasend_managed_templates dataclasses type them as `str | None`, so an absent
    # subject/preheader/tenant has to round-trip back as None. Hence noqa: DJ001.
    subject_template = models.TextField(null=True, blank=True)  # noqa: DJ001
    preheader_template = models.TextField(null=True, blank=True)  # noqa: DJ001
    version = models.PositiveIntegerField()
    status = models.CharField(max_length=50)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="created_templates"
    )
    updated = models.DateTimeField(auto_now=True)
    tenant = models.CharField(max_length=255, null=True, blank=True, db_index=True)  # noqa: DJ001

    objects: ManagedTemplateManager = ManagedTemplateManager()

    history: "RelatedManager[ManagedTemplateStatusRecord]"

    def __str__(self):
        return self.name


class ManagedTemplateStatusRecord(models.Model):
    template = models.ForeignKey(ManagedTemplate, on_delete=models.CASCADE, related_name="history")
    status = models.CharField(max_length=50, choices=ManagedTemplateStatusChoices.choices)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="modified_template_status"
    )
    tenant = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001

    def __str__(self):
        return f"{self.template.key} v{self.template.version}: {self.status}"
