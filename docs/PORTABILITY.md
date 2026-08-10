# Portability — leaving, and proving you can

Laurelin's durable advantage over a proprietary platform is that you can leave.
"It's Parquet on disk" is a claim about file formats, not a capability: nobody
wants to hand-reassemble datasets, versions, ontology bindings, policies and
markings out of a directory tree — that is the same work they were trying to
escape.

So the deliverable is not a tarball helper. It is: **a workspace can be
exported, reconstructed elsewhere, and proven to govern identically.** The proof
is the product.

```bash
laurelin export workspace.tar --fingerprint -w /srv/laurelin/acme
laurelin import workspace.tar -w /srv/laurelin/acme-new
laurelin verify-governance --baseline workspace.tar -w /srv/laurelin/acme-new
```

Or over HTTP, from Admin → Portability: preview, download, import, re-supply,
verify.

---

## What is in the archive

A POSIX (PAX) `tar` stream, optionally gzipped. Nothing else — `tar tvf` and
`tar xOf` inspect it on a machine that has never heard of Laurelin, and it
streams in both directions, so `laurelin export - | ssh host laurelin import -`
works. An opaque archive would make the anti-lock-in claim self-refuting.

```
manifest.json                          # always first: what this is, and what was withheld
laurelin.yml
ontology/<name>.yml                    # byte-faithful
pipelines/<name>.py                    # byte-faithful
tables/<table>.jsonl                   # one JSON object per row
data/<dataset>/parts/<uuid>.parquet    # keys identical to files_json
TRAILER.json                           # always last: a digest per member
```

`manifest.json` is at the head so `--dry-run` can print the whole withheld-secret
checklist without reading a terabyte. `TRAILER.json` is at the tail because
per-member digests cannot be known until after the members are written.

**The metadata database is not copied.** It cannot be: it holds SQLite-only FTS5
shadow tables that Postgres replaces with a pg_trgm index, and WAL mode means
`cp` on a running server silently loses committed rows. Rows travel as JSONL
through the store, which is the only dialect-neutral option — and it is why an
archive taken from SQLite restores onto PostgreSQL and back.

---

## What the archive will never contain

**The export omits secrets by allowlist; it does not redact them.** There is no
`--with-secrets` flag and there will not be one.

The reason is measured, not theoretical. Laurelin's three API redactors were
attacked with nine credential shapes and eight of them leaked — including
`postgresql://alice:p/w@db.internal:5432/prod`, which passes through the
production dataset redactor completely untouched. Every miss produced output
that *looked* redacted. A denylist over free-form values fails invisibly; an
allowlist fails as loud absence, which is what an operator can act on.

So, concretely:

| Location | What travels |
|---|---|
| `datasets.source_json`, `sources.config_json` | **Only** shape keys (`type`, `table`, `format`, `catalog`, `database`, `schema`, `namespace`, `mode`, `query`, `cursor_column`, `batch_size`, `branch`, `snapshot_id`). Every other key travels as `null`, whatever it is called. |
| `engines.uri` | Nothing. |
| `engines.options_json` | Keys, no values — the key is the shape you must re-fill. |
| `users.password_hash` | Nothing. |
| `sessions`, `oidc_flows` | The tables are not exported at all. |
| `api_tokens` | `(id, name, user_id, created_at)` unless `--no-audit`; never `token_hash`. |
| `audit_log.details_json` | Subjects (`username`, `dataset`, …) travel; credential-shaped keys, endpoint keys and free-form error text do not, and a row whose remaining values still look like a credential is withheld whole. |
| `sources.last_sync_error`, `builds.error`, `build_tasks.error` | Nulled, with a count in the manifest — driver error text is a documented credential channel and no redactor in this tree covers prose. |

**The export is deliberately stricter than the API.** `GET /sources` keeps
`user@host:port/db`, and a test enforces that: an admin reading a live system
they can already reach loses nothing and gains recognizability. A *file* is
different. `postgresql://svc_laurelin@pg-prod-3.internal:5432/crm` in an emailed
tarball is an internal network map plus a valid username, so the export
withholds the entire endpoint. The manifest still names the source, which the
operator already knows.

Every withheld field is **positively enumerated** in `manifest.withheld`, with
the route that puts it back. Absence is not a report.

### Content the export refuses to strip

`pipelines/*.py` is a code-execution channel — `transforms/api.py` `exec`s every
`.py` in it, unsandboxed, on every build. `ontology/*.yml`, a dashboard panel's
SQL, an object app's config and a schedule's targets are the same shape:
authored content that travels near-verbatim because stripping it would destroy
what it means.

The export scans all of them for credential-shaped lines and **refuses to write**
if it finds any. It never edits them behind your back — a transform silently
changed still runs, and computes something else. `--allow-content-warnings`
carries them as they are; the manifest records each one as `file:line` plus a
preview that stops at the match, so the report is not itself a leak.

### `data/` is unmasked

Exported Parquet is pre-policy bytes: the rows that column masks and row
policies hide from most principals at the source. That is correct for leaving
and wrong for sharing with a colleague, and one command cannot be both. Data is
included by default, `manifest.datasets[].data_state` says so, and the archive
is mode 0600 with ownerless tar headers.

Use `--metadata-only` for the governance-only dump: same members minus `data/**`,
a few hundred KB, gzipped. It is also how you audit the secrets posture — the
whole withheld list, without producing a terabyte.

---

## What import does, and refuses to do

**Import writes every governance rule verbatim and binds no principal.** For any
principal that exists in the target beforehand, the post-import answer —
`(can_view, can_edit)`, the visible rows, the unmasked cells — is a *subset* of
the source's answer for the same-named principal. Import may narrow. It may
never widen.

That means:

- **Grants import even when their subject does not resolve.** Measured: emptying
  a dataset's grant list flips it from an allowlist to readable-by-every-viewer.
  Dropping an unresolvable grant would *widen*.
- **`users` are not created**, `groups` land **empty**, and `clearances` travel
  in the archive and are **never written**. A clearance is the one row type whose
  only possible effect is to widen. Every one is listed in the import report as a
  checklist — naming the marking as it exists *at the target*, which matters when
  a name collision namespaced it.
- **Rebinding is an explicit admin act** through the existing audited routes.
  Nothing is inferred from a username. A destination IdP that mints
  `finance-lead` must not inherit the archive's `finance-lead`.
- **`schedules` land disabled** and `sources`/`engines` land marked
  needs-credentials, so an imported workspace never fires a build or a sync at a
  production system on boot.
- **Imported pipelines do not execute** until an admin acknowledges them
  (`POST /api/v1/workspace/import/acknowledge-pipelines`). Builds refuse first.

### Refusals

Import into a **non-empty** workspace refuses by default: with username and
marking name as the only join keys, an existing `analysts` group starts matching
the imported grants the instant the rows land. `--merge` runs a two-phase flow —
a report you read, then `--confirm <its sha256>` — and refuses anything with no
safe merge: a dataset name collision (unless `--rename-prefix`), an ontology
grant for a type the target owns, a workspace file the target already has, a
duplicate object-type `api_name`, and any authored row (dashboard, app, schedule,
source, engine) whose name is already taken.

An archive is also refused if a member escapes the workspace, is not in normal
form, appears twice, is nested inside another member, declares more bytes than
the size ceiling, names a storage key outside the data prefix, carries a row no
other code path could have written, or does not match its own `TRAILER.json`.

**What the trailer does not do:** there is no signature. An attacker who
rewrites a member can rewrite the trailer with it. What the digests catch is
corruption and *partial* rewrites — the case where two governance members are
swapped and the trailer is left alone.

---

## Proving it

`laurelin verify-governance` and `POST /api/v1/workspace/governance/fingerprint`
recompute a decision matrix: for each `(principal, dataset)` cell, a sha256 over
the **Arrow IPC bytes** of the result, through all three enforcement paths
(`apply_table_policy`, the Arrow plan, and the SQL renderer on DuckDB), plus the
permission tuples.

Arrow IPC bytes and not Python values, because a null mask preserves a column's
type while a redact mask turns it into a string, and both can round-trip to the
same `to_pylist()`. All three paths, because hash masking takes a different
branch — a round trip can preserve one renderer and break another.

The Portability page renders the source's fingerprint from the manifest beside
the target's, cell by cell. That panel is the product. A tarball helper has a
download button; a portability guarantee has a proof you can look at.

---

## Limitations — stated plainly

- **Non-managed datasets carry no data.** `federated`, `clickhouse` and
  `starrocks` datasets are pointers; their rows live in the remote system. Their
  governance, versions and registration shape travel, and reads at the
  destination refuse with **409 and a message** rather than returning zero rows —
  because in a governance product an empty result is indistinguishable from a
  working row policy.
- **Iceberg tables are not moved.** `warehouse_uri()` and `source.path` are
  absolute `file://`-derived paths and the catalog is a separate database at
  `<root>/iceberg-catalog.db`. The export warns and carries the governance; you
  re-register the table against a reachable warehouse.
- **Multi-workspace mode**: a user's effective role lives in
  `control.db::workspace_members`, outside the workspace. Export refuses unless
  you choose `--include-membership` (superadmin) or `--no-membership` (a
  governance-incomplete archive, recorded as such).
- **Object-store data planes are unverified.** A full export from
  `LAURELIN_DATA_URI` has not been measured end-to-end; it is opt-in behind
  `--allow-remote-data-plane`. `--metadata-only` reads no parts and is unaffected.
- **No incremental, resumable or scheduled export.** Full snapshot only; a
  resumable multi-terabyte transfer is `rsync`'s job.
- **`format_version: 1` only.** An archive from a future version is refused
  rather than guessed at.
- **Sessions and API tokens are never continuous.** Every credential is reissued
  at the destination, by design.

## The unsupported escape hatch

You can always `tar czf` the workspace directory. It is a **worse** artifact and
you should know why: `metadata.db` is mode 0644 (measured) and carries live
session tokens, plaintext PKCE verifiers, scrypt password hashes and every
connector DSN in the clear. It also cannot be restored onto a different metadata
backend.

If you want the bytes, take them — it is your directory, and that is the point.
The supported path exists because it is the one that does not hand somebody a
file full of live credentials.
