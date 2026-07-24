# 2 · Model an ontology and act on it

**Goal:** turn two flat datasets into connected business objects, then change
one through a validated action.

**You'll learn:** object types, link types, actions, and how write-back works
without mutating your Parquet.

This continues [tutorial 1](01-ingest-transform-build.md). If you skipped it,
run `laurelin demo demo-workspace` and read along — the aviation demo has the
same shapes.

---

## Why bother

`clean_orders` is a table. It has rows and columns, and every question you ask
it is a SQL question.

An **ontology** says: these rows are *orders*, those rows are *customers*, and
this order belongs to that customer. Once Laurelin knows that, you get object
pages, link traversal, typed search, and — the part nothing else in OSS really
does — **actions**: validated, audited operations that write back.

This is the layer that turns a catalog into an operational platform.

## Add a customers dataset

Our orders reference customers by name; let's give customers their own
identity. Save as `customers.csv`:

```csv
customer,tier,account_manager
Aule Foundry,gold,mira
Yavanna Seeds,silver,mira
Nienna Textiles,gold,teo
Ulmo Shipping,bronze,teo
```

```bash
laurelin upload customers customers.csv --workspace .
```

## Declare the object types

Ontology lives in `ontology/*.yml`. Save as `ontology/orders.yml`:

```yaml
object_types:
  - api_name: customer
    display_name: Customer
    description: A company that places orders.
    backing_dataset: customers
    primary_key: customer
    title_property: customer
    properties:
      customer:        {type: string,  display_name: Name}
      tier:            {type: string,  display_name: Tier}
      account_manager: {type: string,  display_name: Account manager}

  - api_name: order
    display_name: Order
    description: A single customer order.
    backing_dataset: clean_orders
    primary_key: order_id
    title_property: order_id
    properties:
      order_id: {type: string, display_name: Order ID}
      customer: {type: string, display_name: Customer}
      region:   {type: string, display_name: Region}
      status:   {type: string, display_name: Status}
      amount:   {type: float,  display_name: Amount}

link_types:
  - api_name: customer_orders
    display_name: Orders
    from: customer
    to: order
    cardinality: one_to_many
    from_property: customer
    to_property: customer

actions:
  - api_name: mark_shipped
    display_name: Mark order shipped
    description: Flip an order to shipped once it leaves the warehouse.
    object_type: order
    kind: update
    parameters:
      status: {type: string, required: true, description: New status}

  - api_name: set_customer_tier
    display_name: Set customer tier
    description: Move a customer between service tiers.
    object_type: customer
    kind: update
    parameters:
      tier: {type: string, required: true, description: gold | silver | bronze}
```

What each block does:

- **`object_types`** — a named entity backed by a dataset. `primary_key` is the
  column that identifies one object; `title_property` is what to show as its
  label.
- **`link_types`** — a join, declared once. `from_property`/`to_property` are
  the columns on each side. Now every customer knows its orders.
- **`actions`** — the operations users are allowed to perform, with typed
  parameters. If it isn't declared here, it can't be done through the ontology.

No restart needed — the ontology is re-read per request.

## Explore it

```bash
laurelin serve --workspace . --no-auth
```

Open **Ontology** at <http://127.0.0.1:8787>. Click **Customer → Aule
Foundry**. You'll see its properties, and an **Orders** section listing the
orders linked to it — that's `customer_orders` resolving.

## Apply an action

On an order object, open the **Actions** panel, choose **Mark order shipped**,
set `status` to `shipped`, and apply. The object updates immediately.

Same thing over the API:

```bash
curl -X POST localhost:8787/api/v1/ontology/actions/mark_shipped/apply \
  -H 'Content-Type: application/json' \
  -d '{"pk": "1002", "parameters": {"status": "shipped"}}'
```

Now check the **Audit** tab. There's an `action_applied` entry naming the
action, the object, the parameters, and who did it.

### Where did the write go?

**Not into your Parquet.** Actions write to an *edit overlay* — an append-only
log of changes in `metadata.db`. When you read an object, Laurelin materializes
it from the backing dataset and then applies any edits on top.

That design is deliberate:

- **Your source data stays immutable.** A rebuild of `clean_orders` doesn't
  clobber operational edits, and edits don't corrupt the dataset a pipeline
  produced.
- **Every change is attributable.** The overlay *is* the history: who changed
  what, when, with what parameters.
- **Nothing is trapped.** Edits are queryable rows, not opaque state.

The tradeoff, stated plainly: the overlay is authoritative for object reads but
is *not* folded back into the Parquet. If a downstream SQL transform reads
`clean_orders` directly, it sees the pipeline's data, not the edits. Promoting
edits back into a dataset is a modeling decision you make with a transform, not
something Laurelin does behind your back.

### Validation is real

Try to apply an action with a missing required parameter, or one that isn't
declared:

```bash
curl -X POST localhost:8787/api/v1/ontology/actions/mark_shipped/apply \
  -H 'Content-Type: application/json' -d '{"pk": "1002", "parameters": {}}'
# 400 — missing required parameter 'status'

curl -X POST localhost:8787/api/v1/ontology/actions/delete_everything/apply \
  -H 'Content-Type: application/json' -d '{"pk": "1002", "parameters": {}}'
# 404 — no such action
```

The ontology is the contract. Anything not in it is not a capability.

## Let an agent drive it

The ontology is also the cleanest surface to hand an AI agent, because the
actions define exactly what it may do:

```bash
pip install 'laurelin[mcp]'
laurelin mcp --url http://127.0.0.1:8787 --token <your-api-token>
```

That serves 17 MCP tools — `search_objects`, `get_object`,
`get_linked_objects`, `apply_action`, `query_sql`, and more. Point Claude or
any MCP client at it and the agent can answer "which gold-tier customers have
open orders in EU?" and act on the answer.

Crucially, the agent is **not** privileged. It authenticates with an API token
belonging to a user, and every call runs through the same permission checks and
lands in the same audit log as a human's. Tutorial 3 is what makes that
statement mean something.

---

**What you built:** two object types, a link, and two validated actions —
a small operational app's worth of semantics, in ~40 lines of YAML.

**Next:** [Lock a dataset down →](03-securing-data.md)
