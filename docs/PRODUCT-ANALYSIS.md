# Laurelin — product & adoption analysis

*How potential users will use and perceive the platform, where adoption leaks,
and what to improve.*

> **Read this as a dated snapshot, not as a description of the platform
> today.** The analysis below was written against a much earlier tree, and the
> two bottlenecks it identifies — no connectors, synchronous in-request builds
> — were **both closed**, along with most of the "Remaining" list at the
> bottom. The "Who actually shows up" table and "The adoption funnel" section
> are preserved as the original diagnosis; every present-tense capability claim
> in them is stale. What is still open, verified against the tree:
>
> - **A hosted live demo** — still nothing; needs hosting infrastructure.
> - **Generated typed SDKs** — not built (`laurelin sdk generate` does not
>   exist; `laurelin/mcp/client.py` is a hand-written client, not codegen).
> - **App builder** — `ObjectAppInfo` is configured, not composed; there is no
>   builder UI.
> - **Dashboard v2** (cross-filtering, parameters) — not built.
> - **More connectors** — still exactly three: postgres, http, file
>   (`laurelin/connectors/connectors.py`).
>
> Everything else in "Remaining" shipped: query resource limits and admission
> control (`laurelin/core/limits.py`), streaming transforms, a real ontology
> index (`laurelin/ontology/store.py`), cron and on-upstream schedules,
> scheduled syncs, incremental builds, and an object-storage data plane
> (`LAURELIN_DATA_URI`). Anyone using this page to plan work should check the
> tree first.

## TL;DR *(as originally written — see the note above)*

Laurelin is **unusually complete on governance and identity** for an
open-source project — multi-workspace tenancy, local + OIDC + SAML SSO, SCIM
provisioning, three composed layers of authorization (RBAC → dataset/ontology
ACLs → row/column security → mandatory **classification markings that propagate
through lineage**), audit, and a Postgres + Helm deployment story. Its genuine
differentiator is the **ontology + actions + write-back** layer, which almost no
OSS competitor has.

But adoption will bottleneck at two predictable places that have nothing to do
with those strengths:

1. **Getting your own data in** — ingestion is upload-only (CSV/Parquet); there
   are no connectors. This is a cliff between the polished demo and real data.
2. **Trusting it past demo scale** — compute is single-process, in-memory
   DuckDB; the query path and ontology materialization load whole datasets into
   memory. Fine at MB–low-GB, not at "enterprise scale."

The strategic mismatch: the **closer** (governance/identity, which people are
*relieved* to have) is the strongest part; the **opener** (ingestion + a visible
payoff, which is what actually drives someone to adopt) is the weakest. That's
backwards for adoption.

## Who actually shows up

| Persona | What they want | First thing they try | Where they stall | Verdict today |
|---|---|---|---|---|
| **Platform / data engineer** (the decision-maker) | An open Foundry: pipelines, lineage, scale, ops | "Point it at my Postgres / S3 / warehouse" | No connector — upload only. Then: "how big can a dataset be?" | Impressed by the model, blocked on ingestion + unsure on scale |
| **Analytics engineer / analyst** | Model data semantically, explore it | SQL workbench, then the ontology | Ontology is queryable but abstract — no dashboard/app to *show* the payoff | "Workbench is nice; ontology intriguing but what do I get?" |
| **Operational-app builder** (the *real* Foundry use case) | An app over an ontology with validated write-back | Object types, links, actions | The pieces exist but there's no app-builder — you'd hand-build a frontend | Differentiator is 80% there and invisible |
| **Security / compliance** | Can it hold regulated data? | SSO, ACLs, RLS, markings, audit | *Selling* persona now — SSO, per-object/dataset ACLs, RLS+masking, marking propagation, per-workspace isolation are real and reviewed | Pleasantly surprised; a reason to say yes |
| **OSS evaluator / small team** | `pip install`, kick tires, self-host | The demo, local-first story | Docs are architecture-oriented, no task tutorials | Converts easily if onboarding lands |
| **AI / agent builder** (2026) | A governed semantic layer agents can operate | (nothing yet — no MCP/SDK) | No surface for them | Cheapest-to-serve, highest-upside wedge — the substrate already exists |

## The adoption funnel — where it leaks

- **Discovery → first run:** strong. `pip install` + a seeded demo workspace is a real asset.
- **First run → "aha":** medium. The aha differs per persona and only the SQL one lands cleanly; docs are reference, not tutorial.
- **Aha → my own data:** **biggest leak.** Upload-only ingestion walls off everyone who liked the demo.
- **My data → production:** leaks on **ops + scale**. Builds run *synchronously in the HTTP request* (no scheduler/queue/incremental) — a real pipeline times out.
- **Production → scale:** honest ceiling. `catalog.query` registers each whole dataset as an in-memory Arrow table; RLS reads whole tables; the ontology materializes all objects per request with no index.

## How they'll perceive it

**Lands:** "Shockingly complete for OSS" (months of auth/RBAC/RLS/SSO/tenancy
most projects never do); "no lock-in is real, not marketing"; "the ontology is
the interesting part."

**Hesitation:** "Is this real or a very good scaffold?" — no benchmarks, no
hosted demo, no case study, no "who maintains this." For a platform asking to
hold crown-jewel data, **trust is the conversion barrier, and it's currently
asserted, not demonstrated.**

## What to improve — prioritized by adoption leverage

**Tier 1 — unblock "demo → my data" (nothing else matters if users can't get in):**
1. **Connectors** — even a thin set (JDBC/Postgres pull, S3/GCS parquet/CSV glob,
   HTTP/REST). Highest-leverage feature for the decision-maker.
2. **Async, scheduled builds** — move builds off the request thread onto a
   Postgres-backed queue + workers; add cron/on-upstream triggers and
   incremental transforms. Synchronous in-request builds break first.
3. **A scale-honest query path** — push filters/limits/projection into DuckDB
   over the Parquet files instead of loading whole Arrow tables; index the
   ontology instead of full-scan materialization.

**Tier 2 — make the differentiator visible (this is what makes it *not* just another catalog):**
4. **Charts + dashboards**, and a **simple object-app view** over the ontology.
   The ontology+actions layer is the moat; today it's invisible. Showing a
   stakeholder a working operational app is the demo that sells the platform.
5. **MCP server + generated SDKs** — cheap vs impact. "Your agents operate your
   ontology through the normal permission + audit path" is a 2026-native pitch
   nobody else in OSS can make, and the substrate already exists.

**Tier 3 — convert the skeptics (perception, not features):**
6. **A hosted live demo** (read-only, seeded) — try it in 30s, no install.
   *(Still open — needs hosting infrastructure.)*
7. ~~**Real numbers**~~ — **done**: [SCALE.md](SCALE.md) publishes measured,
   reproducible benchmarks plus an explicit "what this is / isn't." The
   honesty paid off immediately: it surfaced the ontology full-scan ceiling
   (~100 K objects/type) as the top optimization target.
8. ~~**Onboarding docs**~~ — **done**: three task-shaped
   [tutorials](tutorials/) (each verified end-to-end) and a
   [`SECURITY.md`](../SECURITY.md) with trust boundaries and a disclosure
   process.

## What's already closed vs what remains

**Closed (as of this analysis):** multi-workspace tenancy; local + OIDC + SAML
SSO; SCIM provisioning + deprovisioning; RBAC + dataset/ontology ACLs; row-level
security + column masking; **classification markings with lineage propagation**;
audit; PostgreSQL control plane; Docker + Helm + HA-for-the-API-tier. The
enterprise *governance and identity* gaps are effectively done — this is the
"closer," and it's strong.

**Closed since (first Tier-1 pass):** first connectors — PostgreSQL (streamed),
HTTP CSV/Parquet, server-side file drops — with redacted-secret source configs
and one-click sync in the UI; async builds (POST /builds returns immediately,
worker pool executes, UI polls); a scale-honest workbench query path (lazy
Arrow scans with filter pushdown for un-policied datasets — query memory now
scales with the result, not the dataset).

**Closed since (Tier-2 pass):** charts on workbench results + dashboards
(saved-query grids whose panels run with the viewer's credentials — RLS/ACLs
per user); the MCP server (`laurelin mcp`) + `LaurelinClient` Python SDK —
agents operate datasets, SQL, the ontology, actions, and builds through the
normal token/permission/audit path.

**Closed since (Tier-3 pass, trust):** three task-shaped tutorials
(ingest→transform→build; ontology + actions; locking a dataset down), each
verified end-to-end against a real workspace; `SECURITY.md` with the security
model, named trust boundaries, and a disclosure process; and `docs/SCALE.md` —
published, reproducible benchmarks (`bench/benchmark.py`) with an explicit
"what this is / isn't," including the ontology full-scan ceiling.

**Closed since (scale pass):** incremental storage — a version is a manifest
of Parquet parts, so appends cost O(delta) not O(dataset) (~70× on 5 M rows
with a 1% delta) — plus cursor-based incremental connector syncs; and
ontology query pushdown into DuckDB (~26× faster; 36 s → 1.4 s at 5 M
objects, point lookups 279 ms).

RLS predicate pushdown also landed: row policies are applied with
`Dataset.filter()` inside the scan, so the 3.6× tax is gone (1.0× at 5 M
rows) and policied users get the fast ontology path too.

**Remaining, re-checked against the tree** (the list that follows this
paragraph was written before most of it shipped; struck items are done):

- ~~scheduled syncs; cron/event-triggered + incremental builds~~ — shipped
  (`laurelin/core/scheduler.py`, leased so firing is exactly-once).
- ~~a true ontology *index*~~ — shipped (`laurelin/ontology/store.py`), with
  key lookups and selective search constant-time.
- ~~query resource limits + admission control~~ — shipped
  (`laurelin/core/limits.py`).
- ~~streaming Python transforms~~ — shipped (`streaming=True`).
- ~~object-storage-backed workspaces~~ — shipped (`LAURELIN_DATA_URI`).
- **More connectors** — still open. Exactly three:
  `CONNECTOR_TYPES = ("postgres", "http", "file")`.
- **A multi-process worker/queue** — still open. Builds run on a per-replica
  `ThreadPoolExecutor`, coordinated across replicas by leases, not by a queue.
- **Dashboard v2** (cross-filtering, parameters) **and an app builder** —
  still open.
- **Generated typed SDKs** — still open; there is no `laurelin sdk generate`.
- **A hosted demo** — still open.

## The one strategic call

**Resist adding more enterprise checkboxes; close the "demo → my data → a visible
result" loop instead** (Tier 1 + #4). The governance depth is a genuine strength,
but it's a *closer*, not an *opener*. People don't adopt a data platform because
of its RLS — they adopt because it got their data in and showed them something,
and *then* they're relieved the governance is there. Right now the opener is the
weakest part and the closer is the strongest — which is exactly backwards for
driving adoption.
