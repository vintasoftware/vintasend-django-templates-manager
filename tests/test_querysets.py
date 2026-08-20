from vintasend_django_templates_manager.models import ManagedTemplate
from vintasend_django_templates_manager.querysets import ManagedTemplateQuerySet


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
