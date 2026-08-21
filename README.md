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

## What is stored

| Model | What it holds |
|---|---|
| `ManagedTemplate` | One **version** of a template. `(key, version)` is unique, and a key has as many rows as it has versions. Nothing about a row changes once it exists except its status, its tags, and the derived `is_abstract` flag. |
| `ManagedTemplateStatusRecord` | The audit trail: who moved a version to which status, and when. |
| `ManagedTemplateTag` | A label shared across templates, identified by the slug `vintasend_managed_templates.tags.slugify_tag` derives from its text. |

Versions are the reason a published template can never change under a notification that already
referenced it: `update_template` inserts the next version and leaves its predecessor exactly as it
was, content, status and history alike.

## The admin

All three models are registered.

* **Templates** — `key` and `version` lock once the row exists, since they are its identity. Every
  status change made here is written to the audit trail, which is inlined read-only on the page.
* **Tags** — the slug is derived from the text on save rather than typed in, and collides safely
  (`black-friday-2`). Archive and restore are bulk actions; archiving retires a tag from the
  pickers without severing it from the templates carrying it.
* **Status history** — browsable, never editable.

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
ManagedTemplate.objects.sendable()      # what a picker should offer
ManagedTemplate.objects.abstract()      # the bases
service.get_filtered_templates({"is_abstract": False})   # the same, through the library
```

`ManagedTemplate.save()` derives it, so every write path keeps it honest — the admin,
`DjangoTemplateManager`, a data migration, a shell session. It is `editable=False`: nobody types it
in, and a template whose composition tags are malformed saves as concrete rather than blowing up a
write (the admin form has already refused that template anyway).

To recompute rather than trust the column — for a row edited in memory, or one written before the
column existed:

```python
from vintasend_django_templates_manager.composition import template_is_abstract

template_is_abstract(template)                 # raises on a malformed tag
template_is_abstract(template, strict=False)   # reads an unparseable template as concrete
```

Composition resolves references through this backend, which means an unpinned `{% managed_extends
"base-email" %}` picks up the **latest** version of that key, draft included — the same rule
`get_template(key)` follows everywhere else. Pin it with `"base-email[v2]"` (or the equivalent
`version=2`) when a template has to keep composing against an exact base.

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
