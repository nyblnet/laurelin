# Laurelin tutorials

Three task-shaped walkthroughs. They share one scenario — a small logistics
company's orders — and build on each other, but each stands alone.

| # | Tutorial | You end up with | Time |
|---|---|---|---|
| 1 | [Ingest → transform → build](01-ingest-transform-build.md) | Raw CSV in, a two-stage pipeline, derived datasets, lineage | ~10 min |
| 2 | [Model an ontology and act on it](02-ontology-and-actions.md) | Object types, a link, and a validated write-back action | ~15 min |
| 3 | [Lock a dataset down](03-securing-data.md) | ACLs, row-level security, column masking, classification markings | ~15 min |

**Before you start**

```bash
pip install laurelin          # or: pip install -e ".[dev]" from a checkout
laurelin --help
```

Every command below runs against a workspace directory you own. Nothing is
hosted, nothing phones home, and every artifact — Parquet data, YAML ontology,
Python pipelines — stays a file you can read, diff, and commit.

If you would rather explore something already populated, `laurelin demo
demo-workspace` generates an aviation workspace with data, a pipeline, and an
ontology already in place.
