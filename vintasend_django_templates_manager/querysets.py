from typing import TYPE_CHECKING

from django.db.models import Count, Exists, OuterRef, Q, QuerySet

from vintasend_managed_templates.constants import (
    MOST_RECENT_ACTIVE_VERSION_STATUSES,
    ManagedTemplateTagStatus,
)
from vintasend_managed_templates.tags import slugify_tag


if TYPE_CHECKING:
    from .models import ManagedTemplate, ManagedTemplateTag


class ManagedTemplateQuerySet(QuerySet["ManagedTemplate", "ManagedTemplate"]):
    def get_latest_version(self, key: str) -> "ManagedTemplate | None":
        """
        Retrieves the latest version of a template based on its key.

        param key: str
        return: ManagedTemplate | None
        """
        return self.filter(key=key).order_by("-version").first()

    def with_all_tags(self, slugs: list[str]) -> "ManagedTemplateQuerySet":
        """
        Narrows to the templates carrying every one of these tags.

        param slugs: list[str]
        return: ManagedTemplateQuerySet
        """
        return self.filter(all_tags_q(slugs))

    def with_any_tags(self, slugs: list[str]) -> "ManagedTemplateQuerySet":
        """
        Narrows to the templates carrying at least one of these tags.

        param slugs: list[str]
        return: ManagedTemplateQuerySet
        """
        return self.filter(any_tags_q(slugs))

    def most_recent_active_versions(self) -> "ManagedTemplateQuerySet":
        """
        Narrows to one row per key: its current version.

        return: ManagedTemplateQuerySet
        """
        return self.filter(most_recent_active_version_q())

    def abstract(self) -> "ManagedTemplateQuerySet":
        """
        Narrows to the bases: the templates other templates are built on.

        Reads the denormalized ``is_abstract`` column, which ``ManagedTemplate.save`` keeps in
        step with the sources -- which is what makes this a WHERE clause rather than a parse
        of every row.

        return: ManagedTemplateQuerySet
        """
        return self.filter(is_abstract=True)

    def sendable(self) -> "ManagedTemplateQuerySet":
        """
        Narrows to the templates meant to be sent, leaving the bases out.

        The list a "which template does this notification use" picker should be drawn from:
        an abstract template renders perfectly well, it just renders a layout with a hole in
        it where the content was supposed to go.

        return: ManagedTemplateQuerySet
        """
        return self.filter(is_abstract=False)


def normalize_tag_slugs(tags: object) -> list[str]:
    """Slugify a filter's tag values, so a filter may name a tag by the text behind it.

    Values that slugify to nothing are dropped: they name no tag, so keeping them would make
    an ``all`` filter unsatisfiable for a reason the caller cannot see.
    """
    if isinstance(tags, str) or not isinstance(tags, (list, tuple, set)):
        return []
    slugs: list[str] = []
    for tag in tags:
        slug = slugify_tag(str(tag))
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def any_tags_q(slugs: list[str]) -> Q:
    """A ``Q`` matching templates carrying at least one of ``slugs``.

    Built as a subquery rather than a plain ``tags__slug__in`` join: joining would return one
    row per matching tag, so a template with two of the tags would appear twice and take two
    slots out of a page. It also makes the filter safe to negate, since ``~Q(pk__in=...)``
    means what it reads as while a negated join does not.

    An empty list matches nothing, following Python's ``any([])``.
    """
    from .models import ManagedTemplate

    if not slugs:
        return Q(pk__in=[])
    matching = ManagedTemplate.objects.filter(tags__slug__in=slugs).values("pk")
    return Q(pk__in=matching)


def all_tags_q(slugs: list[str]) -> Q:
    """A ``Q`` matching templates carrying every one of ``slugs``.

    Counts the distinct matching tags per template and keeps the ones that reached the full
    set. Filtering before annotating is what makes the count mean "of the tags asked for":
    Django reuses the join the filter established, so the aggregate sees only those rows.

    An empty list constrains nothing, following Python's ``all([])``.
    """
    from .models import ManagedTemplate

    if not slugs:
        return Q()
    matching = (
        ManagedTemplate.objects.filter(tags__slug__in=slugs)
        .values("pk")
        .annotate(matched=Count("tags", distinct=True))
        .filter(matched=len(slugs))
        .values("pk")
    )
    return Q(pk__in=matching)


def most_recent_active_version_q() -> Q:
    """A ``Q`` matching the current version of each key.

    Current means the highest-numbered version whose status is in
    ``MOST_RECENT_ACTIVE_VERSION_STATUSES`` -- what is published, plus the draft on its way to
    replacing it. A key whose versions are all INACTIVE or ARCHIVED matches nothing.

    Written as "this row is active or draft, and no active-or-draft row of the same key is
    numbered higher" rather than as a ``pk IN (... ORDER BY version DESC LIMIT 1)`` subquery:
    MySQL rejects ``LIMIT`` inside ``IN``, and this form negates cleanly, which the filter
    translator needs to express ``not {"most_recent_active_version": True}``.
    """
    from .models import ManagedTemplate

    statuses = [status.value for status in MOST_RECENT_ACTIVE_VERSION_STATUSES]
    higher_version = ManagedTemplate.objects.filter(
        key=OuterRef("key"),
        status__in=statuses,
        version__gt=OuterRef("version"),
    )
    return Q(status__in=statuses) & ~Q(Exists(higher_version))


class ManagedTemplateTagQuerySet(QuerySet["ManagedTemplateTag", "ManagedTemplateTag"]):
    def get_by_slug(self, slug: str) -> "ManagedTemplateTag | None":
        """
        Retrieves one tag by slug, or by the text the slug would come from.

        Slugifying here is what makes ``"Black Friday"`` and ``"black-friday"`` the same
        lookup everywhere, rather than in each caller.

        param slug: str
        return: ManagedTemplateTag | None
        """
        return self.filter(slug=slugify_tag(slug)).first()

    def active(self) -> "ManagedTemplateTagQuerySet":
        """
        Narrows to the tags still on offer -- what a tag picker should show.

        return: ManagedTemplateTagQuerySet
        """
        return self.filter(status=ManagedTemplateTagStatus.ACTIVE.value)

    def search(self, term: str) -> "ManagedTemplateTagQuerySet":
        """
        Narrows to the tags whose text or slug contains ``term``, ignoring case.

        param term: str
        return: ManagedTemplateTagQuerySet
        """
        return self.filter(Q(text__icontains=term) | Q(slug__icontains=term))
