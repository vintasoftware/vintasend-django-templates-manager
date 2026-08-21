import datetime

from django.db.models import Q

import pytest
from freezegun import freeze_time
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import ManagedTemplateInvalidFilterError

from .conftest import keys, string_lookup


class TestStringLookups:
    def test_exact_is_case_sensitive_by_default(self, manager, templates):
        assert keys(manager.get_filtered_templates({"name": string_lookup("name-alpha")})) == [
            "alpha"
        ]
        assert manager.get_filtered_templates({"name": string_lookup("NAME-ALPHA")}) is not None
        assert keys(manager.get_filtered_templates({"name": string_lookup("NAME-ALPHA")})) == []

    def test_exact_can_ignore_case(self, manager, templates):
        results = manager.get_filtered_templates(
            {"name": string_lookup("NAME-ALPHA", case_sensitive=False)}
        )
        assert keys(results) == ["alpha"]

    def test_starts_with(self, manager, templates):
        results = manager.get_filtered_templates({"name": string_lookup("name-", "starts_with")})
        assert sorted(keys(results)) == ["alpha", "beta", "gamma"]

    def test_starts_with_ignoring_case(self, manager, templates):
        results = manager.get_filtered_templates(
            {"name": string_lookup("NAME-A", "starts_with", case_sensitive=False)}
        )
        assert keys(results) == ["alpha"]

    def test_ends_with(self, manager, templates):
        results = manager.get_filtered_templates({"name": string_lookup("-beta", "ends_with")})
        assert keys(results) == ["beta"]

    def test_ends_with_ignoring_case(self, manager, templates):
        results = manager.get_filtered_templates(
            {"name": string_lookup("-BETA", "ends_with", case_sensitive=False)}
        )
        assert keys(results) == ["beta"]

    def test_includes(self, manager, templates):
        results = manager.get_filtered_templates({"name": string_lookup("gamm", "includes")})
        assert keys(results) == ["gamma"]

    def test_includes_ignoring_case(self, manager, templates):
        results = manager.get_filtered_templates(
            {"name": string_lookup("GAMM", "includes", case_sensitive=False)}
        )
        assert keys(results) == ["gamma"]

    def test_applies_to_description(self, manager, templates):
        results = manager.get_filtered_templates({"description": string_lookup("Sent on signup")})
        assert sorted(keys(results)) == ["alpha", "beta", "gamma"]

    def test_applies_to_body_and_subject(self, manager, templates):
        assert sorted(
            keys(
                manager.get_filtered_templates(
                    {"body_template": string_lookup("Hello", "includes")}
                )
            )
        ) == ["alpha", "beta", "gamma"]
        assert sorted(
            keys(
                manager.get_filtered_templates(
                    {"subject_template": string_lookup("shared subject")}
                )
            )
        ) == ["alpha", "gamma"]

    def test_string_lookup_works_on_key(self, manager, templates):
        assert keys(manager.get_filtered_templates({"key": string_lookup("alpha")})) == ["alpha"]

    def test_string_lookup_works_on_backend(self, manager, templates):
        results = manager.get_filtered_templates(
            {"template_managed_backend": string_lookup("django")}
        )
        assert sorted(results and keys(results)) == ["alpha", "beta", "gamma"]


class TestBareStringFilters:
    def test_a_bare_string_matches_by_equality(self, manager, templates):
        assert keys(manager.get_filtered_templates({"name": "name-alpha"})) == ["alpha"]


class TestChoiceFilters:
    def test_exact_status(self, manager, templates):
        results = manager.get_filtered_templates(
            {"status": {"lookup": "exact", "value": ManagedTemplateStatus.ACTIVE}}
        )
        assert keys(results) == ["beta"]

    def test_exact_status_matching_nothing(self, manager, templates):
        results = manager.get_filtered_templates(
            {"status": {"lookup": "exact", "value": ManagedTemplateStatus.INACTIVE}}
        )
        assert keys(results) == []

    def test_status_in(self, manager, templates):
        results = manager.get_filtered_templates(
            {
                "status": {
                    "lookup": "in",
                    "value": [ManagedTemplateStatus.ACTIVE, ManagedTemplateStatus.ARCHIVED],
                }
            }
        )
        assert sorted(keys(results)) == ["beta", "gamma"]


class TestIntegerFilters:
    def test_version_gte(self, manager, templates):
        results = manager.get_filtered_templates({"version": {"lookup": "gte", "value": 2}})
        assert sorted(keys(results)) == ["beta", "gamma"]

    def test_version_lt(self, manager, templates):
        results = manager.get_filtered_templates({"version": {"lookup": "lt", "value": 2}})
        assert keys(results) == ["alpha"]


class TestMembershipFilters:
    def test_key_in(self, manager, templates):
        results = manager.get_filtered_templates(
            {"key": {"lookup": "in", "value": ["alpha", "beta"]}}
        )
        assert sorted(keys(results)) == ["alpha", "beta"]

    def test_key_exact(self, manager, templates):
        results = manager.get_filtered_templates({"key": {"lookup": "exact", "value": "alpha"}})
        assert keys(results) == ["alpha"]


class TestDateRangeFilters:
    @pytest.fixture
    def dated(self, make_template):
        with freeze_time("2024-01-01 12:00:00"):
            make_template(key="old", name="old")
        with freeze_time("2024-06-01 12:00:00"):
            make_template(key="mid", name="mid")
        with freeze_time("2024-12-01 12:00:00"):
            make_template(key="new", name="new")

    def _at(self, month):
        return datetime.datetime(2024, month, 15, tzinfo=datetime.UTC)

    def test_lower_bound_only(self, manager, dated):
        results = manager.get_filtered_templates({"created_at_range": {"from": self._at(3)}})
        assert sorted(keys(results)) == ["mid", "new"]

    def test_upper_bound_only(self, manager, dated):
        results = manager.get_filtered_templates({"created_at_range": {"to": self._at(3)}})
        assert keys(results) == ["old"]

    def test_both_bounds(self, manager, dated):
        results = manager.get_filtered_templates(
            {"created_at_range": {"from": self._at(3), "to": self._at(8)}}
        )
        assert keys(results) == ["mid"]

    def test_a_range_matching_nothing(self, manager, dated):
        results = manager.get_filtered_templates(
            {"created_at_range": {"from": self._at(2), "to": self._at(3)}}
        )
        assert keys(results) == []

    def test_updated_at_range(self, manager, dated):
        results = manager.get_filtered_templates({"updated_at_range": {"from": self._at(3)}})
        assert sorted(keys(results)) == ["mid", "new"]


class TestBooleanComposition:
    def test_and_requires_every_branch(self, manager, templates):
        results = manager.get_filtered_templates(
            {
                "and": [
                    {"name": string_lookup("name-alpha")},
                    {"description": string_lookup("Sent on signup")},
                ]
            }
        )
        assert keys(results) == ["alpha"]

    def test_and_with_a_failing_branch_matches_nothing(self, manager, templates):
        results = manager.get_filtered_templates(
            {"and": [{"name": string_lookup("name-alpha")}, {"name": string_lookup("name-beta")}]}
        )
        assert keys(results) == []

    def test_or_accepts_either_branch(self, manager, templates):
        results = manager.get_filtered_templates(
            {"or": [{"name": string_lookup("name-alpha")}, {"name": string_lookup("name-beta")}]}
        )
        assert sorted(keys(results)) == ["alpha", "beta"]

    def test_not_inverts(self, manager, templates):
        results = manager.get_filtered_templates({"not": {"name": string_lookup("name-alpha")}})
        assert sorted(keys(results)) == ["beta", "gamma"]

    def test_nested_groups(self, manager, templates):
        results = manager.get_filtered_templates(
            {
                "and": [
                    {
                        "or": [
                            {"name": string_lookup("name-alpha")},
                            {"name": string_lookup("name-beta")},
                        ]
                    },
                    {"description": string_lookup("Sent on signup")},
                ]
            }
        )
        assert sorted(keys(results)) == ["alpha", "beta"]

    def test_double_negation_cancels(self, manager, templates):
        results = manager.get_filtered_templates(
            {"not": {"not": {"name": string_lookup("name-alpha")}}}
        )
        assert keys(results) == ["alpha"]

    def test_de_morgan_over_and(self, manager, templates):
        """not (A and B) == (not A) or (not B)."""
        combined = {
            "and": [
                {"name": string_lookup("name-alpha")},
                {"subject_template": string_lookup("shared subject")},
            ]
        }
        negated = keys(manager.get_filtered_templates({"not": combined}))
        expanded = keys(
            manager.get_filtered_templates(
                {
                    "or": [
                        {"not": {"name": string_lookup("name-alpha")}},
                        {"not": {"subject_template": string_lookup("shared subject")}},
                    ]
                }
            )
        )
        assert sorted(negated) == sorted(expanded) == ["beta", "gamma"]

    def test_de_morgan_over_or(self, manager, templates):
        """not (A or B) == (not A) and (not B)."""
        combined = {
            "or": [{"name": string_lookup("name-alpha")}, {"name": string_lookup("name-beta")}]
        }
        negated = keys(manager.get_filtered_templates({"not": combined}))
        expanded = keys(
            manager.get_filtered_templates(
                {
                    "and": [
                        {"not": {"name": string_lookup("name-alpha")}},
                        {"not": {"name": string_lookup("name-beta")}},
                    ]
                }
            )
        )
        assert sorted(negated) == sorted(expanded) == ["gamma"]

    def test_several_keys_in_one_field_filter_are_anded(self, manager, templates):
        results = manager.get_filtered_templates(
            {"name": string_lookup("name-alpha"), "description": string_lookup("Sent on signup")}
        )
        assert keys(results) == ["alpha"]

    def test_several_keys_are_ored_under_negation(self, manager, templates):
        results = manager.get_filtered_templates(
            {"not": {"name": string_lookup("name-alpha"), "description": string_lookup("nope")}}
        )
        assert sorted(keys(results)) == ["alpha", "beta", "gamma"]


class TestNullSemantics:
    def test_a_positive_filter_excludes_null_rows(self, manager, templates):
        """``beta`` has a NULL subject_template, so it cannot match a positive comparison."""
        results = manager.get_filtered_templates(
            {"subject_template": string_lookup("shared subject")}
        )
        assert sorted(keys(results)) == ["alpha", "gamma"]

    def test_a_negated_filter_includes_null_rows(self, manager, templates):
        """SQL would drop NULLs from `NOT (subject = 'shared subject')`; the translator ORs in
        an __isnull=True so they come back, matching the in-memory reference evaluator."""
        results = manager.get_filtered_templates(
            {"not": {"subject_template": string_lookup("shared subject")}}
        )
        assert keys(results) == ["beta"]


class TestEmptyAndUnknownFilters:
    def test_an_empty_filter_matches_everything(self, manager, templates):
        assert sorted(keys(manager.get_filtered_templates({}))) == ["alpha", "beta", "gamma"]

    def test_a_negated_empty_filter_matches_nothing(self, manager, templates):
        assert keys(manager.get_filtered_templates({"not": {}})) == []

    def test_an_empty_and_matches_everything(self, manager, templates):
        assert sorted(keys(manager.get_filtered_templates({"and": []}))) == [
            "alpha",
            "beta",
            "gamma",
        ]

    def test_an_empty_or_matches_nothing(self, manager, templates):
        assert keys(manager.get_filtered_templates({"or": []})) == []

    def test_an_unknown_field_matches_nothing(self, manager, templates):
        assert keys(manager.get_filtered_templates({"not_a_field": "x"})) == []

    def test_an_unrecognised_lookup_dict_is_rejected(self, manager, templates):
        with pytest.raises(
            ManagedTemplateInvalidFilterError, match="field name filter is not valid"
        ):
            manager.get_filtered_templates({"name": {"bogus": 1}})


class TestOrdering:
    def test_defaults_to_newest_created_first(self, manager, templates):
        assert keys(manager.get_filtered_templates({})) == ["gamma", "beta", "alpha"]

    def test_created_ascending(self, manager, templates):
        results = manager.get_filtered_templates({}, {"field": "created_at", "direction": "asc"})
        assert keys(results) == ["alpha", "beta", "gamma"]

    def test_created_descending(self, manager, templates):
        results = manager.get_filtered_templates({}, {"field": "created_at", "direction": "desc"})
        assert keys(results) == ["gamma", "beta", "alpha"]

    def test_updated_ascending(self, manager, templates):
        results = manager.get_filtered_templates({}, {"field": "updated_at", "direction": "asc"})
        assert keys(results) == ["alpha", "beta", "gamma"]

    def test_updated_descending(self, manager, templates):
        results = manager.get_filtered_templates({}, {"field": "updated_at", "direction": "desc"})
        assert keys(results) == ["gamma", "beta", "alpha"]

    def test_ties_break_on_id_in_the_same_direction(self, manager, make_template):
        """A stable tiebreaker is what keeps offset pagination from dropping or repeating rows
        when many templates share a timestamp."""
        with freeze_time("2024-01-01 12:00:00"):
            for name in ["a", "b", "c"]:
                make_template(key=name, name=name)

        ascending = keys(
            manager.get_filtered_templates({}, {"field": "created_at", "direction": "asc"})
        )
        descending = keys(
            manager.get_filtered_templates({}, {"field": "created_at", "direction": "desc"})
        )

        assert ascending == ["a", "b", "c"]
        assert descending == ["c", "b", "a"]


class TestFilteredPagination:
    def test_paginates_a_filtered_set(self, manager, templates):
        page = manager.get_paginated_filtered_templates(
            {"name": string_lookup("name-", "starts_with")}, page=1, page_size=2
        )
        assert len(keys(page)) == 2

    def test_walks_every_page_without_gaps(self, manager, templates):
        filters = {"name": string_lookup("name-", "starts_with")}
        first = keys(manager.get_paginated_filtered_templates(filters, page=1, page_size=2))
        second = keys(manager.get_paginated_filtered_templates(filters, page=2, page_size=2))
        assert sorted(first + second) == ["alpha", "beta", "gamma"]

    def test_respects_the_ordering(self, manager, templates):
        page = manager.get_paginated_filtered_templates(
            {}, page=1, page_size=2, order_by={"field": "created_at", "direction": "asc"}
        )
        assert keys(page) == ["alpha", "beta"]

    def test_a_filter_matching_nothing_paginates_to_empty(self, manager, templates):
        page = manager.get_paginated_filtered_templates(
            {"name": string_lookup("nothing")}, page=1, page_size=2
        )
        assert keys(page) == []


class TestTranslationHelpers:
    """Direct unit tests for the Q builders, covering the defensive branches that the public
    filter API cannot reach on its own."""

    def test_a_bare_string_spec_becomes_a_case_sensitive_exact_match(self, manager):
        assert manager._string_lookup_q("name", "Welcome") == Q(name__exact="Welcome")

    def test_an_unknown_lookup_falls_back_to_exact(self, manager):
        spec = {"lookup": "not_a_lookup", "value": "Welcome", "case_sensitive": True}
        assert manager._string_lookup_q("name", spec) == Q(name__exact="Welcome")

    def test_a_lookup_spec_honours_case_insensitivity(self, manager):
        spec = {"lookup": "includes", "value": "Wel", "case_sensitive": False}
        assert manager._string_lookup_q("name", spec) == Q(name__icontains="Wel")

    def test_a_non_dict_range_spec_is_ignored(self, manager):
        assert manager._range_q("created", "not-a-range") == Q()

    def test_an_empty_range_spec_constrains_nothing(self, manager):
        assert manager._range_q("created", {}) == Q()

    def test_a_range_spec_builds_both_bounds(self, manager):
        lower = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
        upper = datetime.datetime(2024, 2, 1, tzinfo=datetime.UTC)
        assert manager._range_q("created", {"from": lower, "to": upper}) == (
            Q(created__gte=lower) & Q(created__lte=upper)
        )


class TestBareValueFilters:
    def test_a_bare_string_filters_on_any_value(self, manager, make_template):
        """Regression guard for the `value in (...)` / `field in (...)` mix-up, which used to
        make a bare string match only when it happened to spell a filterable field name."""
        make_template(key="odd", name="description")
        make_template(key="normal", name="name-normal")

        assert keys(manager.get_filtered_templates({"name": "description"})) == ["odd"]
        assert keys(manager.get_filtered_templates({"name": "name-normal"})) == ["normal"]

    def test_a_bare_string_matches_the_key(self, manager, templates):
        assert keys(manager.get_filtered_templates({"key": "alpha"})) == ["alpha"]

    def test_a_bare_enum_is_normalised_to_its_value(self, manager, templates):
        assert keys(manager.get_filtered_templates({"status": ManagedTemplateStatus.ACTIVE})) == [
            "beta"
        ]

    def test_a_bare_integer_matches_the_version(self, manager, templates):
        assert keys(manager.get_filtered_templates({"version": 2})) == ["beta"]


class TestNegatedUnknownField:
    def test_negating_an_unknown_field_matches_everything(self, manager, templates):
        """An unknown field has no column to test for NULL, so negation falls back to the
        plain inverse of the match-nothing Q."""
        results = manager.get_filtered_templates({"not": {"not_a_field": "x"}})
        assert sorted(keys(results)) == ["alpha", "beta", "gamma"]


class TestMalformedLookupsAreRejected:
    """Each field category falls through to the invalid-filter error when the value is shaped
    wrongly for it, rather than being silently captured by a different category."""

    def test_an_unknown_choice_lookup(self, manager, templates):
        with pytest.raises(ManagedTemplateInvalidFilterError, match="field status"):
            manager.get_filtered_templates({"status": {"lookup": "bogus", "value": "active"}})

    def test_a_choice_lookup_carrying_a_non_enum(self, manager, templates):
        with pytest.raises(ManagedTemplateInvalidFilterError, match="field status"):
            manager.get_filtered_templates({"status": {"lookup": "exact", "value": "active"}})

    def test_an_unknown_membership_lookup(self, manager, templates):
        with pytest.raises(ManagedTemplateInvalidFilterError, match="field key"):
            manager.get_filtered_templates({"key": {"lookup": "bogus", "value": "alpha"}})

    def test_an_unknown_integer_lookup(self, manager, templates):
        with pytest.raises(ManagedTemplateInvalidFilterError, match="field version"):
            manager.get_filtered_templates({"version": {"lookup": "bogus", "value": 1}})

    def test_a_range_field_given_a_string_lookup(self, manager, templates):
        with pytest.raises(ManagedTemplateInvalidFilterError, match="field created_at_range"):
            manager.get_filtered_templates(
                {"created_at_range": {"lookup": "exact", "value": "x", "case_sensitive": True}}
            )

    def test_a_string_field_given_a_date_range(self, manager, templates):
        with pytest.raises(ManagedTemplateInvalidFilterError, match="field name"):
            manager.get_filtered_templates(
                {"name": {"from": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)}}
            )
