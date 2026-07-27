# Contributing to Laurelin

Thanks for looking. This page is short on purpose: it covers how to run what CI
runs, so nothing about your first pull request is a surprise.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,postgres,saml,mcp,engines,scheduler,metrics]'
```

Every optional extra is worth installing locally. Each one gates a group of
tests, and without it those tests *skip* rather than fail — so a partial install
gives you a green run that proved less than you think.

SAML additionally needs the `xmlsec1` binary (`apt install xmlsec1`,
`brew install libxmlsec1`).

## Running the tests

```bash
pytest -q                       # SQLite only
```

That leaves ~13 tests skipped. The PostgreSQL path is what makes multi-replica
deployment safe, so it deserves a real run:

```bash
docker run -d --rm --name laurelin-pg -p 55432:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=laurelin postgres:16

LAURELIN_TEST_POSTGRES='postgresql://postgres:test@127.0.0.1:55432/laurelin' pytest -q
```

Use `-rs` to see exactly what skipped and why.

> One hard-won piece of advice: don't run ad-hoc SQL against that database while
> a suite is running against it. Dropping a schema mid-run produces failures
> that look like real bugs in code that is fine.

## Lint

```bash
ruff check laurelin tests bench
```

The enabled rule set is close to ruff's defaults, plus import sorting. Rules
that are *not* enabled carry a comment in `pyproject.toml` saying why, so the
gap is a decision rather than an oversight.

## The scaling gate

`docs/SCALE.md` publishes measured numbers. `bench/regression.py` keeps them
honest:

```bash
python bench/regression.py            # exits 1 if a published claim broke
python bench/regression.py --report   # print the ratios, always exit 0
```

It asserts ratios, never milliseconds — a CI runner is too noisy for absolute
timings, and a flaky gate gets disabled, which protects nothing. Each threshold
is several times looser than the measured value, so it fires on a change in
complexity class (a lost pushdown, a materialization creeping back in) rather
than on drift.

If you change one of those numbers *deliberately*, update `docs/SCALE.md` in the
same pull request. Silencing the gate without deciding which side is wrong is
the one thing we'd rather you didn't do.

For the full picture, run the real benchmarks: `python bench/benchmark.py`.

## What CI enforces

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs four jobs:

| Job | What it protects |
|---|---|
| `lint` | ruff, plus vermin checking the syntax floor matches `requires-python` |
| `test` | The suite on Python 3.11–3.14, against SQLite **and** PostgreSQL |
| `package` | The wheel installs into a clean venv, serves, and still contains the UI |
| `bench` | The scaling claims published in the docs |

`tests/test_ci_guards.py` fails the run if the PostgreSQL suite skipped or an
optional extra is missing. A skip is invisible in a green run, and that is
exactly how a whole dialect stops being tested while the badge stays green.

## Pull requests

- Small and focused beats large and comprehensive.
- A behaviour change wants a test that fails without it.
- If you find a documented claim that is no longer true, fixing the document
  counts as a contribution. Several of the limitations in `docs/SCALE.md` were
  written honestly and then quietly outlived by the code.

## Security

Please don't open a public issue for a vulnerability. See
[SECURITY.md](SECURITY.md).
