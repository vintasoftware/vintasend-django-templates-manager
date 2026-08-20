from django.contrib.auth import get_user_model

import pytest
from vintasend_managed_templates.constants import ManagedTemplateStatus

from vintasend_django_templates_manager.django_templates_manager import DjangoTemplateManager
from vintasend_django_templates_manager.models import ManagedTemplate


@pytest.fixture
def manager() -> DjangoTemplateManager:
    return DjangoTemplateManager()


@pytest.fixture
def editor(db):
    return get_user_model().objects.create_user(
        username="editor", email="editor@example.com", password="pw"  # noqa: S106
    )


@pytest.fixture
def make_template(db):
    """Build rows directly, bypassing ``create_template`` (see test_backend_crud)."""

    def _make(**overrides) -> ManagedTemplate:
        defaults = {
            "name": "Welcome email",
            "description": "Sent on signup",
            "key": "welcome",
            "template_managed_backend": "django",
            "body_template": "Hello {{ name }}",
            "subject_template": "Welcome aboard",
            "preheader_template": "Glad you are here",
            "version": 1,
            "status": ManagedTemplateStatus.DRAFT.value,
        }
        return ManagedTemplate.objects.create(**{**defaults, **overrides})

    return _make


@pytest.fixture
def templates(make_template) -> dict[str, ManagedTemplate]:
    """Three templates that differ in the fields the filter suite discriminates on.

    ``beta`` deliberately has a NULL ``subject_template`` so negated filters can be checked
    against SQL's three-valued logic.
    """
    return {
        "alpha": make_template(
            key="alpha",
            name="name-alpha",
            subject_template="shared subject",
            version=1,
            status=ManagedTemplateStatus.DRAFT.value,
        ),
        "beta": make_template(
            key="beta",
            name="name-beta",
            subject_template=None,
            version=2,
            status=ManagedTemplateStatus.ACTIVE.value,
            tenant="acme",
        ),
        "gamma": make_template(
            key="gamma",
            name="name-gamma",
            subject_template="shared subject",
            version=3,
            status=ManagedTemplateStatus.ARCHIVED.value,
        ),
    }


def keys(results) -> list[str]:
    """Template keys from an iterable of serialized templates, in order."""
    return [template.key for template in results]


def string_lookup(value: str, lookup: str = "exact", case_sensitive: bool = True) -> dict:
    """A ``StringFilterLookup``. Bare-string filters are broken (see test_backend_filters),
    so the composition tests build their leaves with this instead."""
    return {"lookup": lookup, "value": value, "case_sensitive": case_sensitive}
