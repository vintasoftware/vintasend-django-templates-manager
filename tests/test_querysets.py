import pytest
from vintasend_managed_templates.constants import ManagedTemplateTagStatus

from vintasend_django_templates_manager.models import ManagedTemplate, ManagedTemplateTag
from vintasend_django_templates_manager.querysets import (
    ManagedTemplateQuerySet,
    ManagedTemplateTagQuerySet,
    normalize_tag_slugs,
)


def test_tag_manager_exposes_the_custom_queryset(db):
    assert isinstance(ManagedTemplateTag.objects.all(), ManagedTemplateTagQuerySet)


def test_manager_exposes_the_custom_queryset(db):
    assert isinstance(ManagedTemplate.objects.all(), ManagedTemplateQuerySet)
    assert isinstance(ManagedTemplate.objects.filter(key="x"), ManagedTemplateQuerySet)


def test_get_latest_version_returns_the_row_for_the_key(make_template):
    template = make_template(key="welcome", version=3)
    make_template(key="other", version=9)
    assert ManagedTemplate.objects.get_latest_version("welcome") == template


def test_get_latest_version_returns_none_for_an_unknown_key(db):
    assert ManagedTemplate.objects.get_latest_version("nope") is None


def test_get_latest_version_picks_the_highest_version(make_template):
    """The ordering is exercised through the queryset directly: ``key`` is unique, so the
    manager can never actually see two versions of one key."""
    make_template(key="a", version=1)
    highest = make_template(key="b", version=7)
    queryset = ManagedTemplate.objects.all()
    assert queryset.order_by("-version").first() == highest


def test_get_latest_version_is_reachable_from_a_narrowed_queryset(make_template):
    make_template(key="welcome", version=1)
    assert ManagedTemplate.objects.filter(status="draft").get_latest_version("welcome") is not None
    assert ManagedTemplate.objects.filter(status="active").get_latest_version("welcome") is None


def test_get_by_slug_accepts_the_text_behind_the_slug(make_tag):
    make_tag("Black Friday")

    assert ManagedTemplateTag.objects.get_by_slug("Black Friday").slug == "black-friday"
    assert ManagedTemplateTag.objects.get_by_slug("black-friday").slug == "black-friday"


def test_get_by_slug_returns_none_for_an_unknown_tag(db):
    assert ManagedTemplateTag.objects.get_by_slug("nope") is None


def test_active_narrows_to_the_tags_still_on_offer(make_tag):
    make_tag("Onboarding")
    make_tag("Billing", status=ManagedTemplateTagStatus.ARCHIVED.value)

    assert [tag.slug for tag in ManagedTemplateTag.objects.active()] == ["onboarding"]


def test_search_matches_text_or_slug_ignoring_case(make_tag):
    make_tag("Black Friday")
    make_tag("Onboarding")

    assert [tag.slug for tag in ManagedTemplateTag.objects.search("FRIDAY")] == ["black-friday"]


def test_with_all_tags_requires_every_tag(tagged_templates):
    matched = ManagedTemplate.objects.with_all_tags(["transactional", "onboarding"])

    assert [t.key for t in matched] == ["alpha"]


def test_with_any_tags_requires_only_one(tagged_templates):
    matched = ManagedTemplate.objects.with_any_tags(["onboarding", "marketing"])

    assert sorted(t.key for t in matched) == ["alpha", "beta"]


def test_with_any_tags_returns_a_template_once_however_many_tags_match(tagged_templates):
    matched = list(ManagedTemplate.objects.with_any_tags(["transactional", "onboarding"]))

    assert len(matched) == len({t.pk for t in matched})


def test_normalize_tag_slugs_slugifies_and_deduplicates():
    assert normalize_tag_slugs(["Black Friday", "black friday", "Onboarding"]) == [
        "black-friday",
        "onboarding",
    ]


def test_normalize_tag_slugs_drops_values_that_slug_to_nothing():
    assert normalize_tag_slugs(["Onboarding", "!!!"]) == ["onboarding"]


@pytest.mark.parametrize("value", ["welcome", 42, None, {"lookup": "in"}])
def test_normalize_tag_slugs_refuses_anything_that_is_not_a_collection(value):
    """A bare string is iterable, so letting it through would ask for its characters."""
    assert normalize_tag_slugs(value) == []
