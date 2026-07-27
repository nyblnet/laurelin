# 3 · Lock a dataset down

**Goal:** turn auth on, then restrict the same dataset four different ways —
by user, by row, by column, and by classification.

**You'll learn:** how Laurelin's authorization layers compose, and which one to
reach for.

Continues [tutorial 2](02-ontology-and-actions.md).

---

## Turn auth on

Drop `--no-auth` and restart:

```bash no-run
laurelin serve --workspace .
```

The first visit to <http://127.0.0.1:8787> shows a **setup** screen: create the
first administrator. (Everything so far ran as an implicit admin — that's what
`--no-auth` means, and why it's local-only.)

Create a couple more users, **Admin → Users**, or from the CLI:

```bash
laurelin users list --workspace .
```

For this tutorial, make:

| User | Role | Why |
|---|---|---|
| `root` | admin | you |
| `mira` | editor | account manager, US/EU |
| `teo` | viewer | account manager, APAC |

```bash
laurelin users create mira --role editor --password mira-password --workspace .
laurelin users create teo --role viewer --password teo-password --workspace .
laurelin users list --workspace .
```

(Passwords are on the command line here so the tutorial runs unattended; omit
`--password` and you'll be prompted, which is what you want on a real machine.)

Roles are the coarse layer: **viewer** reads, **editor** writes data and runs
builds, **admin** manages users and policy.

## Layer 1 — dataset ACLs

Roles are workspace-wide. ACLs are per dataset.

**Admin → Dataset access → `customers`**, add a grant for `mira` with view+edit
and save. Or:

```bash
curl -X PUT localhost:8787/api/v1/datasets/customers/permissions \
  -H 'Content-Type: application/json' \
  -d '{"grants": [{"subject_kind": "user", "subject": "mira",
                   "can_view": true, "can_edit": true}]}'
```

The rule that matters: **a dataset with no grants is open to the roles; the
moment it has one grant, it is closed to everyone not named.** So `teo` — a
viewer who could read `customers` a second ago — now can't see it at all. It
vanishes from `GET /datasets`, its rows 403, and in the SQL workbench it
becomes an unknown table rather than a permission error (no oracle telling you
what exists).

Grants also apply to `group`, `role`, and `everyone` subjects, so you rarely
name individuals in practice.

## Layer 2 — row-level security

Sometimes everyone may see the dataset, but only *their* rows.

**Admin → Data security → `clean_orders`**:

```bash
curl -X PUT localhost:8787/api/v1/datasets/clean_orders/policy \
  -H 'Content-Type: application/json' \
  -d '{"row_policy": {"column": "region",
                      "rules": [
                        {"subject_kind": "user", "subject": "mira",
                         "values": ["us-east", "eu-central"]},
                        {"subject_kind": "user", "subject": "teo",
                         "values": ["apac"]}
                      ]}}'
```

Now `mira` sees US/EU orders, `teo` sees APAC, and neither knows the other rows
exist. Two properties worth internalizing:

- **It fails closed.** A user with no matching rule sees *zero* rows, not all
  rows. If the policy column is missing from the data, you get zero rows rather
  than a leak.
- **It applies at the choke point.** Row filtering happens in one place that
  the rows API, the SQL workbench, dashboard panels, ontology objects, and the
  MCP tools all pass through. You cannot route around it by picking a different
  read path — that is the whole design.

Admins bypass row policies (otherwise nobody could audit the data).

## Layer 3 — column masking

Same dataset, sensitive column:

```bash
curl -X PUT localhost:8787/api/v1/datasets/clean_orders/policy \
  -H 'Content-Type: application/json' \
  -d '{"row_policy": {"column": "region",
                      "rules": [{"subject_kind": "user", "subject": "mira",
                                 "values": ["us-east", "eu-central"]},
                                {"subject_kind": "user", "subject": "teo",
                                 "values": ["apac"]}]},
       "column_masks": [{"column": "amount", "mode": "hash",
                         "exempt": [{"subject_kind": "user", "subject": "mira"}]}]}'
```

Three modes:

- **`null`** — blank it, keep the column's type. Good for "this shouldn't be
  here at all."
- **`redact`** — replace with `***`. Good for humans reading a table.
- **`hash`** — a stable sha256 prefix. The pseudonym is *consistent*, so you can
  still group and join on the column without ever seeing the value. This is the
  one people underuse.

`mira` is exempt and sees real amounts; `teo` sees hashes; `root` (admin) sees
everything.

## Layer 4 — classification markings

The three layers above are *discretionary* — someone grants you access.
Markings are **mandatory**: a label on the data that you must be cleared for,
and — the important part — **it propagates through lineage**.

Create a marking and apply it to the raw dataset:

```bash
curl -X POST localhost:8787/api/v1/markings \
  -H 'Content-Type: application/json' \
  -d '{"name": "pii", "description": "Customer-identifying data"}'

curl -X PUT localhost:8787/api/v1/datasets/raw_orders/markings \
  -H 'Content-Type: application/json' -d '{"markings": ["pii"]}'
```

Now run a build and look at `GET /api/v1/dataset-markings`:

```
clean_orders       explicit=[]      effective=['pii']    <- inherited
customers          explicit=[]      effective=[]
raw_orders         explicit=['pii'] effective=['pii']
revenue_by_region  explicit=[]      effective=['pii']    <- inherited
```

Nobody marked `clean_orders` or `revenue_by_region`. They inherited `pii`
because they descend from marked data — and `revenue_by_region` is two hops
downstream, holding nothing but regional sums. **This is the property that's
hard to retrofit and easy to get wrong**: derived data staying as classified as
its sources, automatically, without anyone remembering to re-label the outputs
of a pipeline. `customers` is untouched — it isn't downstream of anything
marked.

Grant clearance to let someone through:

```bash
curl -X PUT localhost:8787/api/v1/users/mira/clearances \
  -H 'Content-Type: application/json' -d '{"markings": ["pii"]}'
```

A non-admin needs clearance for **every** effective marking on a dataset.
Before the clearance, `mira` could see only `customers`; after it, she sees all
four. `teo` — no clearance, and locked out of `customers` by the ACL — now sees
an empty dataset list, which is the correct answer rather than an error.
Markings recompute after every build and on every marking change.

Admins bypass markings — a deliberate call, so that a marking can never lock
every human out of their own workspace. If you need admins constrained too,
that's a separate deployment posture (see [SECURITY.md](../../SECURITY.md)).

## How they compose

A read succeeds only if **all** of these pass:

```
role (viewer/editor/admin)
  └─ dataset ACL grant (if any grants exist)
       └─ classification clearance (every effective marking)
            └─ row policy (which rows)   +   column masks (which values)
```

Ontology objects compose too: seeing an object type requires the ontology grant
**and** access to its backing dataset — so locking a dataset also hides its
objects, and there's no path through the object API to data you can't read
directly.

## Check your work

The honest way to verify a security model is to try to break it. Log in as
`teo` and confirm:

```bash
# should be 403 / absent / unknown-table, never data
curl -b teo-cookies localhost:8787/api/v1/datasets/customers/rows
curl -b teo-cookies localhost:8787/api/v1/datasets            # customers absent
curl -b teo-cookies -X POST localhost:8787/api/v1/query \
  -H 'Content-Type: application/json' -d '{"sql": "SELECT * FROM customers"}'
```

Then check **Audit**: every policy change you just made is recorded, with the
actor.

## Two things to do before production

1. **`--lock-pipelines`.** Transforms are Python that the server executes.
   Anyone who can write a pipeline has code execution on the server — that's
   inherent to the feature, not a bug, and it's why in-browser transform
   authoring is editor-gated. On a multi-tenant or untrusted-editor
   deployment, serve with `--lock-pipelines` and manage pipelines through git.
2. **TLS and `--secure-cookies`.** Session cookies get the `Secure` flag; put a
   TLS-terminating ingress in front. See [DEPLOYMENT.md](../DEPLOYMENT.md).

---

**What you built:** four composed authorization layers over one dataset,
including mandatory markings that follow derived data through the DAG.

**Where next:** [ARCHITECTURE.md](../ARCHITECTURE.md) for how it's implemented,
[SCALE.md](../SCALE.md) for what this handles and what it doesn't, or
[DEPLOYMENT.md](../DEPLOYMENT.md) to ship it.
