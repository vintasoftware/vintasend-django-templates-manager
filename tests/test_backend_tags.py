"""The tag half of the storage seam, against a real database.

These cover what only a database can get wrong: the unique constraint on ``slug``, the
through-table writes behind tagging, and the two membership filters -- which are subqueries
rather than joins, so a template carrying two of the tags asked for still comes back once.
"""

import pytest
from vintasend_managed_templates.constants import ManagedTemplateTagStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplateCreateInput,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateInvalidTagError,
    ManagedTemplateNotFoundError,
    ManagedTemplateTagAlreadyExistsError,
    ManagedTemplateTagNotFoundError,
)

from vintasend_django_templates_manager.models import ManagedTemplate, ManagedTemplateTag

from .conftest import keys


pytestmark = pytest.mark.django_db


def slugs(tags) -> list[str]:
    return [tag.slug for tag in tags]


def create_input(key: str = "welcome", tags: list[str] | None = None, tenant=None):
    return ManagedTemplateCreateInput(
        name="Welcome email",
        description="Sent on signup",
        key=key,
        template_managed_backend="django",
        template_body="Hello",
        template_subject="Welcome",
        template_preheader=None,
        tenant=tenant,
        tags=tags,
    )


def update_input(tags: list[str] | None = None, name: str | None = None):
    return ManagedTemplateUpdateInput(
        name=name,
        description=None,
        template_body=None,
        template_subject=None,
        template_preheader=None,
        tags=tags,
    )


# ----------------------------------------------------------------------
# Tag CRUD
# ----------------------------------------------------------------------


def test_creating_a_tag_stores_the_text_and_its_slug(manager):
    tag = manager.create_tag("Black Friday")

    assert (tag.text, tag.slug) == ("Black Friday", "black-friday")
    assert tag.status is ManagedTemplateTagStatus.ACTIVE


def test_a_created_tag_can_carry_a_tenant(manager):
    assert manager.create_tag("Onboarding", tenant="acme").tenant == "acme"


def test_creating_a_tag_whose_slug_is_taken_is_an_error(manager):
    manager.create_tag("Black Friday")

    with pytest.raises(ManagedTemplateTagAlreadyExistsError, match="black-friday"):
        manager.create_tag("black friday")


@pytest.mark.parametrize("text", ["", "   ", "!!!"])
def test_creating_a_tag_with_nothing_sluggable_is_rejected(manager, text):
    with pytest.raises(ManagedTemplateInvalidTagError):
        manager.create_tag(text)

    assert not ManagedTemplateTag.objects.exists()


def test_the_slug_column_is_unique(make_tag):
    make_tag("Black Friday")

    with pytest.raises(Exception, match=r"(?i)unique"):
        make_tag("Something else", slug="black-friday")


def test_a_tag_is_fetched_by_slug_or_by_its_text(manager, make_tag):
    make_tag("Black Friday")

    assert manager.get_tag("black-friday").text == "Black Friday"
    assert manager.get_tag("Black Friday").text == "Black Friday"


def test_fetching_an_unknown_tag_raises(manager):
    with pytest.raises(ManagedTemplateTagNotFoundError, match="nope"):
        manager.get_tag("nope")


def test_renaming_a_tag_regenerates_its_slug(manager, make_tag):
    make_tag("Blak Friday")

    renamed = manager.update_tag("blak-friday", "Black Friday")

    assert (renamed.text, renamed.slug) == ("Black Friday", "black-friday")
    assert not ManagedTemplateTag.objects.filter(slug="blak-friday").exists()


def test_renaming_keeps_the_row_and_its_templates(manager, make_tag, make_template):
    tag = make_tag("Blak Friday")
    template = make_template()
    template.tags.set([tag])

    renamed = manager.update_tag("blak-friday", "Black Friday")

    assert renamed.id == tag.pk
    assert slugs(manager.get_template_tags("welcome")) == ["black-friday"]


def test_renaming_onto_a_taken_slug_gets_a_numeric_suffix(manager, make_tag):
    """Two tags may legitimately read the same; the slug is what tells them apart."""
    make_tag("Black Friday")
    make_tag("Cyber Monday")

    renamed = manager.update_tag("cyber-monday", "Black Friday")

    assert (renamed.text, renamed.slug) == ("Black Friday", "black-friday-2")


def test_renaming_a_tag_to_a_variant_of_its_own_text_keeps_its_slug(manager, make_tag):
    """The tag is excluded from its own uniqueness check, so a case change adds no suffix."""
    make_tag("Black Friday")

    assert manager.update_tag("black-friday", "BLACK FRIDAY").slug == "black-friday"


def test_renaming_to_text_with_nothing_sluggable_is_rejected(manager, make_tag):
    make_tag("Black Friday")

    with pytest.raises(ManagedTemplateInvalidTagError):
        manager.update_tag("black-friday", "!!!")

    assert ManagedTemplateTag.objects.get().text == "Black Friday"


def test_renaming_an_unknown_tag_raises(manager):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        manager.update_tag("nope", "Whatever")


def test_archiving_a_tag_changes_its_status_only(manager, make_tag, make_template):
    tag = make_tag("Onboarding")
    template = make_template()
    template.tags.set([tag])

    archived = manager.set_tag_status("onboarding", ManagedTemplateTagStatus.ARCHIVED)

    assert archived.status is ManagedTemplateTagStatus.ARCHIVED
    assert slugs(manager.get_template_tags("welcome")) == ["onboarding"]


def test_an_archived_tag_can_be_restored(manager, make_tag):
    make_tag("Onboarding", status=ManagedTemplateTagStatus.ARCHIVED.value)

    restored = manager.set_tag_status("onboarding", ManagedTemplateTagStatus.ACTIVE)

    assert restored.status is ManagedTemplateTagStatus.ACTIVE


def test_setting_the_status_of_an_unknown_tag_raises(manager):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        manager.set_tag_status("nope", ManagedTemplateTagStatus.ARCHIVED)


def test_deleting_a_tag_unlinks_it_from_every_template(manager, make_tag, make_template):
    tag = make_tag("Onboarding")
    other = make_tag("Billing")
    template = make_template()
    template.tags.set([tag, other])

    manager.delete_tag("onboarding")

    assert slugs(manager.get_template_tags("welcome")) == ["billing"]
    assert not ManagedTemplateTag.objects.filter(slug="onboarding").exists()


def test_deleting_a_tag_leaves_the_templates_themselves_alone(manager, make_tag, make_template):
    tag = make_tag("Onboarding")
    template = make_template()
    template.tags.set([tag])

    manager.delete_tag("onboarding")

    assert ManagedTemplate.objects.filter(pk=template.pk).exists()


def test_deleting_an_unknown_tag_raises(manager):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        manager.delete_tag("nope")


# ----------------------------------------------------------------------
# get_or_create
# ----------------------------------------------------------------------


def test_get_or_create_creates_the_tags_that_do_not_exist(manager):
    resolved = manager.get_or_create_tags(["Black Friday", "Onboarding"])

    assert slugs(resolved) == ["black-friday", "onboarding"]
    assert ManagedTemplateTag.objects.count() == 2


def test_get_or_create_resolves_an_existing_tag_rather_than_duplicating_it(manager, make_tag):
    existing = make_tag("Black Friday")

    resolved = manager.get_or_create_tags(["black friday"])

    assert [tag.id for tag in resolved] == [existing.pk]
    assert ManagedTemplateTag.objects.count() == 1


def test_get_or_create_leaves_an_existing_tags_text_and_status_alone(manager, make_tag):
    """Re-using an archived tag must not quietly bring it back on offer."""
    make_tag("Black Friday", status=ManagedTemplateTagStatus.ARCHIVED.value)

    resolved = manager.get_or_create_tags(["BLACK FRIDAY"])

    assert resolved[0].text == "Black Friday"
    assert resolved[0].status is ManagedTemplateTagStatus.ARCHIVED


def test_get_or_create_collapses_repeats_within_one_call(manager):
    resolved = manager.get_or_create_tags(["Onboarding", "onboarding", "ONBOARDING"])

    assert slugs(resolved) == ["onboarding"]
    assert ManagedTemplateTag.objects.count() == 1


def test_get_or_create_rejects_a_text_with_nothing_sluggable(manager):
    with pytest.raises(ManagedTemplateInvalidTagError):
        manager.get_or_create_tags(["Fine", "!!!"])


# ----------------------------------------------------------------------
# Listing tags
# ----------------------------------------------------------------------


def test_tags_are_listed_in_text_order(manager, make_tag):
    make_tag("Onboarding")
    make_tag("Billing")

    assert slugs(manager.get_tags()) == ["billing", "onboarding"]


def test_tags_can_be_narrowed_by_status(manager, make_tag):
    make_tag("Onboarding")
    make_tag("Billing", status=ManagedTemplateTagStatus.ARCHIVED.value)

    listed = manager.get_tags(status=[ManagedTemplateTagStatus.ACTIVE])

    assert slugs(listed) == ["onboarding"]


@pytest.mark.parametrize("term", ["friday", "FRIDAY", "black-fri"])
def test_tag_search_matches_text_or_slug_ignoring_case(manager, make_tag, term):
    make_tag("Black Friday")
    make_tag("Onboarding")

    assert slugs(manager.get_tags(search=term)) == ["black-friday"]


def test_tags_can_be_narrowed_by_tenant(manager, make_tag):
    make_tag("Onboarding", tenant="acme")
    make_tag("Billing", tenant="other")

    assert slugs(manager.get_tags(tenant="acme")) == ["onboarding"]


def test_tags_can_be_paginated(manager, make_tag):
    make_tag("Alpha")
    make_tag("Beta")
    make_tag("Gamma")

    assert slugs(manager.get_paginated_tags(page=2, page_size=2)) == ["gamma"]


# ----------------------------------------------------------------------
# Tagging templates
# ----------------------------------------------------------------------


def test_creating_a_template_with_tags_creates_and_attaches_them(manager):
    template = manager.create_template(create_input(tags=["Transactional", "Onboarding"]))

    assert slugs(template.tags) == ["onboarding", "transactional"]


def test_a_template_created_without_tags_has_none(manager):
    assert manager.create_template(create_input()).tags == []


def test_a_rejected_tag_rolls_the_template_back(manager):
    """The M2M write is a second statement, so it shares a transaction with the insert."""
    with pytest.raises(ManagedTemplateInvalidTagError):
        manager.create_template(create_input(tags=["!!!"]))

    assert not ManagedTemplate.objects.exists()


def test_a_new_version_keeps_the_tags_when_the_input_carries_none(manager):
    manager.create_template(create_input(tags=["Onboarding"]))

    updated = manager.update_template("welcome", update_input(name="Renamed"))

    assert slugs(updated.tags) == ["onboarding"]


def test_a_new_version_can_replace_the_tags(manager):
    manager.create_template(create_input(tags=["Onboarding"]))

    updated = manager.update_template("welcome", update_input(tags=["Billing"]))

    assert slugs(updated.tags) == ["billing"]


def test_an_empty_tag_list_on_an_update_clears_the_tags(manager):
    manager.create_template(create_input(tags=["Onboarding"]))

    assert manager.update_template("welcome", update_input(tags=[])).tags == []


def test_set_template_tags_replaces_the_whole_set(manager):
    manager.create_template(create_input(tags=["Onboarding", "Billing"]))

    updated = manager.set_template_tags("welcome", ["Marketing"])

    assert slugs(updated.tags) == ["marketing"]


def test_set_template_tags_does_not_bump_the_version_or_the_status(manager):
    created = manager.create_template(create_input(tags=["Onboarding"]))

    updated = manager.set_template_tags("welcome", ["Marketing"])

    assert (updated.version, updated.status) == (created.version, created.status)


def test_set_template_tags_can_name_the_version_explicitly(manager):
    """``key`` is unique on this model, so a key is one row whose ``version`` is bumped in
    place -- naming the version is a check that the caller means the row that is there."""
    manager.create_template(create_input())
    manager.update_template("welcome", update_input(name="v2"))

    updated = manager.set_template_tags("welcome", ["Onboarding"], version=2)

    assert slugs(updated.tags) == ["onboarding"]


def test_set_template_tags_on_a_version_that_is_not_there_raises(manager):
    manager.create_template(create_input())

    with pytest.raises(ManagedTemplateNotFoundError):
        manager.set_template_tags("welcome", ["Onboarding"], version=99)


def test_set_template_tags_on_an_unknown_key_raises(manager):
    with pytest.raises(ManagedTemplateNotFoundError):
        manager.set_template_tags("nope", ["Onboarding"])


def test_get_template_tags_on_an_unknown_version_raises(manager, make_template):
    make_template()

    with pytest.raises(ManagedTemplateNotFoundError):
        manager.get_template_tags("welcome", version=99)


def test_tags_come_back_on_every_template_read(manager, make_template, make_tag):
    template = make_template()
    template.tags.set([make_tag("Onboarding")])

    assert slugs(manager.get_template("welcome").tags) == ["onboarding"]
    assert slugs(next(iter(manager.get_all_templates())).tags) == ["onboarding"]


def test_listing_templates_does_not_query_tags_per_row(
    manager, make_template, make_tag, django_assert_num_queries
):
    """Without the prefetch this is one query per template, which a long page turns into a
    query storm nobody notices until the list is slow."""
    tag = make_tag("Onboarding")
    for index in range(5):
        template = make_template(key=f"key-{index}")
        template.tags.set([tag])

    # One for the templates, one for the prefetched tags.
    with django_assert_num_queries(2):
        list(manager.get_all_templates())


# ----------------------------------------------------------------------
# Tag filters
# ----------------------------------------------------------------------


def test_includes_all_tags_matches_a_template_carrying_every_one(manager, tagged_templates):
    results = manager.get_filtered_templates({"includes_all_tags": ["transactional", "onboarding"]})

    assert keys(results) == ["alpha"]


def test_includes_all_tags_excludes_a_template_missing_one(manager, tagged_templates):
    results = manager.get_filtered_templates({"includes_all_tags": ["transactional", "marketing"]})

    assert keys(results) == ["beta"]


def test_includes_any_of_tags_matches_a_template_carrying_at_least_one(manager, tagged_templates):
    results = manager.get_filtered_templates({"includes_any_of_tags": ["onboarding", "marketing"]})

    assert sorted(keys(results)) == ["alpha", "beta"]


def test_a_template_carrying_several_of_the_tags_appears_once(manager, tagged_templates):
    """A join would return it twice, and the duplicate would eat a slot out of a page."""
    results = list(
        manager.get_filtered_templates({"includes_any_of_tags": ["transactional", "onboarding"]})
    )

    assert sorted(keys(results)) == ["alpha", "beta"]
    assert len(results) == len(set(keys(results)))


def test_a_tag_filter_may_name_a_tag_by_its_text(manager, tagged_templates):
    results = manager.get_filtered_templates({"includes_any_of_tags": ["Transactional"]})

    assert sorted(keys(results)) == ["alpha", "beta"]


def test_an_unknown_tag_matches_nothing(manager, tagged_templates):
    assert list(manager.get_filtered_templates({"includes_any_of_tags": ["nope"]})) == []


def test_matching_all_of_no_tags_constrains_nothing(manager, tagged_templates):
    results = manager.get_filtered_templates({"includes_all_tags": []})

    assert sorted(keys(results)) == ["alpha", "beta", "gamma"]


def test_matching_any_of_no_tags_matches_nothing(manager, tagged_templates):
    assert list(manager.get_filtered_templates({"includes_any_of_tags": []})) == []


def test_a_tag_filter_combines_with_the_other_fields(manager, tagged_templates):
    results = manager.get_filtered_templates(
        {"includes_any_of_tags": ["transactional"], "key": {"lookup": "in", "value": ["beta"]}}
    )

    assert keys(results) == ["beta"]


def test_a_negated_tag_filter_returns_the_untagged_templates_too(manager, tagged_templates):
    """``gamma`` has no tags at all, so it has to survive the negation."""
    results = manager.get_filtered_templates({"not": {"includes_any_of_tags": ["transactional"]}})

    assert keys(results) == ["gamma"]


def test_a_negated_all_tags_filter_keeps_partial_matches(manager, tagged_templates):
    results = manager.get_filtered_templates(
        {"not": {"includes_all_tags": ["transactional", "onboarding"]}}
    )

    assert sorted(keys(results)) == ["beta", "gamma"]


def test_tag_filters_compose_under_and_or(manager, tagged_templates):
    results = manager.get_filtered_templates(
        {
            "or": [
                {"includes_all_tags": ["transactional", "onboarding"]},
                {"includes_any_of_tags": ["marketing"]},
            ]
        }
    )

    assert sorted(keys(results)) == ["alpha", "beta"]


def test_a_tag_filter_paginates_without_duplicates(manager, tagged_templates):
    tag_filter = {"includes_any_of_tags": ["transactional", "onboarding", "marketing"]}
    first = list(manager.get_paginated_filtered_templates(tag_filter, page=1, page_size=1))
    second = list(manager.get_paginated_filtered_templates(tag_filter, page=2, page_size=1))

    assert len(first) == len(second) == 1
    assert keys(first) != keys(second)


def test_filtering_by_an_archived_tag_still_finds_its_templates(manager, tagged_templates):
    manager.set_tag_status("transactional", ManagedTemplateTagStatus.ARCHIVED)

    results = manager.get_filtered_templates({"includes_any_of_tags": ["transactional"]})

    assert sorted(keys(results)) == ["alpha", "beta"]
