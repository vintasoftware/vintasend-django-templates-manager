from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models

from .contants import ManagedTemplateStatusChoices, ManagedTemplateTagStatusChoices
from .managers import ManagedTemplateManager, ManagedTemplateTagManager


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


class ManagedTemplateTag(models.Model):
    """A label shared across templates, identified by its slug.

    ``slug`` is normalized from ``text`` by ``vintasend_managed_templates.tags.slugify_tag``
    and is unique store-wide, which is what makes it the thing filters match on and URLs
    carry. Two tags may read the same ("Black Friday" twice, from different tenants) and are
    then told apart by the ``-2`` suffix ``DjangoTemplateManager`` appends.

    Uniqueness is global rather than per-tenant, matching ``ManagedTemplate.key``: this app
    treats ``tenant`` as a label on a row, not as a partition of the key space.
    """

    text = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    status = models.CharField(
        max_length=50,
        choices=ManagedTemplateTagStatusChoices.choices,
        default=ManagedTemplateTagStatusChoices.ACTIVE,
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="created_template_tags",
    )
    # NULL is meaningful here for the same reason it is on ManagedTemplate: the dataclass
    # types it as `str | None`, so an absent tenant has to round-trip back as None.
    tenant = models.CharField(max_length=255, null=True, blank=True, db_index=True)  # noqa: DJ001

    objects: ManagedTemplateTagManager = ManagedTemplateTagManager()

    templates: "RelatedManager[ManagedTemplate]"

    class Meta:
        ordering = ("text", "id")

    def __str__(self):
        return self.text


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
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="created_templates",
    )
    updated = models.DateTimeField(auto_now=True)
    tenant = models.CharField(max_length=255, null=True, blank=True, db_index=True)  # noqa: DJ001
    # Tags hang off a *version*, not off a key: two versions of one template can be labelled
    # differently, which is what lets a draft be tagged for review without relabelling the
    # version currently live.
    tags = models.ManyToManyField(
        ManagedTemplateTag,
        related_name="templates",
        blank=True,
    )

    objects: ManagedTemplateManager = ManagedTemplateManager()

    history: "RelatedManager[ManagedTemplateStatusRecord]"

    def __str__(self):
        return self.name


class ManagedTemplateStatusRecord(models.Model):
    template = models.ForeignKey(ManagedTemplate, on_delete=models.CASCADE, related_name="history")
    status = models.CharField(max_length=50, choices=ManagedTemplateStatusChoices.choices)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="modified_template_status",
    )
    tenant = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001

    def __str__(self):
        return f"{self.template.key} v{self.template.version}: {self.status}"
