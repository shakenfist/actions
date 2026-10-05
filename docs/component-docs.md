# Publishing component documentation

Several Shaken Fist components publish their `docs/` directory as a
section of the main documentation site. `tools/ci_sync_docs.sh`, which
`shakenfist`'s `sync-external-docs.yml` workflow runs, checks out each
of those repositories and runs `tools/sync_component_docs.py` over its
`docs/`. That copies the markdown into
`docs/components/<component>/` and fills the component's placeholder in
`mkdocs.yml` with a navigation section.

This page describes the files a component can put in its `docs/` to
control that section.

## `component.yml`

An optional `component.yml` at the root of `docs/` names the
component's section. Without it, the component's name is title-cased.

```yaml
title: Kerbside
```

## `order.yml`

A directory without an `order.yml` lists every `.md` file in it,
sorted by the file's first `# ` heading, then a section for each
subdirectory that contains markdown.

A directory with an `order.yml` lists only the pages named there, in
that order. It only gets subsections for subdirectories that have an
`order.yml` of their own. Pages missing from the nav are still copied,
so links to them keep working. The exception is the docs root: a root
page that the root `order.yml` does not list is not published at all.
That makes commenting out a line the way to hold back an unfinished
page.

The usual form is a list of `file: title` entries:

```yaml
- index.md: Overview
- channels.md: Channels
# - draft.md: Not published yet
```

## Naming a subdirectory's section

A subdirectory's section is labelled with its directory name, with
hyphens and underscores turned into spaces and the result title-cased,
so `use-cases/` becomes "Use Cases" and `spice/` becomes "Spice". To
choose the label, write that directory's `order.yml` as a mapping, with
the label under `title` and the usual list under `pages`:

```yaml
title: SPICE protocol
pages:
  - index.md: Overview
  - channels.md: Channels
```

The list form keeps working, and a directory that does not set a
`title` keeps its existing label. A `title` in the root `order.yml` is
ignored, because `component.yml` names the root section.
