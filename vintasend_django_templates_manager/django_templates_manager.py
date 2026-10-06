import functools
from collections.abc import Iterable
from enum import Enum

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.db import transaction
from django.db.models import Q

from vintasend_managed_templates.base_template_manager_backend import BaseTemplateManagerBackend
from vintasend_managed_templates.constants import ManagedTemplateStatus, ManagedTemplateTagStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate as ManagedTemplateDataclass,
)
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateTag as ManagedTemplateTagDataclass,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateChangeUserNotFoundError,
    ManagedTemplateInvalidFilterError,
    ManagedTemplateInvalidTagError,
    ManagedTemplateNotFoundError,
    ManagedTemplateTagAlreadyExistsError,
    ManagedTemplateTagNotFoundError,
)
from vintasend_managed_templates.filters import (
    MANAGED_TEMPLATE_ORDER_BY_FIELDS,
    ManagedTemplateFilter,
    ManagedTemplateOrderBy,
    is_choice_exact_filter_lookup,
    is_choice_in_filter_lookup,
    is_date_filter_lookup,
    is_field_filter,
    is_integer_filter_lookup,
    is_string_filter_lookup,
    is_string_membership_exact_lookup,
    is_string_membership_in_lookup,
    order_by_capability_key,
)
from vintasend_managed_templates.tags import next_available_slug, slugify_tag

from .models import ManagedTemplate, ManagedTemplateStatusRecord, ManagedTemplateTag
from .querysets import (
    ManagedTemplateQuerySet,
    ManagedTemplateTagQuerySet,
    all_tags_q,
    any_tags_q,
    most_recent_active_version_q,
    normalize_tag_slugs,
)


User = get_user_model()


_CHOICE_FIELDS: dict[str, str] = {
    "status": "status",
}
_CHOICE_FIELD_ENUMS: dict[str, type[Enum]] = {
    "status": ManagedTemplateStatus,
}
_MEMBERSHIP_FIELDS: dict[str, str] = {
    "template_managed_backend": "template_managed_backend",
    "key": "key",
}
_INTEGER_FIELDS: dict[str, str] = {"version": "version"}
_STRING_LOOKUP_FIELDS: dict[str, str] = {
    "body_template": "body_template",
    "subject_template": "subject_template",
    "name": "name",
    "description": "description",
    "key": "key",
    "template_managed_backend": "template_managed_backend",
}
_RANGE_FIELDS: dict[str, str] = {
    "created_at_range": "created",
    "updated_at_range": "updated",
}
# Tag membership. Both take a collection of slugs rather than a lookup dict, so they are
# translated by their own branch in ``_field_leaf`` instead of by one of the tables above.
_TAG_FIELDS: frozenset[str] = frozenset({"includes_all_tags", "includes_any_of_tags"})
# The two boolean fields, each with its own branch in ``_field_leaf``. ``is_abstract`` is a
# column the model derives from its own sources on save; ``most_recent_active_version`` is the
# only filter answered against the key's *other* versions rather than against the row.
_IS_ABSTRACT_FIELD = "is_abstract"
_MOST_RECENT_ACTIVE_VERSION_FIELD = "most_recent_active_version"
# order_by field name -> model field. Every orderable field the vocabulary defines is a real
# indexed column here, so each is answered by the database rather than in memory. ``created_at``
# maps to ``created`` and ``updated_at`` to ``updated``, matching the model's ``auto_now_add`` /
# ``auto_now`` fields; the other four are named the same on both sides.
#
# Each entry was established by running the sort, not by reading the column definition.
# ``version`` is the one worth saying that about: it is a ``PositiveIntegerField``, so 10 sorts
# after 2 -- a store keeping versions as strings would sort them 10, 2, 3 and look correct until
# a key reached its tenth version.
_ORDER_FIELD_TO_ATTR: dict[str, str] = {
    "key": "key",
    "name": "name",
    "version": "version",
    "status": "status",
    "created_at": "created",
    "updated_at": "updated",
}
# Django lookup suffixes for each string lookup, case-sensitive first / case-insensitive second.
_STRING_LOOKUP_SUFFIX: dict[str, tuple[str, str]] = {
    "exact": ("exact", "iexact"),
    "starts_with": ("startswith", "istartswith"),
    "ends_with": ("endswith", "iendswith"),
    "includes": ("contains", "icontains"),
}
# Every filter field name -> its model field, used for bare (non-lookup) values.
_FILTER_FIELD_TO_MODEL_FIELD: dict[str, str] = {
    **_RANGE_FIELDS,
    **_STRING_LOOKUP_FIELDS,
    **_INTEGER_FIELDS,
    **_CHOICE_FIELDS,
    **_MEMBERSHIP_FIELDS,
}
# A Q that matches no row, used for an unknown filter field (the reference evaluator treats an
# unknown field as non-matching) and for ``not {}`` (negating the match-everything empty filter).
_MATCH_NOTHING = Q(pk__in=[])


def _and_all(queries: list[Q]) -> Q:
    if not queries:
        return Q()
    return functools.reduce(lambda left, right: left & right, queries)


def _or_all(queries: list[Q]) -> Q:
    if not queries:
        return _MATCH_NOTHING
    return functools.reduce(lambda left, right: left | right, queries)


class DjangoTemplateManager(BaseTemplateManagerBackend):
    template_backend_name = "django"

    def _serialize_tag(self, tag: ManagedTemplateTag):
        return ManagedTemplateTagDataclass(
            id=tag.pk,
            text=tag.text,
            slug=tag.slug,
            status=ManagedTemplateTagStatus(tag.status),
            created=tag.created,
            updated=tag.updated,
            tenant=tag.tenant,
        )

    def _serialize_template(self, template: ManagedTemplate):
        return ManagedTemplateDataclass(
            id=template.pk,
            name=template.name,
            description=template.description,
            key=template.key,
            template_managed_backend=template.template_managed_backend,
            body_template=template.body_template,
            subject_template=template.subject_template,
            preheader_template=template.preheader_template,
            version=template.version,
            status=ManagedTemplateStatus(template.status),
            tenant=template.tenant,
            created=template.created,
            updated=template.updated,
            tags=[self._serialize_tag(tag) for tag in template.tags.all()],
            # The stored flag rather than a fresh parse: the model derives it on every save,
            # and reading the column is what keeps serializing a page of templates from
            # parsing three sources per row.
            is_abstract=template.is_abstract,
        )

    def _serialize_template_queryset(self, queryset: ManagedTemplateQuerySet):
        # Prefetch rather than let ``template.tags.all()`` fire per row: serialization reads
        # the tags of every template it is handed, so without this a page of 20 costs 21
        # queries. Harmless when the caller already prefetched -- Django keeps the first.
        for template in queryset.prefetch_related("tags"):
            yield self._serialize_template(template)

    def _paginate_queryset(
        self, queryset: ManagedTemplateQuerySet, page: int, page_size: int
    ) -> ManagedTemplateQuerySet:
        return queryset[((page - 1) * page_size) : ((page - 1) * page_size) + page_size]

    def create_template(self, data: ManagedTemplateCreateInput):
        # M2M writes need the row to exist, so tagging is a second statement -- inside the
        # same transaction as the insert, so an unusable tag rolls the template back rather
        # than leaving one behind that the caller was told had failed.
        with transaction.atomic():
            template = ManagedTemplate.objects.create(
                name=data.name,
                description=data.description,
                key=data.key,
                template_managed_backend=data.template_managed_backend,
                body_template=data.template_body,
                subject_template=data.template_subject,
                preheader_template=data.template_preheader,
                tenant=data.tenant,
                version=1,
                status=ManagedTemplateStatus.DRAFT.value,
            )
            if data.tags:
                template.tags.set(self._resolve_tags(data.tags, data.tenant))
        return self._serialize_template(template)

    def get_template(self, template_key: str, version: int | None = None):
        template: ManagedTemplate | None
        if version is not None:
            try:
                template = ManagedTemplate.objects.get(key=template_key, version=version)
            except ManagedTemplate.DoesNotExist as e:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' and version {version} does not exist."
                ) from e
        else:
            template = ManagedTemplate.objects.get_latest_version(template_key)

        if template is None:
            raise ManagedTemplateNotFoundError(
                f"Template with key '{template_key}' does not exist."
            )

        return self._serialize_template(template)

    def update_template(self, template_key: str, data: ManagedTemplateUpdateInput):
        """Insert the next version of a key, leaving the version it was copied from alone.

        A new row, never an edit. A version that is already ACTIVE keeps its content, its
        status and its history while its successor is drafted, so notifications recorded
        against it go on rendering exactly what they were sent with -- which is the whole
        reason templates are versioned rather than updated.

        The new version starts in DRAFT, whatever its predecessor was in, matching the seam's
        reference implementation: a copy nobody has reviewed should not inherit "published".
        Activate it when it is ready, and the previous version stays live until you retire it.

        The read and the insert share a transaction, and the previous version is locked for
        the length of it. Two concurrent updates therefore serialize; if one still races past
        the lock, the ``(key, version)`` unique constraint rejects the duplicate rather than
        letting two rows claim the same version number.
        """
        with transaction.atomic():
            previous = ManagedTemplate.objects.select_for_update().get_latest_version(template_key)

            if previous is None:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' does not exist."
                )

            # Resolved before the insert so an unusable tag text fails the whole update
            # rather than leaving a new version behind with the wrong labels.
            tags = (
                list(previous.tags.all())
                if data.tags is None
                else self._resolve_tags(data.tags, previous.tenant)
            )

            template = ManagedTemplate.objects.create(
                name=data.name or previous.name,
                description=data.description or previous.description,
                key=previous.key,
                template_managed_backend=previous.template_managed_backend,
                body_template=data.template_body or previous.body_template,
                subject_template=data.template_subject or previous.subject_template,
                preheader_template=data.template_preheader or previous.preheader_template,
                version=previous.version + 1,
                status=ManagedTemplateStatus.DRAFT.value,
                tenant=previous.tenant,
                # Left unattributed on purpose: ``ManagedTemplateUpdateInput`` carries no
                # user, and copying the previous version's author would credit this version
                # to someone who may have had nothing to do with it.
            )
            template.tags.set(tags)
        return self._serialize_template(template)

    def delete_template(self, template_key: str, version: int | None = None) -> None:
        template: ManagedTemplate | None
        with transaction.atomic():
            if version is not None:
                try:
                    template = ManagedTemplate.objects.select_for_update().get(
                        key=template_key, version=version
                    )
                except ManagedTemplate.DoesNotExist as e:
                    raise ManagedTemplateNotFoundError(
                        f"Template with key '{template_key}' and version {version} does not exist."
                    ) from e
            else:
                template = ManagedTemplate.objects.select_for_update().get_latest_version(
                    template_key
                )

            if not template:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' does not exist."
                )

            template.delete()

    def create_template_status_update(
        self,
        template_key: str,
        version: int,
        status: ManagedTemplateStatus,
        changed_by: str | None = None,
    ) -> None:
        user: AbstractUser | None = None
        if changed_by:
            try:
                user = User.objects.filter(pk=changed_by).first()
            except (ValueError, TypeError) as e:
                # A pk the backing column cannot even represent, e.g. "abc" for an integer pk.
                raise ManagedTemplateChangeUserNotFoundError(
                    f"`changed_by` user with id {changed_by} couldn't be found"
                ) from e
            if user is None:
                raise ManagedTemplateChangeUserNotFoundError(
                    f"`changed_by` user with id {changed_by} couldn't be found"
                )

        with transaction.atomic():
            try:
                template = ManagedTemplate.objects.select_for_update().get(
                    key=template_key, version=version
                )
            except ManagedTemplate.DoesNotExist as e:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' and version '{version}' does not exist."
                ) from e

            ManagedTemplateStatusRecord.objects.create(
                template=template,
                status=status.value,
                created_by_id=user.pk if user else None,
            )
            template.status = status.value
            template.save(update_fields=["status"])

    def get_template_status_history(self, template_key: str, version: int | None = None):
        """The status trail of one version, or of every version of the key, newest first.

        Omitting ``version`` reads the *key's* whole trail rather than its latest version's:
        each version carries its own records, so anything narrower would quietly drop the
        history of the versions still serving older notifications.
        """
        versions = ManagedTemplate.objects.filter(key=template_key)
        if version is not None:
            versions = versions.filter(version=version)

        if not versions.exists():
            if version is not None:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' and version '{version}' does not exist."
                )
            raise ManagedTemplateNotFoundError(
                f"Template with key '{template_key}' does not exist."
            )

        records = (
            ManagedTemplateStatusRecord.objects.filter(template__in=versions)
            .select_related("template", "created_by")
            # ``-id`` breaks a tie between records written in the same transaction, which
            # share a timestamp to the microsecond often enough to matter under a frozen clock.
            .order_by("-created", "-id")
        )
        return [
            ManagedTemplateStatusHistory(
                template_key=record.template.key,
                version=record.template.version,
                status=ManagedTemplateStatus(record.status),
                created=record.created,
                created_by=str(record.created_by.pk) if record.created_by else None,
                tenant=record.tenant,
            )
            for record in records
        ]

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def _tag_or_raise(self, slug: str) -> ManagedTemplateTag:
        """Fetch a tag by slug, accepting the text it was created from as well.

        Slugifying the argument is what makes ``get_tag("Black Friday")`` and
        ``get_tag("black-friday")`` the same lookup, which is the contract the seam documents.
        """
        tag = ManagedTemplateTag.objects.get_by_slug(slug)
        if tag is None:
            raise ManagedTemplateTagNotFoundError(f"Tag '{slug}' does not exist.")
        return tag

    def _slug_or_raise(self, text: str) -> str:
        slug = slugify_tag(text)
        if not slug:
            raise ManagedTemplateInvalidTagError(
                f"Tag text {text!r} has no characters that can be turned into a slug."
            )
        return slug

    def _is_taken(self, slug: str, excluding_pk: int | str | None = None) -> bool:
        taken = ManagedTemplateTag.objects.filter(slug=slug)
        if excluding_pk is not None:
            taken = taken.exclude(pk=excluding_pk)
        return taken.exists()

    def _resolve_tags(
        self, texts: Iterable[str], tenant: str | None = None
    ) -> list[ManagedTemplateTag]:
        """Model instances for these texts, creating the tags that do not exist yet.

        ``get_or_create`` on the slug rather than on the text, because the slug is the
        identity: "Black Friday" and "black friday" have to resolve to one row, and only the
        slug says so.
        """
        resolved: list[ManagedTemplateTag] = []
        seen: set[str] = set()
        for text in texts:
            slug = self._slug_or_raise(text)
            if slug in seen:
                continue
            seen.add(slug)
            # The defaults apply to a create only, so an existing tag keeps its own text,
            # status and tenant -- re-using an ARCHIVED tag does not quietly revive it.
            tag, _created = ManagedTemplateTag.objects.get_or_create(
                slug=slug,
                defaults={
                    "text": text,
                    "status": ManagedTemplateTagStatus.ACTIVE.value,
                    "tenant": tenant,
                },
            )
            resolved.append(tag)
        return resolved

    def get_or_create_tags(self, texts: Iterable[str], tenant: str | None = None):
        with transaction.atomic():
            return [self._serialize_tag(tag) for tag in self._resolve_tags(texts, tenant)]

    def create_tag(self, text: str, tenant: str | None = None):
        slug = self._slug_or_raise(text)
        if self._is_taken(slug):
            raise ManagedTemplateTagAlreadyExistsError(f"Tag '{slug}' already exists.")
        tag = ManagedTemplateTag.objects.create(
            text=text,
            slug=slug,
            status=ManagedTemplateTagStatus.ACTIVE.value,
            tenant=tenant,
        )
        return self._serialize_tag(tag)

    def get_tag(self, slug: str):
        return self._serialize_tag(self._tag_or_raise(slug))

    def update_tag(self, slug: str, text: str):
        """Rename a tag, giving it the slug its new text produces.

        The new slug is uniquified against the other tags, so renaming one tag onto another's
        text is allowed and yields ``-2``: two tags may legitimately read the same, and the
        slug is what tells them apart. A tag renamed to a variant of its own text (a case or
        accent change) keeps its slug rather than gaining a suffix -- it is excluded from its
        own uniqueness check.
        """
        with transaction.atomic():
            tag = ManagedTemplateTag.objects.select_for_update().get(pk=self._tag_or_raise(slug).pk)
            tag.text = text
            tag.slug = next_available_slug(
                self._slug_or_raise(text),
                lambda candidate: self._is_taken(candidate, excluding_pk=tag.pk),
            )
            tag.save(update_fields=["text", "slug", "updated"])
        return self._serialize_tag(tag)

    def set_tag_status(self, slug: str, status: ManagedTemplateTagStatus):
        tag = self._tag_or_raise(slug)
        tag.status = status.value
        tag.save(update_fields=["status", "updated"])
        return self._serialize_tag(tag)

    def delete_tag(self, slug: str) -> None:
        """Delete a tag. The M2M rows go with it, so every template loses the label.

        No template is deleted and none is otherwise touched -- ``delete()`` on one side of a
        ManyToMany removes the through rows and nothing else.
        """
        self._tag_or_raise(slug).delete()

    def _tag_queryset(
        self,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ) -> ManagedTemplateTagQuerySet:
        queryset = ManagedTemplateTag.objects.all()
        if status is not None:
            queryset = queryset.filter(status__in=[s.value for s in status])
        if search:
            queryset = queryset.search(search)
        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)
        return queryset

    def get_tags(
        self,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ):
        return [self._serialize_tag(tag) for tag in self._tag_queryset(status, search, tenant)]

    def get_paginated_tags(
        self,
        page: int,
        page_size: int,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ):
        """One page of tags. Not part of the seam -- an extra, like ``order_by`` on the
        filtered-template reads -- for callers paginating a long tag list.

        param page: int -- 1-indexed.
        param page_size: int
        return: list[ManagedTemplateTag]
        """
        queryset = self._tag_queryset(status, search, tenant)
        start = (page - 1) * page_size
        return [self._serialize_tag(tag) for tag in queryset[start : start + page_size]]

    def _template_row(self, template_key: str, version: int | None) -> ManagedTemplate:
        """The row behind a key/version pair, raising the seam's not-found error."""
        template: ManagedTemplate | None
        if version is not None:
            try:
                template = ManagedTemplate.objects.get(key=template_key, version=version)
            except ManagedTemplate.DoesNotExist as e:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' and version {version} does not exist."
                ) from e
        else:
            template = ManagedTemplate.objects.get_latest_version(template_key)

        if template is None:
            raise ManagedTemplateNotFoundError(
                f"Template with key '{template_key}' does not exist."
            )
        return template

    def get_template_tags(self, template_key: str, version: int | None = None):
        template = self._template_row(template_key, version)
        return [self._serialize_tag(tag) for tag in template.tags.all()]

    def set_template_tags(self, template_key: str, tags: Iterable[str], version: int | None = None):
        """Replace one version's tags in place -- no new version, no status change.

        Tags describe how a template is found rather than what it renders, so relabelling one
        should not fork a version and drop it back to DRAFT.
        """
        with transaction.atomic():
            template = self._template_row(template_key, version)
            template.tags.set(self._resolve_tags(tags, template.tenant))
        return self._serialize_template(template)

    def get_all_templates(self):
        return self._serialize_template_queryset(ManagedTemplate.objects.all())

    def get_templates_by_status(self, status: Iterable[ManagedTemplateStatus]):
        """
        Retrieves all templates from the backend with any of the given statuses.

        param status: Iterable[ManagedTemplateStatus]
        return: Iterable[ManagedTemplate]
        """
        return self._serialize_template_queryset(
            ManagedTemplate.objects.filter(status__in=[s.value for s in status])
        )

    def _string_lookup_q(self, model_field: str, spec: object) -> Q:
        if isinstance(spec, dict):
            lookup = spec.get("lookup", "exact")
            value = spec.get("value", "")
            case_sensitive = spec.get("case_sensitive", True)
        else:
            lookup = "exact"
            value = spec
            case_sensitive = True
        sensitive_suffix, insensitive_suffix = _STRING_LOOKUP_SUFFIX.get(
            lookup, _STRING_LOOKUP_SUFFIX["exact"]
        )
        suffix = sensitive_suffix if case_sensitive else insensitive_suffix
        return Q(**{f"{model_field}__{suffix}": value})

    def _range_q(self, model_field: str, spec: object) -> Q:
        query = Q()
        if not isinstance(spec, dict):
            return query
        lower = spec.get("from")
        upper = spec.get("to")
        if lower is not None:
            query &= Q(**{f"{model_field}__gte": lower})
        if upper is not None:
            query &= Q(**{f"{model_field}__lte": upper})
        return query

    def _field_leaf(self, field: str, value: object) -> tuple[Q, str | None]:
        """Positive Q for one field filter, plus the model field to OR ``__isnull`` on when
        this leaf is negated. Returns the match-nothing Q (and no null field) for an unknown
        field, mirroring the reference evaluator's "unknown field never matches"."""
        if field == _IS_ABSTRACT_FIELD:
            # A plain column, unlike the flag below it: ``ManagedTemplate.save`` derives it
            # from the row's own sources, so the query is a boolean lookup rather than
            # anything computed here. Not nullable, so no null field to fold in on negation.
            return Q(is_abstract=bool(value)), None
        if field == _MOST_RECENT_ACTIVE_VERSION_FIELD:
            # ``False`` is the complement, not "no filter": it asks for the rows the ``True``
            # filter leaves behind -- older versions, and every version of a key whose
            # versions are all retired. No null field, since nothing here reads a column that
            # could be NULL.
            current = most_recent_active_version_q()
            return (current if value else ~current), None
        if field in _TAG_FIELDS:
            # No null field to report: tag membership is a subquery on the through table, and
            # a template with no tags is simply absent from it. There is no NULL to fold in
            # under negation the way there is for a nullable column.
            slugs = normalize_tag_slugs(value)
            if field == "includes_all_tags":
                return all_tags_q(slugs), None
            return any_tags_q(slugs), None
        if isinstance(value, dict):
            # Dispatch on what the FIELD accepts, then check the value's shape. Testing shape
            # first let a lookup meant for one category be captured by another -- a plain
            # ``{"lookup": "exact", "value": "..."}`` on ``key`` used to satisfy the choice
            # guard and then fail on a _CHOICE_FIELDS lookup.
            if field in _RANGE_FIELDS and is_date_filter_lookup(value):
                model_field = _RANGE_FIELDS[field]
                return self._range_q(model_field, value), model_field
            if field in _STRING_LOOKUP_FIELDS and is_string_filter_lookup(value):
                model_field = _STRING_LOOKUP_FIELDS[field]
                return self._string_lookup_q(model_field, value), model_field
            if field in _INTEGER_FIELDS and is_integer_filter_lookup(value):
                model_field = _INTEGER_FIELDS[field]
                return Q(**{f"{model_field}__{value['lookup']}": value["value"]}), model_field
            if field in _CHOICE_FIELDS:
                model_field = _CHOICE_FIELDS[field]
                enum_cls = _CHOICE_FIELD_ENUMS[field]
                if is_choice_in_filter_lookup(value, enum_cls):
                    normalized = [member.value for member in value["value"]]
                    return Q(**{f"{model_field}__in": normalized}), model_field
                if is_choice_exact_filter_lookup(value, enum_cls):
                    return Q(**{model_field: value["value"].value}), model_field
            if field in _MEMBERSHIP_FIELDS:
                model_field = _MEMBERSHIP_FIELDS[field]
                if is_string_membership_in_lookup(value):
                    return Q(**{f"{model_field}__in": list(value["value"])}), model_field
                if is_string_membership_exact_lookup(value):
                    return Q(**{model_field: value["value"]}), model_field
            raise ManagedTemplateInvalidFilterError(f"field {field} filter is not valid")
        # A bare (non-lookup) value means equality on that field.
        bare_field = _FILTER_FIELD_TO_MODEL_FIELD.get(field)
        if bare_field is not None:
            return Q(**{bare_field: value.value if isinstance(value, Enum) else value}), bare_field
        return _MATCH_NOTHING, None

    def _translate_filter(self, filter: ManagedTemplateFilter, negated: bool = False) -> Q:  # noqa: A002
        """Translate a composable filter to a Django ``Q``, pushing negation to the leaves.

        Working in negation-normal form keeps NULL semantics correct: a positive leaf excludes
        NULL rows (a positive filter on a NULL value never matches), while a negated leaf ORs
        in ``field__isnull=True`` so NULL rows ARE included under ``not`` -- exactly what the
        reference in-memory evaluator does.
        """
        if "and" in filter:
            subs = [self._translate_filter(sub, negated) for sub in filter["and"]]  # type: ignore[typeddict-item]
            combiner = _or_all if negated else _and_all  # De Morgan under negation
            return combiner(subs)
        if "or" in filter:
            subs = [self._translate_filter(sub, negated) for sub in filter["or"]]  # type: ignore[typeddict-item]
            combiner = _and_all if negated else _or_all
            return combiner(subs)
        if "not" in filter:
            return self._translate_filter(filter["not"], not negated)  # type: ignore[typeddict-item]

        # Field filter. Empty ``{}`` matches everything (or nothing when negated). Multiple keys
        # are an implicit AND (OR under negation, by De Morgan).
        if not is_field_filter(filter):
            return _MATCH_NOTHING if not negated else Q()
        items = list(filter.items())
        if not items:
            return _MATCH_NOTHING if negated else Q()
        leaf_qs: list[Q] = []
        for key, value in items:
            positive_q, null_field = self._field_leaf(key, value)
            if not negated:
                leaf_qs.append(positive_q)
            else:
                negated_q = ~positive_q
                if null_field is not None:
                    negated_q |= Q(**{f"{null_field}__isnull": True})
                leaf_qs.append(negated_q)
        return _or_all(leaf_qs) if negated else _and_all(leaf_qs)

    def _filtered_queryset(
        self,
        filter: ManagedTemplateFilter,  # noqa: A002
        order_by: ManagedTemplateOrderBy | None = None,
    ) -> ManagedTemplateQuerySet:
        return self._ordered(
            ManagedTemplate.objects.filter(self._translate_filter(filter)), order_by
        )

    @staticmethod
    def _ordered(
        queryset: ManagedTemplateQuerySet,
        order_by: ManagedTemplateOrderBy | None,
    ) -> ManagedTemplateQuerySet:
        """Apply an order to the whole queryset, before any page is sliced off it.

        Ordering the queryset rather than the page is the seam's requirement and not a detail:
        a page ordered after it was chosen sorts rows *within* the page while the rows selected
        *for* it came back in the store's own order -- right on page 1, wrong on every page
        after it. Django composes ``order_by`` into the SQL, so the slice in
        ``_paginate_queryset`` is taken from the ordered set.

        Falls back to newest-first when no order was asked for, because an unordered offset
        page over a table with a row per version is free to repeat one row and skip another.
        """
        if order_by is None:
            return queryset.order_by("-created", "-id")

        attr = _ORDER_FIELD_TO_ATTR[order_by["field"]]
        prefix = "-" if order_by["direction"] == "desc" else ""
        # id tiebreaker in the SAME direction, so offset pagination over a non-unique key
        # does not drop or duplicate rows across pages.
        return queryset.order_by(f"{prefix}{attr}", f"{prefix}id")

    def get_filter_capabilities(self) -> dict[str, bool]:
        """
        Declare what this backend can be asked for.

        Every filter the vocabulary defines is translated into SQL by ``_translate_filter``,
        so nothing is declined there and the all-``True`` default in
        ``DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES`` is already correct for the filters.

        Ordering is different: those keys default to ``False``, so a backend that can sort has
        to say so. All six are declared because all six are indexed columns on
        ``ManagedTemplate`` -- see ``_ORDER_FIELD_TO_ATTR``, whose entries were each checked by
        running the sort rather than by reading the column definitions.

        ``status`` is included even though it is stored as a ``CharField``: its four values are
        lowercase ASCII, so a database collation orders them the same way the library's own
        ``sort_templates`` does. A store that mapped two statuses onto one sortable value would
        have to decline it -- a sort that cannot tell two of the four apart is worse than none.

        return: dict[str, bool]
        """
        return {order_by_capability_key(field): True for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS}

    def get_filtered_templates(
        self,
        filters: ManagedTemplateFilter,
        order_by: ManagedTemplateOrderBy | None = None,
    ):
        """
        Retrieves templates from the backend that match the given filters.

        param filters: ManagedTemplateFilter
        return: list[ManagedTemplate]
        """
        return self._serialize_template_queryset(self._filtered_queryset(filters, order_by))

    def get_paginated_templates(
        self,
        page: int,
        page_size: int,
        order_by: ManagedTemplateOrderBy | None = None,
    ):
        # Ordered even when the caller asks for none: a key has a row per version, so an
        # unordered offset page is free to return a row twice and skip another. The fallback
        # matches ``_filtered_queryset``'s, id-tiebroken for rows that share a timestamp.
        return self._serialize_template_queryset(
            self._paginate_queryset(
                self._ordered(ManagedTemplate.objects.all(), order_by),
                page=page,
                page_size=page_size,
            )
        )

    def get_paginated_filtered_templates(
        self,
        filters: ManagedTemplateFilter,
        page: int,
        page_size: int,
        order_by: ManagedTemplateOrderBy | None = None,
    ):
        return self._serialize_template_queryset(
            self._paginate_queryset(
                self._filtered_queryset(filters, order_by),
                page=page,
                page_size=page_size,
            )
        )
