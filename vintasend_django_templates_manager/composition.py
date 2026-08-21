"""Composition, as it applies to the Django rows.

The tag language, the resolution rules and the errors all belong to
``vintasend_managed_templates.composition``. What lives here is the two things a Django app
needs on top of it: a composer wired to this app's backend, and the derivation behind
``ManagedTemplate.is_abstract``.

That flag is a denormalization -- the source is the truth, and the column is a copy of what
the source says, kept so a picker can exclude bases with a WHERE clause instead of parsing
every row in the store. ``ManagedTemplate.save`` recomputes it, so every write path through
the ORM keeps it honest: the admin, ``DjangoTemplateManager``, a data migration, a shell
session. ``template_is_abstract`` is the same answer computed on demand, for checking a row
whose flag you do not trust -- one edited in memory, or written before the column existed.
"""

from typing import TYPE_CHECKING

from vintasend_managed_templates.composition import TEMPLATE_FIELDS, TemplateComposer
from vintasend_managed_templates.exceptions import ManagedTemplateCompositionError


if TYPE_CHECKING:
    from .models import ManagedTemplate


def backend_composer() -> TemplateComposer:
    """A composer that resolves ``managed_*`` references through this app's own backend.

    The import is function-local because the backend imports the models and the models import
    this module -- resolving it at call time keeps that from being a cycle at import time.

    return: TemplateComposer
    """
    from .django_templates_manager import DjangoTemplateManager

    return TemplateComposer.from_backend(DjangoTemplateManager())


def template_is_abstract(template: "ManagedTemplate", strict: bool = True) -> bool:
    """Whether a row is a base to build on rather than a template to send.

    Computed from the row's own three sources, with no query and no reference resolved: a
    template is abstract because of what it declares, not because of what it extends. This is
    the authority the ``is_abstract`` column is a copy of.

    param template: ManagedTemplate -- the Django model, not the library dataclass.
    param strict: bool -- True raises on a malformed composition tag, which is what a check
        run to *report* a problem wants. False reads a template nobody can parse as concrete,
        which is what ``save()`` and the changelist want: neither is a place to raise, and a
        template that cannot be parsed cannot be extended either.
    return: bool
    raises ManagedTemplateCompositionSyntaxError: when ``strict`` and a tag is malformed.
    """
    composer = TemplateComposer()

    try:
        return any(
            composer.source_is_abstract(getattr(template, field) or "", field=field)
            for field in TEMPLATE_FIELDS
        )
    except ManagedTemplateCompositionError:
        if strict:
            raise
        return False
