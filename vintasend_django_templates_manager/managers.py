from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from django.db.models import Manager, Prefetch

from .querysets import ManagedTemplateQuerySet


if TYPE_CHECKING:
    from .models import ManagedTemplate


class ManagedTemplateManager(Manager.from_queryset(ManagedTemplateQuerySet)):  # type: ignore[misc]
    """
    Custom manager for the ManagedTemplate model.
    """

    if TYPE_CHECKING:
        # ``from_queryset`` copies the queryset methods onto a class built at runtime, so no
        # static checker can see them: django-stubs types it as ``type[Manager[ManagedTemplate]]``
        # and the mypy plugin only recovers the rest for mypy. Declaring the transitions we use
        # keeps the custom queryset visible to plugin-less checkers (Pylance/pyright) too. Every
        # ``QuerySet`` method returns ``Self``, so only the manager -> queryset hop needs this.
        def get_queryset(self) -> ManagedTemplateQuerySet: ...
        def all(self) -> ManagedTemplateQuerySet: ...
        def filter(self, *args: Any, **kwargs: Any) -> ManagedTemplateQuerySet: ...
        def exclude(self, *args: Any, **kwargs: Any) -> ManagedTemplateQuerySet: ...
        def order_by(self, *field_names: Any) -> ManagedTemplateQuerySet: ...
        def prefetch_related(self, *lookups: "str | Prefetch[Any, Any, Any]") -> ManagedTemplateQuerySet: ...
        def select_related(self, *fields: str) -> ManagedTemplateQuerySet: ...
        def select_for_update(
            self,
            nowait: bool = ...,
            skip_locked: bool = ...,
            of: Sequence[str] = ...,
            no_key: bool = ...,
        ) -> ManagedTemplateQuerySet: ...
        def get_latest_version(self, key: str) -> "ManagedTemplate | None": ...
