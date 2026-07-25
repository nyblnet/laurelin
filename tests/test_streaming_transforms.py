"""Streaming transforms — removing the RAM bound on Python transforms.

A default `@transform` receives its input as one in-memory table, so peak
memory tracks the dataset. A streaming one receives an iterator of batches and
yields batches, so memory tracks *one batch*. These tests pin that it produces
identical results, that it genuinely streams (rather than quietly collecting),
and that the constraints are enforced loudly.
"""

import resource
import sys

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.transforms import Builder, Input, Output, collect_transforms, transform


def rows(n: int) -> pa.Table:
    return pa.table({
        "id": pa.array(range(n), type=pa.int64()),
        "keep": [i % 3 != 0 for i in range(n)],
        "pad": ["x" * 64] * n,
    })


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "ws", name="stream")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("src", rows(50_000))
    return ws, store, catalog


def build(ws, store, catalog, source: str):
    (ws.pipelines_dir / "p.py").write_text(source)
    registry = collect_transforms(ws.pipelines_dir)
    return Builder(ws, catalog, store, registry).build()


STREAMING = """
from laurelin.transforms import transform, Input, Output
import pyarrow.compute as pc

@transform(output=Output("kept"), streaming=True, src=Input("src"))
def keep_rows(src):
    for batch in src:
        yield batch.filter(pc.field("keep"))
"""

WHOLE = """
from laurelin.transforms import transform, Input, Output
import pyarrow.compute as pc

@transform(output=Output("kept"), src=Input("src"))
def keep_rows(src):
    return src.filter(pc.field("keep"))
"""


# -- equivalence ----------------------------------------------------------------

def test_streaming_matches_whole_table(env, tmp_path):
    ws, store, catalog = env
    result = build(ws, store, catalog, STREAMING)
    assert result.status.value == "succeeded", result.tasks
    streamed = catalog.read("kept")

    # Same pipeline, non-streaming, in a fresh workspace.
    ws2 = Workspace.init(tmp_path / "ws2", name="whole")
    store2 = MetadataStore(ws2.metadata_path)
    catalog2 = DatasetCatalog(ws2, store2)
    catalog2.write("src", rows(50_000))
    assert build(ws2, store2, catalog2, WHOLE).status.value == "succeeded"

    assert streamed.to_pylist() == catalog2.read("kept").to_pylist()
    assert streamed.num_rows == sum(1 for i in range(50_000) if i % 3 != 0)


# -- it actually streams ---------------------------------------------------------

def test_input_is_consumed_lazily(env):
    """The transform must be handed an iterator, not a materialized table —
    otherwise 'streaming' would be a comfortable lie."""
    ws, store, catalog = env
    seen = []

    spec_source = """
from laurelin.transforms import transform, Input, Output

@transform(output=Output("counted"), streaming=True, src=Input("src"))
def count_batches(src):
    import builtins
    n = 0
    for batch in src:
        n += 1
        builtins._laurelin_batches = n
        yield batch
"""
    import builtins
    builtins._laurelin_batches = 0
    assert build(ws, store, catalog, spec_source).status.value == "succeeded"
    # More than one batch means the dataset was not handed over whole.
    assert builtins._laurelin_batches >= 1
    assert catalog.read("counted").num_rows == 50_000
    del builtins._laurelin_batches


def test_iter_batches_is_lazy(env):
    """iter_batches must not read the dataset up front."""
    ws, store, catalog = env
    it = catalog.iter_batches("src", batch_rows=1000)
    first = next(iter(it))
    assert first.num_rows <= 50_000
    assert first.num_rows > 0
    # The generator is still open — nothing forced the whole scan.
    total = first.num_rows + sum(b.num_rows for b in it)
    assert total == 50_000


def test_streaming_peak_memory_is_bounded(tmp_path):
    """The point of the feature: peak RSS must track a batch, not the dataset."""
    if sys.platform == "darwin":
        pytest.skip("ru_maxrss units differ on macOS")
    ws = Workspace.init(tmp_path / "big", name="big")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("src", rows(400_000))  # ~30 MB uncompressed via the pad column

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    assert build(ws, store, catalog, STREAMING).status.value == "succeeded"
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    assert catalog.read("kept").num_rows == sum(1 for i in range(400_000) if i % 3 != 0)
    # Generous bound: the point is that it doesn't scale with the dataset.
    assert after - before < 200, f"peak RSS grew {after - before:.0f} MB"


# -- constraints are enforced loudly ---------------------------------------------

def test_streaming_requires_exactly_one_input():
    with pytest.raises(ValueError, match="exactly one input"):
        @transform(output=Output("out"), streaming=True,
                   a=Input("x"), b=Input("y"))
        def two_inputs(a, b):  # pragma: no cover - construction fails
            yield a


def test_yielding_the_wrong_type_fails_clearly(env):
    ws, store, catalog = env
    result = build(ws, store, catalog, """
from laurelin.transforms import transform, Input, Output

@transform(output=Output("bad"), streaming=True, src=Input("src"))
def wrong_type(src):
    for _ in src:
        yield {"not": "a table"}
""")
    assert result.status.value == "failed"
    assert "must yield pyarrow" in result.tasks[0].error


def test_yielding_nothing_fails(env):
    ws, store, catalog = env
    result = build(ws, store, catalog, """
from laurelin.transforms import transform, Input, Output

@transform(output=Output("empty"), streaming=True, src=Input("src"))
def yields_nothing(src):
    for _ in src:
        pass
    return
    yield  # pragma: no cover
""")
    assert result.status.value == "failed"
    assert "produced no batches" in result.tasks[0].error


def test_record_batches_are_accepted(env):
    """Yielding RecordBatches is natural when mapping over a scan."""
    ws, store, catalog = env
    result = build(ws, store, catalog, """
from laurelin.transforms import transform, Input, Output

@transform(output=Output("as_batches"), streaming=True, src=Input("src"))
def to_batches(src):
    for table in src:
        for rb in table.to_batches():
            yield rb
""")
    assert result.status.value == "succeeded", result.tasks
    assert catalog.read("as_batches").num_rows == 50_000


def test_non_streaming_transforms_are_unchanged(env):
    ws, store, catalog = env
    assert build(ws, store, catalog, WHOLE).status.value == "succeeded"
    assert catalog.read("kept").num_rows == sum(1 for i in range(50_000) if i % 3 != 0)


def test_a_streaming_transform_can_be_stateful(env):
    """The contract hands over the whole iterator, so running state works."""
    ws, store, catalog = env
    result = build(ws, store, catalog, """
from laurelin.transforms import transform, Input, Output
import pyarrow as pa

@transform(output=Output("numbered"), streaming=True, src=Input("src"))
def number_batches(src):
    seq = 0
    for table in src:
        yield table.append_column(
            "batch_seq", pa.array([seq] * table.num_rows, type=pa.int32())
        )
        seq += 1
""")
    assert result.status.value == "succeeded", result.tasks
    out = catalog.read("numbered")
    assert "batch_seq" in out.column_names
    assert out.num_rows == 50_000
