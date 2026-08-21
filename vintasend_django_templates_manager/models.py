from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models

from .composition import template_is_abstract
from .contants import ManagedTemplateStatusChoices, ManagedTemplateTagStatusChoices
from .managers import ManagedTemplateManager, ManagedTemplateTagManager


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


# The fields ``ManagedTemplate.is_abstract`` is derived from: change one and the flag has to
# be recomputed, leave them alone and it cannot have changed.
SOURCE_FIELDS = frozenset({"body_template", "subject_template", "preheader_template"})


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
    """One **version** of a template. A key has as many rows as it has versions.

    Several versions of one key can be live at once, and that is the point: a notification
    recorded against v1 keeps rendering v1 after v2 is published, so the two flows never
    collide. Nothing about a version is edited once it exists --
    ``DjangoTemplateManager.update_template`` inserts the next one and leaves its predecessor
    alone -- except its ``status`` and its tags, which are what a version's lifecycle is.
    """

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    # Not unique on its own: the key names the *template*, and the (key, version) pair below
    # names the row. No db_index either -- the unique constraint's index is on (key, version),
    # whose leftmost column is key, so a lookup by key alone already uses it.
    key = models.CharField(max_length=255)
    template_managed_backend = models.CharField(max_length=255, db_index=True)
    body_template = models.TextField()
    # NULL is meaningful on the fields below, not interchangeable with "": the
    # vintasend_managed_templates dataclasses type them as `str | None`, so an absent
    # subject/preheader/tenant has to round-trip back as None. Hence noqa: DJ001.
    subject_template = models.TextField(null=True, blank=True)  # noqa: DJ001
    preheader_template = models.TextField(null=True, blank=True)  # noqa: DJ001
    version = models.PositiveIntegerField()
    status = models.CharField(max_length=50)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="created_templates",
    )
    updated = models.DateTimeField(auto_now=True, db_index=True)
    tenant = models.CharField(max_length=255, null=True, blank=True, db_index=True)  # noqa: DJ001
    # Whether this is a base to build on rather than a template to send -- it declares a
    # ``{% managed_children %}`` hole, or blocks without extending anything.
    #
    # Derived, never typed in, which is what ``editable=False`` says: ``save()`` recomputes it
    # from this row's own sources every time one of them changes. The column exists so a
    # picker can exclude bases with a WHERE clause; answering the same question by parsing
    # would mean reading every row in the store to draw one page. Indexed for that query, and
    # for it alone.
    is_abstract = models.BooleanField(default=False, editable=False, db_index=True)
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

    class Meta:
        constraints = [  # noqa: RUF012 - Django Meta options are not ClassVar-annotated
            # The row's identity. It is also what makes a concurrent second ``update_template``
            # fail loudly instead of silently minting a duplicate version number: both
            # transactions read the same latest version, and only one insert can win.
            models.UniqueConstraint(
                fields=("key", "version"),
                name="vintasend_managed_template_unique_key_version",
            ),
        ]

    def __str__(self):
        # The version is part of the label because it is part of the identity: without it two
        # versions of one template read identically everywhere Django renders a row by name.
        return f"{self.name} (v{self.version})"

    def save(self, *args, **kwargs):
        """Bring ``is_abstract`` back in step with the sources on the way to the database.

        Here rather than in ``DjangoTemplateManager`` because the manager is not the only
        thing that writes these rows: the admin, a data migration and a shell session all go
        through ``save``, and a denormalized flag that only one write path maintains is a flag
        that drifts. A malformed template reads as concrete rather than raising -- a save is
        not the place to report a syntax error, and the admin form has already refused it.

        A save narrowed with ``update_fields`` recomputes only when it touches a source, and
        then carries ``is_abstract`` along so the new value is actually written.
        """
        update_fields = kwargs.get("update_fields")
        if update_fields is None or not SOURCE_FIELDS.isdisjoint(update_fields):
            self.is_abstract = template_is_abstract(self, strict=False)
            if update_fields is not None:
                kwargs["update_fields"] = [*update_fields, "is_abstract"]
        super().save(*args, **kwargs)


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
