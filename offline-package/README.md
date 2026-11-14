# Offline resource package

This directory is the flattened, offline copy of the JSON resources under
`suite/tests`, plus the suite control document and dialect index.

## Layout

- `*.json` -- one flat file per source resource.
- `test-schema.json` -- the suite's control schema, reachable without a
  directory climb.
- `specifications.json` -- the unchanged dialect index.
- `migration.json` -- machine-readable old/new entry correspondence,
  rewritten links, preserved external identities and alias groups.
- `ALIASES.md` -- human-readable groups of entries with identical bytes.
- `verification.json` -- reference-equivalence results.

## Naming

A flat filename is `STEM--DRAFT.json`, e.g.
`multiple-lookup--07.json` for
`tests/json-schema-draft-07/multiple-lookup.json`.

The draft is part of the name because resources with different dialects are
different business definitions.  In particular, a resource whose content
merely matches another resource's bytes stays a separate entry.  This
prevents two aliases from being folded together with a third resource which
shares their final filename but not their contents.

## References

Only file-level relative control links are rewritten:

```text
"$schema": "../../test-schema.json"  ->  "$schema": "test-schema.json"
```

The control document's `$schema` pattern is adjusted the same way.  Every URI
identity (`$id` / `id`, `$ref`, `$dynamicRef`, registry keys, `urn:`, `tag:`
and absolute `http:` contracts) is copied verbatim.  Relative and
fragment-only identities keep their meaning because their containing resource
is copied byte-for-byte apart from the control link.

## Verification

`verification.json` records:

- `assertions`: old and new lookups compared, including every chained `then`.
- `expectations`: migrated lookups checked against the suite's declared
  `target` or `error`.
- `links_checked`: local control links which resolve to the flat control
  document.

RFC 3986 normalization cases are compared old-to-new but excluded from the
declared-target check, mirroring the suite's own `xfail` while URL
canonicalization is unsupported by the current API.

## Reproduce

From the repository root:

```bash
.venv/bin/python -m referencing.flatten suite offline-package --force
```

The migration refuses to overwrite a non-empty directory unless `--force`
is given or the directory carries the `.migrated` marker.
