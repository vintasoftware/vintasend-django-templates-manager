from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from vintasend_managed_templates.constants import (
    ManagedTemplateStatus,
    ManagedTemplateTagStatus,
)


class ManagedTemplateStatusChoices(TextChoices):
    DRAFT = ManagedTemplateStatus.DRAFT.value, _("Draft")
    ACTIVE = ManagedTemplateStatus.ACTIVE.value, _("Active")
    INACTIVE = ManagedTemplateStatus.INACTIVE.value, _("Inactive")
    ARCHIVED = ManagedTemplateStatus.ARCHIVED.value, _("Archived")


class ManagedTemplateTagStatusChoices(TextChoices):
    ACTIVE = ManagedTemplateTagStatus.ACTIVE.value, _("Active")
    ARCHIVED = ManagedTemplateTagStatus.ARCHIVED.value, _("Archived")
