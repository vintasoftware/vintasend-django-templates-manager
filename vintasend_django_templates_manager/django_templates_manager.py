import functools
from collections.abc import Iterable
from enum import Enum

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.db import transaction
from django.db.models import Prefetch, Q

from vintasend_managed_templates.base_template_manager_backend import BaseTemplateManagerBackend
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate as ManagedTemplateDataclass,
)
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateChangeUserNotFoundError,
    ManagedTemplateInvalidFilterError,
    ManagedTemplateNotFoundError,
)
from vintasend_managed_templates.filters import (
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
)

from .models import ManagedTemplate, ManagedTemplateStatusRecord
from .querysets import ManagedTemplateQuerySet


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
_INTEGER_FIELDS: dict[str, str] = {
    "version": "version"
}
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
# order_by field name -> model field. ``created_at`` maps to ``created`` and ``updated_at`` to
# ``modified``, matching the model's ``AutoCreatedField`` / ``AutoLastModifiedField``.
_ORDER_FIELD_TO_ATTR: dict[str, str] = {
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
        )

    def _serialize_template_queryset(self, queryset: ManagedTemplateQuerySet):
        for template in queryset:
            yield self._serialize_template(template)

    def _paginate_queryset(
        self, queryset: ManagedTemplateQuerySet, page: int, page_size: int
    ) -> ManagedTemplateQuerySet:
        return queryset[((page - 1) * page_size) : ((page - 1) * page_size) + page_size]

    def create_template(self, data: ManagedTemplateCreateInput):
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
            raise ManagedTemplateNotFoundError(f"Template with key '{template_key}' does not exist.")

        return self._serialize_template(template)

    def update_template(self, template_key: str, data: ManagedTemplateUpdateInput):
        template = ManagedTemplate.objects.select_for_update().get_latest_version(template_key)

        if template is None:
            raise ManagedTemplateNotFoundError(f"Template with key '{template_key}' does not exist.")

        template.version += 1
        template.name = data.name or template.name
        template.description = data.description or template.description
        template.body_template = data.template_body or template.body_template
        template.subject_template = data.template_subject or template.subject_template
        template.preheader_template = data.template_preheader or template.preheader_template
        template.save()
        return self._serialize_template(template)

    def delete_template(self, template_key: str, version: int | None = None) -> None:
        template: ManagedTemplate | None
        if version is not None:
            try:
                template = ManagedTemplate.objects.select_for_update().get(key=template_key, version=version)
            except ManagedTemplate.DoesNotExist as e:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' and version {version} does not exist."
                ) from e
        else:
            template = ManagedTemplate.objects.select_for_update().get_latest_version(template_key)

        if not template:
            raise ManagedTemplateNotFoundError(f"Template with key '{template_key}' does not exist.")

        template.delete()

    def create_template_status_update(
        self,
        template_key: str,
        version: int,
        status: ManagedTemplateStatus,
        changed_by: str | None = None
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

        try:
            template = ManagedTemplate.objects.select_for_update().get(key=template_key, version=version)
        except ManagedTemplate.DoesNotExist as e:
            raise ManagedTemplateNotFoundError(
                f"Template with key '{template_key}' and version '{version}' does not exist."
            ) from e

        with transaction.atomic():
            ManagedTemplateStatusRecord.objects.create(
                template=template,
                status=status.value,
                created_by_id=user.pk if user else None,
            )
            template.status = status.value
            template.save(update_fields=["status"])


    def get_template_status_history(self, template_key: str, version: int | None = None):
        template: ManagedTemplate | None

        if version is not None:
            try:
                template = ManagedTemplate.objects.prefetch_related(
                    Prefetch(
                        "history",
                        ManagedTemplateStatusRecord.objects.all().order_by("-created")
                    )
                ).get(key=template_key, version=version)
            except ManagedTemplate.DoesNotExist as e:
                raise ManagedTemplateNotFoundError(
                    f"Template with key '{template_key}' and version '{version}' does not exist."
                ) from e
        else:
            template = ManagedTemplate.objects.get_latest_version(template_key)

        if not template:
            raise ManagedTemplateNotFoundError(f"Template with key '{template_key}' does not exist.")

        status_history = template.history.all()
        return [
            ManagedTemplateStatusHistory(
                template_key=template.key,
                version=template.version,
                status=ManagedTemplateStatus(record.status),
                created=record.created,
                created_by=str(record.created_by.pk) if record.created_by else None,
                tenant=record.tenant,
            )
            for record in status_history
        ]

    def get_all_templates(self):
        return self._serialize_template_queryset(
            ManagedTemplate.objects.all()
        )

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
        queryset = ManagedTemplate.objects.filter(self._translate_filter(filter))
        if order_by is None:
            order_fields = ["-created", "-id"]
        else:
            attr = _ORDER_FIELD_TO_ATTR[order_by["field"]]
            prefix = "-" if order_by["direction"] == "desc" else ""
            # id tiebreaker in the SAME direction, so offset pagination over a non-unique key
            # does not drop or duplicate rows across pages.
            order_fields = [f"{prefix}{attr}", f"{prefix}id"]
        return queryset.order_by(*order_fields)

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
        return self._serialize_template_queryset(
            self._filtered_queryset(filters, order_by)
        )

    def get_paginated_templates(self, page: int, page_size: int):
        return self._serialize_template_queryset(
            self._paginate_queryset(
                ManagedTemplate.objects.all(),
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
