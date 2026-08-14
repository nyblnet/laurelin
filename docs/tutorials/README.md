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
pip install -e ".[dev]"       # from a checkout: Laurelin is not on PyPI yet
laurelin --help
```

Every command below runs against a workspace directory you own. Nothing is
hosted, nothing phones home, and every artifact — Parquet data, YAML ontology,
Python pipelines — stays a file you can read, diff, and commit.

If you would rather explore something already populated, `laurelin demo
demo-workspace` generates an aviation workspace with data, a pipeline, and an
ontology already in place.

---

## These pages are tested

Every command below runs in CI (`tests/test_tutorials.py`), against a real
workspace and a real server, in the order you'd follow them. If a command here
stops doing what the page says, the build goes red.

Fenced blocks carry annotations that are invisible when rendered:

| Annotation | Meaning |
|---|---|
| ```` ```bash ```` | runs, and must succeed |
| ```` ```bash expect-fail ```` | runs, and must **fail** — how the docs prove validation is real |
| ```` ```bash no-run ```` | shown but not run: it needs something you supply (a database, a long-running server) |
| ```` ```csv file=orders.csv ```` | written to that path before the commands that use it |

`no-run` is for commands that genuinely need what a test can't provide. It is
not for making a broken example go quiet.
