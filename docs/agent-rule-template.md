# Using repolens in this repo

This repo is indexed by **repolens** — one ranked search index over its docs, code
purpose-lines, and DB schema. It re-indexes changed files on every query, so results
are never stale, and each hit comes back with the **passage that matched**, not just a
file path.

## The rule

**Default to `repolens find "<what you're after>"`** to locate anything — where X lives,
which file handles Y, what covers a concept. It ranks the best few files and shows the
matching text.

**Use `rg` / grep only for** an exact string you need _every_ match of, or a regex.
