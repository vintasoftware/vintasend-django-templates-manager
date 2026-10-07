# vintasend-django-templates-manager

The Django storage backend for
[vintasend-managed-templates](https://github.com/vintasoftware/vintasend-managed-templates): a
`DjangoTemplateManager` implementing the storage seam, the models behind it, and an admin for the
people who edit the copy.

`vintasend-managed-templates` defines *what* a managed template is — versions, a
draft/active/inactive/archived lifecycle with an audit trail, tags, filtering, and composition. It
deliberately does not say where any of it lives. This package puts it in your Django database.

## Install

```bash
poetry add vintasend-django-templates-manager
# or
pip install vintasend-django-templates-manager
```

Python 3.12–3.14, Django 4.2–6.0.

Add the app and migrate:

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "vintasend_django_templates_manager",
]
```

```bash
python manage.py migrate
```

## Wiring it up

`DjangoTemplateManager` is the backend; hand it to the renderer and the service from
`vintasend-managed-templates`:

```python
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from vintasend_django_templates_manager.django_templates_manager import DjangoTemplateManager

manager_backend = DjangoTemplateManager()
renderer = ManagedTemplateEmailRenderer(manager_backend, inner_renderer)
service = ManagedTemplateService(manager_backend, renderer)
```

`inner_renderer` is any vintasend email renderer. It receives template **source** rather than a
template name, so a renderer that resolves names through a loader —
`DjangoTemplatedEmailRenderer` included — needs a loader that accepts source. See
[What the inner renderer has to do](https://github.com/vintasoftware/vintasend-managed-templates#what-the-inner-renderer-has-to-do).

Everything else — creating templates, publishing versions, tagging, filtering — is
`ManagedTemplateService`'s API, unchanged.

## Which version a send renders

`get_active_template` is implemented natively: one indexed query for the key's highest-numbered
`active` version. That is what an unpinned send renders and what `pin_template_versions` pins to,
so a draft is never sent. A key that exists but has no active version raises
`ManagedTemplateNoActiveVersionError`. `get_template(key)` with no version still answers the newest
version whatever its status, for editors and APIs. See
[Which version a send renders](https://github.com/vintasoftware/vintasend-managed-templates#which-version-a-send-renders).

## Deleting a version

`delete_template` deletes only a version that was **never published**: still `draft`, with nothing
but `draft` in its status history. Anything else raises `ManagedTemplateDeletionNotAllowedError`,
including `delete_template(key)` with no version when the latest version is published. Archive a
published version instead.

The rule is checked with the row locked, inside the same transaction as the delete. To allow hard
deletes of published versions, which an operator should rarely need, switch it off on both the
backend and the service:

```python
manager_backend = DjangoTemplateManager(allow_deleting_published_versions=True)
service = ManagedTemplateService(manager_backend, renderer, allow_deleting_published_versions=True)
```

**Status history is never deleted**, whichever way a version goes: through `delete_template`, the
admin, or `ManagedTemplate.objects...delete()`. Each record stores its own `template_key` and
`version`, and its `template` link is set to `NULL` when the version is deleted, so
`get_template_status_history` keeps returning it. For the same reason a deleted version's number
is never reused: the next version is numbered one above the highest the key has ever had,
history included, so it cannot inherit someone else's trail.

The admin applies the same rule. A published version has no delete button and its delete page
answers 403, and a "Delete selected" batch holding one is refused whole. Set
`allow_deleting_published_versions = True` on a `ManagedTemplateAdmin` subclass to lift it.

## What is stored

| Model | What it holds |
|---|---|
| `ManagedTemplate` | One **version** of a template. `(key, version)` is unique, and a key has as many rows as it has versions. Nothing about a row changes once it exists except its status, its tags, and the derived `is_abstract` flag. |
| `ManagedTemplateStatusRecord` | The audit trail: who moved a version to which status, and when. Each record keeps its version's `template_key` and `version` on its own row, so it outlives the version (`template` becomes `NULL`). |
| `ManagedTemplateTag` | A label shared across templates, identified by the slug `vintasend_managed_templates.tags.slugify_tag` derives from its text. |

Versions are the reason a published template can never change under a notification that already
referenced it: `update_template` inserts the next version and leaves its predecessor exactly as it
was, content, status and history alike.

## Filtering and ordering

Every filter the library's vocabulary defines is translated into a Django `Q` and answered by the
database, so this backend declines nothing. What `get_filter_capabilities` reports is the other
direction — what it *can* do that the library does not assume:

```python
service.get_backend_supported_filter_capabilities()
# {..., 'orderBy.key': True, 'orderBy.name': True, 'orderBy.version': True,
#       'orderBy.status': True, 'orderBy.createdAt': True, 'orderBy.updatedAt': True}
```

The six `orderBy.*` keys are declared explicitly because they default to `False` in the library:
ordering is newer vocabulary than the filters, so a backend that can sort has to say so rather
than be assumed to. All six are real indexed columns on `ManagedTemplate`, so each is answered by
the database:

```python
service.get_paginated_templates(
    page=1, page_size=20, order_by={"field": "version", "direction": "desc"}
)
```

Two details worth knowing:

* **The order is composed into the SQL, not applied to the page.** A page ordered after it was
  chosen sorts rows *within* the page while the rows selected *for* it came back in the store's
  own order — right on page 1, wrong on every page after it.
* **`version` is a `PositiveIntegerField`**, so v10 sorts after v2. A store keeping versions as
  strings gets that wrong silently, which is why every orderable field is pinned by a test that
  runs the sort rather than reads the column definition.

An unordered read still orders by `-created, -id`: a key has a row per version, so an unordered
offset page is free to return one row twice and skip another.

## The admin

All three models are registered.

* **Templates** — `key` and `version` lock once the row exists, since they are its identity. Every
  status change made here is written to the audit trail, which is inlined read-only on the page.
  Only a never-published version can be deleted here — see [Deleting a version](#deleting-a-version).
* **Tags** — the slug is derived from the text on save rather than typed in, and collides safely
  (`black-friday-2`). Archive and restore are bulk actions; archiving retires a tag from the
  pickers without severing it from the templates carrying it.
* **Status history** — browsable, never editable. Listed and searched by each record's own key
  and version, so the history of a deleted version still shows up.

## Upgrading: the status-history migrations

`0002`–`0004` move status history off `on_delete=CASCADE`:

* `0002` adds nullable `template_key` and `version` columns to `ManagedTemplateStatusRecord`.
* `0003` is the data step. It copies each record's key and version from the version it belongs to,
  with one `UPDATE` per column. It only touches records that are still missing them, so running it
  again changes nothing.
* `0004` makes both columns required, makes `template` nullable with `on_delete=SET_NULL`, and adds
  an index on `(template_key, version)`.

They are split so the data copy never shares a transaction with a schema change on the same
table, which PostgreSQL can refuse.

**Deploy order.** Code from before this release writes status records without `template_key` or
`version`, which `0004` makes required, and this release's code needs the columns `0002` adds.
Migrating and switching code in one step (stop, migrate, start) needs nothing more. For a rolling
deploy, where old and new instances serve side by side:

1. `python manage.py migrate vintasend_django_templates_manager 0002`
2. roll out the new code everywhere, and wait until no old instance is left;
3. `python manage.py migrate` — `0003` fills any record an old instance wrote meanwhile, and
   `0004` makes the columns required.

**Rolling back** with `python manage.py migrate vintasend_django_templates_manager 0001` restores
the old schema. One case stops it: if any version has been deleted since the upgrade, its history
records have no version to point at, and the old schema cannot hold them. Rather than delete that
history, the rollback stops and reports how many such records exist. To roll back anyway, export
the records with `template=None` (their `template_key` and `version` say what they were about),
delete them deliberately, and run the rollback again.

## Composition

A template stored here can build on another one: extend a base, fill its hole, override its
blocks, splice in a shared fragment. All of it is resolved **before** the template engine runs, so
what Django's engine receives is one flat string with its own syntax untouched.

```
base-email    <html><body>
                {% managed_block header %}<h1>Acme</h1>{% managed_endblock %}
                {% managed_children %}
                {% managed_include "footer" %}
              </body></html>

welcome       {% managed_extends "base-email" %}
              {% managed_block header %}<h1>Welcome!</h1>{% managed_endblock %}
              <p>Hi {{ name }}, welcome aboard.</p>
```

The tag language, the abstract-template rule and the version semantics are
`vintasend-managed-templates`': see
[Composition](https://github.com/vintasoftware/vintasend-managed-templates#composition-bases-blocks-and-includes).
Composition itself is read off the template source — there is no separate model for it, no join,
and nothing to keep in step except the one flag below.

What this package adds is the editing side:

* **The admin refuses what will not compose.** Saving a template runs every one of its three
  sources through the composer against the database. A base that does not exist, a block left
  open, a chain of bases that loops back to the row being edited — each becomes an error on the
  field that carries it, rather than a notification that fails to send days later. Set
  `validate_composition = False` on a `ManagedTemplateAdminForm` subclass to save a template whose
  base has not been written yet.
* **The change form previews the result.** A read-only *composed body* under the content fields
  shows what the engine will actually receive, resolved against whatever the referenced templates
  are now.
* **The changelist marks the bases**, and filters on them, through the stored flag below.

### `is_abstract`

Whether a template is a base to build on rather than one to send is a fact about its source. It is
also the thing a "pick a template" screen most needs to filter on, so it is denormalized onto a
column:

```python
ManagedTemplate.objects.sendable()  # what a picker should offer
ManagedTemplate.objects.abstract()  # the bases
service.get_filtered_templates({"is_abstract": False})  # the same, through the library
```

`ManagedTemplate.save()` derives it, so every write path keeps it honest — the admin,
`DjangoTemplateManager`, a data migration, a shell session. It is `editable=False`: nobody types it
in, and a template whose composition tags are malformed saves as concrete rather than blowing up a
write (the admin form has already refused that template anyway).

To recompute rather than trust the column — for a row edited in memory, or one written before the
column existed:

```python
from vintasend_django_templates_manager.composition import template_is_abstract

template_is_abstract(template)  # raises on a malformed tag
template_is_abstract(template, strict=False)  # reads an unparseable template as concrete
```

Composition resolves references through this backend, which means an unpinned `{% managed_extends
"base-email" %}` picks up the **latest** version of that key, draft included — the editing-view
rule `get_template(key)` follows, not the send path's newest-active rule. So an active template can
compose against a base's unpublished draft. Pin it with `version=2` when a template has to keep
composing against an exact base.

Pinning the *notification* rather than the base is vintasend's job, through
`requested_template_version` — see
[Template Version Pinning](https://github.com/vintasoftware/vintasend#template-version-pinning).
This app stores those templates; it does not store the notifications.

## Development

```bash
poetry install
poetry run pytest
poetry run mypy
poetry run tox                     # the Python × Django matrix
poetry run pre-commit run --all-files   # ruff lint + format
```
