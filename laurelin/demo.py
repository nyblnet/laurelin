"""Aviation demo workspace generator.

`create_demo` initializes a workspace, writes two deterministic raw datasets
(aircraft + flights, including a few malformed flight rows for the cleaning
step to drop), a pipeline file with cleaning + aggregation transforms, and an
ontology definition with linked object types and write-back actions. All data
is generated in code — no downloads, no randomness, no clock reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

import pyarrow as pa

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import BuildInfo
from laurelin.transforms import Builder, collect_transforms

MODELS = ["A320neo", "A321XLR", "B737-800", "B787-9", "E195-E2", "ATR72-600"]
OPERATORS = ["Laurelin Air", "Telperion Airways", "Silverwing Cargo"]
AIRPORTS = ["OAK", "SEA", "DEN", "AUS", "ORD", "BOS", "PDX", "MSY"]

_PIPELINE_TEMPLATE = '''\
"""Aviation demo pipeline: clean raw datasets, then aggregate flight stats."""

import pyarrow.compute as pc

from laurelin.transforms import Input, Output, sql_transform, transform


@transform(
    Output(dataset="clean_aircraft", description="Validated aircraft records"),
    aircraft=Input("raw_aircraft"),
)
def clean_aircraft(aircraft):
    ok = pc.and_(
        pc.is_valid(aircraft["tail_number"]),
        pc.is_valid(aircraft["model"]),
    )
    return aircraft.filter(ok)


@transform(
    Output(dataset="clean_flights", description="Flights with malformed rows dropped"),
    flights=Input("raw_flights"),
)
def clean_flights(flights):
    has_tail = pc.is_valid(flights["tail_number"])
    delay = flights["delay_minutes"]
    sane_delay = pc.or_kleene(pc.is_null(delay), pc.greater_equal(delay, -60))
    return flights.filter(pc.and_(has_tail, sane_delay))


@sql_transform(
    Output(dataset="flight_stats", description="Per-aircraft flight count and average delay"),
    inputs={"flights": Input("clean_flights"), "aircraft": Input("clean_aircraft")},
    query="""
        SELECT
            a.tail_number,
            any_value(a.model) AS model,
            count(f.flight_id) AS flight_count,
            round(avg(f.delay_minutes), 1) AS avg_delay_minutes
        FROM flights f
        JOIN aircraft a ON f.tail_number = a.tail_number
        GROUP BY a.tail_number
        ORDER BY a.tail_number
    """,
)
def flight_stats():
    pass
'''

_ONTOLOGY_TEMPLATE = """\
object_types:
  - api_name: aircraft
    display_name: Aircraft
    description: An aircraft in the demo fleet.
    backing_dataset: clean_aircraft
    primary_key: tail_number
    title_property: tail_number
    properties:
      tail_number: {type: string, display_name: Tail number}
      model: {type: string, display_name: Model}
      operator: {type: string, display_name: Operator}
      status: {type: string, display_name: Status}
      year_built: {type: integer, display_name: Year built}

  - api_name: flight
    display_name: Flight
    description: A scheduled flight leg.
    backing_dataset: clean_flights
    primary_key: flight_id
    title_property: flight_id
    properties:
      flight_id: {type: string, display_name: Flight ID}
      tail_number: {type: string, display_name: Tail number}
      origin: {type: string, display_name: Origin}
      destination: {type: string, display_name: Destination}
      scheduled_departure: {type: string, display_name: Scheduled departure}
      delay_minutes: {type: integer, display_name: Delay (min)}
      status: {type: string, display_name: Status}

link_types:
  - api_name: aircraft_flights
    display_name: Flights
    from: aircraft
    to: flight
    cardinality: one_to_many
    from_property: tail_number
    to_property: tail_number

actions:
  - api_name: update_aircraft_status
    display_name: Update aircraft status
    description: Set an aircraft's operational status.
    object_type: aircraft
    kind: update
    parameters:
      status: {type: string, required: true, description: New status}

  - api_name: cancel_flight
    display_name: Cancel flight
    description: Mark a flight as cancelled.
    object_type: flight
    kind: update
    parameters:
      status: {type: string, required: true, description: New flight status}

  - api_name: add_aircraft
    display_name: Add aircraft
    description: Register a new aircraft in the fleet.
    object_type: aircraft
    kind: create
    parameters:
      tail_number: {type: string, required: true, description: Registration}
      model: {type: string, required: true, description: Airframe model}
      operator: {type: string, required: false, description: Operating airline}
      status: {type: string, required: false, description: Operational status}
      year_built: {type: integer, required: false, description: Year built}
"""


def _aircraft_table() -> pa.Table:
    rows = []
    for i in range(12):
        rows.append(
            {
                "tail_number": f"N{100 + i}AA",
                "model": MODELS[i % len(MODELS)],
                "operator": OPERATORS[i % len(OPERATORS)],
                "status": "maintenance" if i % 5 == 0 else "in_service",
                "year_built": 2008 + i,
            }
        )
    return pa.table(
        {
            "tail_number": pa.array([r["tail_number"] for r in rows], pa.string()),
            "model": pa.array([r["model"] for r in rows], pa.string()),
            "operator": pa.array([r["operator"] for r in rows], pa.string()),
            "status": pa.array([r["status"] for r in rows], pa.string()),
            "year_built": pa.array([r["year_built"] for r in rows], pa.int64()),
        }
    )


def _flights_table() -> pa.Table:
    tails = [f"N{100 + i}AA" for i in range(12)]
    statuses = ["landed", "departed", "scheduled"]
    rows = []
    for i in range(60):
        day = 5 + (i % 7)  # a week starting 2026-01-05
        hour = 6 + (i * 5) % 16
        minute = (i * 17) % 60
        delay = None if i % 9 == 0 else ((i * 13) % 100) - 15
        rows.append(
            {
                "flight_id": f"LL{i + 1:04d}",
                "tail_number": tails[i % len(tails)],
                "origin": AIRPORTS[i % len(AIRPORTS)],
                "destination": AIRPORTS[(i + 3) % len(AIRPORTS)],
                "scheduled_departure": f"2026-01-{day:02d}T{hour:02d}:{minute:02d}:00",
                "delay_minutes": delay,
                "status": statuses[i % len(statuses)],
            }
        )
    # Malformed rows for the cleaning step: missing tail numbers and
    # nonsense negative delays.
    rows[14]["tail_number"] = None
    rows[38]["tail_number"] = None
    rows[22]["delay_minutes"] = -9999
    rows[51]["delay_minutes"] = -9999
    return pa.table(
        {
            "flight_id": pa.array([r["flight_id"] for r in rows], pa.string()),
            "tail_number": pa.array([r["tail_number"] for r in rows], pa.string()),
            "origin": pa.array([r["origin"] for r in rows], pa.string()),
            "destination": pa.array([r["destination"] for r in rows], pa.string()),
            "scheduled_departure": pa.array(
                [r["scheduled_departure"] for r in rows], pa.string()
            ),
            "delay_minutes": pa.array([r["delay_minutes"] for r in rows], pa.int64()),
            "status": pa.array([r["status"] for r in rows], pa.string()),
        }
    )


class DemoResult(NamedTuple):
    """The workspace, and what the build actually did.

    The build result used to be discarded, and the CLI printed a hardcoded
    "Pipeline built: clean_aircraft, clean_flights, flight_stats" whenever
    ``build=True`` — inspecting nothing. On today's template those three names
    happen to be right, which is the worst kind of wrong: the sentence is a
    literal, so it stays green when the build goes red, and the demo is the
    first build a new operator ever reads. Add a transform to the template, or
    let one fail, and the CLI reports a success that did not happen.
    Reporting what actually built costs one return value.
    """

    workspace: Workspace
    build: Optional[BuildInfo]


def create_demo(path: Path, build: bool = True) -> DemoResult:
    """Create the aviation demo workspace at `path`; optionally run the build."""
    workspace = Workspace.init(
        path,
        name="aviation-demo",
        description="Laurelin aviation demo: aircraft, flights, delay stats.",
    )
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)

    catalog.write(
        "raw_aircraft",
        _aircraft_table(),
        source="upload",
        description="Raw aircraft fleet records (demo-generated)",
    )
    catalog.write(
        "raw_flights",
        _flights_table(),
        source="upload",
        description="Raw flight legs including malformed rows (demo-generated)",
    )

    (workspace.pipelines_dir / "aviation.py").write_text(_PIPELINE_TEMPLATE)
    (workspace.ontology_dir / "aviation.yml").write_text(_ONTOLOGY_TEMPLATE)
    store.log_audit("demo_created", {"workspace": str(workspace.root)})

    info = None
    if build:
        registry = collect_transforms(workspace.pipelines_dir)
        info = Builder(workspace, catalog, store, registry).build()

    return DemoResult(workspace, info)
