from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from vintasend_managed_templates.constants import ManagedTemplateStatus


class ManagedTemplateStatusChoices(TextChoices):
    DRAFT = ManagedTemplateStatus.DRAFT.value, _("Draft")
    ACTIVE = ManagedTemplateStatus.ACTIVE.value, _("Active")
    INACTIVE = ManagedTemplateStatus.INACTIVE.value, _("Inactive")
    ARCHIVED = ManagedTemplateStatus.ARCHIVED.value, _("Archived")
