"""Ordering, and the capability report that makes it discoverable.

Every entry in ``_ORDER_FIELD_TO_ATTR`` is checked by *running* the sort rather than by reading
the column definition, because the way this fails is silent: a sort that ran against the wrong
column still returns a page, in an order nothing downstream can question. ``version`` is the
case worth pinning explicitly -- a store keeping versions as strings orders 10 before 2 and
looks correct until a key reaches its tenth version.

The other half is ``get_filter_capabilities``. Ordering keys default to ``False``, so a backend
that can sort and does not say so is never asked to.
"""

import pytest
from vintasend.services.notification_template_renderers.base import (
    BaseNotificationTemplateRenderer,
)
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.filters import (
    MANAGED_TEMPLATE_ORDER_BY_FIELDS,
    order_by_capability_key,
)
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService


pytestmark = pytest.mark.django_db


class _UnusedRenderer(BaseNotificationTemplateRenderer):
    """A renderer the ordering tests never reach.

    ``ManagedTemplateService`` needs one to construct, but every read here stops at the
    storage seam, so anything that satisfies the type is enough -- and one that raises would
    turn a read accidentally hitting the renderer into a failure rather than a pass.
    """

    def render(self, notification, context):
        raise AssertionError("an ordering test reached the renderer")

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        raise AssertionError("an ordering test reached the renderer")


@pytest.fixture
def template_service(manager) -> ManagedTemplateService:
    return ManagedTemplateService(manager, ManagedTemplateEmailRenderer(manager, _UnusedRenderer()))


@pytest.fixture
def rows(make_template):
    """Four keys whose fields disagree about the order they imply.

    Insertion order is the ``created`` order, and every other field disagrees with it, so a
    sort that quietly fell back to the default would fail rather than coincide.
    """
    return [
        make_template(key="delta", name="Alpha name", version=2, status="draft"),
        make_template(key="alpha", name="Delta name", version=10, status="archived"),
        make_template(key="charlie", name="Bravo name", version=3, status="active"),
        make_template(key="bravo", name="Charlie name", version=1, status="inactive"),
    ]


def ordered_keys(manager, field, direction="asc", **kwargs):
    return [
        t.key
        for t in manager.get_filtered_templates(
            {}, order_by={"field": field, "direction": direction}, **kwargs
        )
    ]


@pytest.mark.parametrize(
    ("field", "ascending"),
    [
        pytest.param("key", ["alpha", "bravo", "charlie", "delta"], id="key"),
        pytest.param("name", ["delta", "charlie", "bravo", "alpha"], id="name"),
        pytest.param("version", ["bravo", "delta", "charlie", "alpha"], id="version"),
        pytest.param("status", ["charlie", "alpha", "delta", "bravo"], id="status"),
        pytest.param("created_at", ["delta", "alpha", "charlie", "bravo"], id="created_at"),
    ],
)
def test_each_declared_field_actually_orders(manager, rows, field, ascending):
    assert ordered_keys(manager, field) == ascending
    assert ordered_keys(manager, field, "desc") == ascending[::-1]


def test_version_orders_numerically(manager, make_template):
    """The lesson from the sibling FHIR backend: v10 must come after v2, not before it."""
    for version in (10, 2, 3, 1, 11):
        make_template(key="welcome", version=version)

    ordered = manager.get_filtered_templates(
        {}, order_by={"field": "version", "direction": "asc"}
    )

    assert [t.version for t in ordered] == [1, 2, 3, 10, 11]


def test_updated_at_orders(manager, make_template):
    """Written apart from the rest: ``updated`` is ``auto_now``, so it moves on every save."""
    first = make_template(key="first")
    second = make_template(key="second")
    first.name = "Touched"
    first.save()

    ordered = manager.get_filtered_templates(
        {}, order_by={"field": "updated_at", "direction": "asc"}
    )

    assert [t.key for t in ordered] == [second.key, first.key]


def test_the_order_is_applied_before_the_page_is_sliced(manager, rows):
    """The failure this catches looks right on page 1 and is wrong on every page after it."""
    first = manager.get_paginated_filtered_templates(
        {}, page=1, page_size=2, order_by={"field": "key", "direction": "asc"}
    )
    second = manager.get_paginated_filtered_templates(
        {}, page=2, page_size=2, order_by={"field": "key", "direction": "asc"}
    )

    assert [t.key for t in first] == ["alpha", "bravo"]
    assert [t.key for t in second] == ["charlie", "delta"]


def test_the_unfiltered_paginated_read_orders_too(manager, rows):
    first = manager.get_paginated_templates(
        page=1, page_size=2, order_by={"field": "key", "direction": "asc"}
    )

    assert [t.key for t in first] == ["alpha", "bravo"]


def test_an_unordered_paginated_read_is_still_deterministic(manager, rows):
    """An unordered offset page over a table with a row per version repeats and skips rows."""
    everything = [t.key for t in manager.get_paginated_templates(page=1, page_size=10)]
    paged = [
        t.key
        for page in (1, 2)
        for t in manager.get_paginated_templates(page=page, page_size=2)
    ]

    assert paged == everything


def test_the_tiebreak_keeps_rows_from_being_dropped_across_pages(manager, make_template):
    """Six rows sharing one name: only the id tiebreak keeps the two pages disjoint."""
    for version in range(1, 7):
        make_template(key="welcome", name="Same", version=version)

    order_by = {"field": "name", "direction": "asc"}
    first = manager.get_paginated_filtered_templates({}, page=1, page_size=3, order_by=order_by)
    second = manager.get_paginated_filtered_templates({}, page=2, page_size=3, order_by=order_by)

    assert len({t.version for t in [*first, *second]}) == 6


# --- the report ----------------------------------------------------------------------


def test_every_orderable_field_is_declared(manager):
    """Declared, and true: each key above is backed by a passing sort in this file."""
    capabilities = manager.get_filter_capabilities()

    assert capabilities == {
        order_by_capability_key(field): True for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS
    }


def test_the_service_offers_every_field_this_backend_declares(manager, template_service):
    assert template_service.get_supported_order_by_fields() == list(
        MANAGED_TEMPLATE_ORDER_BY_FIELDS
    )


def test_no_filter_is_declined(manager):
    """Everything else is translated into SQL, so the all-True default is already right."""
    assert not [key for key in manager.get_filter_capabilities() if not key.startswith("orderBy.")]


def test_the_service_orders_through_this_backend(manager, template_service, rows):
    """End to end: the seam carries the order from the service to the SQL and back."""
    page = template_service.get_paginated_templates(
        1, 2, include_all_versions=True, order_by={"field": "key", "direction": "asc"}
    )

    assert [t.key for t in page] == ["alpha", "bravo"]


def test_a_status_the_backend_can_sort_is_ordered_by_its_wire_value(manager, rows):
    """Stored as a CharField, so the database sorts the four lowercase ASCII values directly."""
    ordered = manager.get_filtered_templates({}, order_by={"field": "status", "direction": "asc"})

    assert [t.status for t in ordered] == [
        ManagedTemplateStatus.ACTIVE,
        ManagedTemplateStatus.ARCHIVED,
        ManagedTemplateStatus.DRAFT,
        ManagedTemplateStatus.INACTIVE,
    ]
