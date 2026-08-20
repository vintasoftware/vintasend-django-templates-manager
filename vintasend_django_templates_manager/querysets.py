from typing import TYPE_CHECKING

from django.db.models import QuerySet


if TYPE_CHECKING:
    from .models import ManagedTemplate


class ManagedTemplateQuerySet(QuerySet["ManagedTemplate", "ManagedTemplate"]):

    def get_latest_version(self, key: str) -> "ManagedTemplate | None":
        """
        Retrieves the latest version of a template based on its key.

        param key: str
        return: ManagedTemplate | None
        """
        return self.filter(key=key).order_by("-version").first()
