"""Transform declaration API: decorators, specs, registry, pipeline collection.

Pipeline files in a workspace's `pipelines/` directory declare transforms with
`@transform` / `@sql_transform`. `collect_transforms` executes those files with
an *active registry* installed so the decorators register into it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional


@dataclass
class Input:
    dataset: str


@dataclass
class Output:
    dataset: str
    description: str = ""


@dataclass
class TransformSpec:
    name: str
    output: Output
    inputs: dict[str, Input] = field(default_factory=dict)
    kind: str = "python"  # "python" | "sql" | "remote" | "flow"
    # remote: the engine that executes `query`. Laurelin submits it and stores
    # the (reduced) result; the cluster does the work.
    engine: Optional[str] = None
    fn: Optional[Callable] = None  # python: fn(**{param: pa.Table}) -> pa.Table
    query: Optional[str] = None  # sql: SELECT over input aliases as table names
    # sql: values bound positionally to `query`'s `?` placeholders.
    #
    # Added so that ejecting a flow is possible at all. A compiled flow puts
    # every author-supplied value in this list and none in the text; before
    # this field existed, `eject_sql` had nowhere to put them and refused every
    # flow containing a single filter constant — which is to say, every flow
    # that filters anything, on a feature whose defining operation is
    # filtering. The alternative, rendering the values into the query text, is
    # the one thing this whole feature is built not to do.
    params: list = field(default_factory=list)
    # flow: the declarative IR a no-code author built (laurelin.transforms.
    # flow_ir.FlowDef). It is compiled to SQL + bound parameters by the Builder
    # immediately before execution — not here, and not at collection time,
    # because compilation needs the *live* schema of every input and a bare
    # registry collection happens on every API request.
    #
    # `query` stays None for a flow. There is deliberately no place to cache
    # compiled text on the spec: a cached statement is one that can be executed
    # against a schema it was not checked against.
    flow: Optional[object] = None
    # Streaming python transforms receive an *iterator* of pa.Table batches for
    # their single input and yield pa.Tables, so neither the input nor the
    # output is ever held whole in memory.
    streaming: bool = False
    # Incremental transforms see only rows added since their last successful
    # build, and their output is appended rather than replaced.
    incremental: bool = False
    # Assertions the output must satisfy, checked against the written Parquet
    # parts *before* the version is committed — see laurelin.transforms.expectations.
    expectations: list = field(default_factory=list)
    # The stem of the pipelines/*.py file this spec was collected from, set by
    # `_collect_python` and by nothing else. It is the key the Builder uses to
    # look up the file's API-recorded author (`pipeline_authors`) for the
    # build-time input-entitlement check. None for flow specs (a flow carries
    # its own author) and for in-memory registries, which are operator code.
    source_file: Optional[str] = None


class TransformRegistry:
    """Named collection of transform specs, indexed by name and output dataset."""

    def __init__(self) -> None:
        self._by_name: dict[str, TransformSpec] = {}
        self._by_output: dict[str, TransformSpec] = {}

    def register(self, spec: TransformSpec) -> None:
        if (spec.incremental or spec.streaming) and spec.kind != "python":
            # Measured on this tree: a spec with `incremental=True` and
            # `kind="sql"` reaches `builder.py`'s `result = spec.fn(**{param:
            # delta})` with `fn=None` and dies as
            # `TypeError: 'NoneType' object is not callable` — a build-time
            # crash with no useful sentence in it.
            #
            # The decorators cannot produce that combination (`@sql_transform`
            # takes no such argument), so it was unreachable until specs could
            # come from somewhere other than a decorator. Enforced here rather
            # than in the flow loader so it holds for *every* producer, present
            # and future.
            raise ValueError(
                f"Transform {spec.name!r} is kind={spec.kind!r}; incremental "
                f"and streaming require a python transform. Both are "
                f"single-input and row-wise by construction — see the arity "
                f"checks in `transform()` above."
            )
        if spec.name in self._by_name:
            raise ValueError(f"Duplicate transform name: {spec.name!r}")
        existing = self._by_output.get(spec.output.dataset)
        if existing is not None:
            raise ValueError(
                f"Dataset {spec.output.dataset!r} is produced by both "
                f"{existing.name!r} and {spec.name!r}; each dataset may have "
                "only one producing transform"
            )
        self._by_name[spec.name] = spec
        self._by_output[spec.output.dataset] = spec

    def get(self, name: str) -> TransformSpec:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"No transform named {name!r}") from None

    def all(self) -> list[TransformSpec]:
        return list(self._by_name.values())

    def by_output(self, dataset: str) -> Optional[TransformSpec]:
        return self._by_output.get(dataset)


# ---------------------------------------------------------------------------
# Active registry: decorators register into whichever registry is active.
# ---------------------------------------------------------------------------

# ContextVar rather than a module global: the API server collects transforms
# per request from a threadpool, and concurrent collections must not see each
# other's registries.
_active_registry: ContextVar[Optional[TransformRegistry]] = ContextVar(
    "laurelin_active_registry", default=None
)


@contextmanager
def use_registry(registry: TransformRegistry) -> Iterator[TransformRegistry]:
    """Make `registry` the active target for @transform / @sql_transform."""
    token = _active_registry.set(registry)
    try:
        yield registry
    finally:
        _active_registry.reset(token)


def _register(spec: TransformSpec) -> None:
    registry = _active_registry.get()
    if registry is not None:
        registry.register(spec)


def transform(
    output: Output,
    streaming: bool = False,
    incremental: bool = False,
    **inputs: Input,
) -> Callable[[Callable], Callable]:
    """Declare a python transform.

    Default: ``fn(**{param: pa.Table}) -> pa.Table``. Each input arrives as one
    in-memory table, so peak memory is roughly the inputs plus the output —
    convenient, and fine up to datasets that comfortably fit in RAM.

    ``streaming=True``: ``fn(param=Iterator[pa.Table]) -> Iterator[pa.Table]``.
    Batches are pulled from the input scan and written out as they are
    produced, so memory tracks one batch rather than the dataset. Requires
    exactly one input — two independent streams have no meaningful alignment,
    and pretending otherwise would silently produce wrong results. Aggregations
    need all the data anyway; express those as SQL transforms, which DuckDB
    streams and spills natively.

    ``incremental=True``: the transform is passed only the rows its input has
    gained since the last successful build, and its result is **appended** to
    the output rather than replacing it. Reprocessing yesterday's rows to
    produce yesterday's answers again is the most common waste in a pipeline;
    this removes it. The first build, or one after the input is rewritten
    rather than appended to, processes everything.

    Incremental is row-wise by construction: it also requires exactly one
    input, and the function must not depend on rows outside its batch.
    """

    def decorator(fn: Callable) -> Callable:
        if incremental and len(inputs) != 1:
            raise ValueError(
                f"Incremental transform {fn.__name__!r} must declare exactly one "
                f"input (got {len(inputs)}) — the delta of several inputs has no "
                f"single meaning."
            )
        if streaming and len(inputs) != 1:
            raise ValueError(
                f"Streaming transform {fn.__name__!r} must declare exactly one "
                f"input (got {len(inputs)}). Use a SQL transform to combine "
                f"several inputs, or a non-streaming transform."
            )
        spec = TransformSpec(
            name=fn.__name__,
            output=output,
            inputs=dict(inputs),
            kind="python",
            fn=fn,
            query=None,
            streaming=streaming,
            incremental=incremental,
        )
        fn.__transform_spec__ = spec  # type: ignore[attr-defined]
        _register(spec)
        return fn

    return decorator


def sql_transform(
    output: Output, inputs: dict[str, Input], query: str,
    params: Optional[list] = None,
) -> Callable[[Callable], Callable]:
    """Declare a SQL transform. The decorated function body is ignored; the
    query runs in DuckDB with each input alias registered as a table name.

    ``params`` are bound positionally to the ``?`` placeholders in ``query``.
    Use them for every value: an interpolated value is a value the database
    may re-read as syntax, and a bound one never is. An ejected flow is written
    this way, which is the only reason ejecting a flow with a filter in it is
    possible at all.
    """

    def decorator(fn: Callable) -> Callable:
        spec = TransformSpec(
            name=fn.__name__,
            output=output,
            inputs=dict(inputs),
            kind="sql",
            fn=None,
            query=query,
            params=list(params or []),
        )
        fn.__transform_spec__ = spec  # type: ignore[attr-defined]
        _register(spec)
        return fn

    return decorator


def remote_transform(
    output: Output, engine: str, query: str
) -> Callable[[Callable], Callable]:
    """Declare a transform executed by a remote engine.

    The query runs on the engine's own cluster — Trino, Dremio, Databricks,
    anything speaking Flight SQL — and Laurelin stores what comes back as an
    ordinary managed dataset, with lineage, markings and ACLs like any other.

    This is how large data is handled without Laurelin owning a distributed
    engine: the cluster does the reduction, Laurelin governs and keeps the
    result. Aggregate on the engine; a query returning millions of rows has
    not reduced anything and will be refused.

    Inputs are not declared, because the query addresses tables in the
    engine's catalog rather than Laurelin datasets. Lineage records the engine
    as the upstream.
    """

    def decorator(fn: Callable) -> Callable:
        if not engine or not str(engine).strip():
            raise ValueError(
                f"Remote transform {fn.__name__!r} must name an engine"
            )
        if not query or not str(query).strip():
            raise ValueError(f"Remote transform {fn.__name__!r} needs a query")
        spec = TransformSpec(
            name=fn.__name__,
            output=output,
            inputs={},
            kind="remote",
            fn=None,
            query=query,
            engine=str(engine),
        )
        fn.__transform_spec__ = spec  # type: ignore[attr-defined]
        _register(spec)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Pipeline collection
# ---------------------------------------------------------------------------

class PipelineError(RuntimeError):
    """A pipeline file failed to execute during collection.

    ``pipeline`` names the offending file — a Laurelin identifier, not a path
    and not a driver's sentence — so a caller can say *which* file will not
    import without repeating ``str(exc)``.

    That distinction is load-bearing under R1. The message of this exception is
    **not** first-party text on the import branch: ``collect_transforms``
    interpolates ``{type(exc).__name__}: {exc}`` from whatever an arbitrary
    library raised while the file was being ``exec``-ed, plus the server's
    absolute path. It is fine in a log and fine as the input to
    ``Failure.from_exception`` (which stores none of it). It must not be put in
    a response body.
    """

    def __init__(self, message: str, pipeline: str = ""):
        super().__init__(message)
        self.pipeline = pipeline


def collect_transforms(pipelines_dir: Path) -> TransformRegistry:
    """Execute every ``*.py`` in `pipelines_dir` (sorted) into a fresh registry,
    then add every ``*.flow.json`` to the **same** registry.

    Each Python file runs in its own module namespace with the returned
    registry active, so `@transform` / `@sql_transform` decorators register into
    it. Flow files are *not* executed — they are parsed as JSON and turned into
    specs (see `laurelin.transforms.flow_files`).

    One registry for both producers is the whole design of the no-code builder.
    Everything downstream is keyed on `spec.name` / `spec.output.dataset` and
    therefore covers flows without knowing they exist: duplicate-name and
    duplicate-output detection (`register` below), planning and cycle detection
    (`Builder.plan`), lineage, marking recompute, expectations-before-publish,
    build leases, object-index refresh, scheduler targets, and the
    imported-pipelines acknowledgement gate, which wraps this function.

    A second execution path for no-code pipelines would mean re-proving every
    one of those properties, and on this codebase's evidence it would fail
    somewhere nobody checked.
    """
    registry = TransformRegistry()
    pipelines_dir = Path(pipelines_dir)
    if not pipelines_dir.is_dir():
        return registry
    _collect_python(pipelines_dir, registry)
    _collect_flow_files(pipelines_dir, registry)
    return registry


def _collect_python(pipelines_dir: Path, registry: TransformRegistry) -> None:
    with use_registry(registry):
        # Skip dotfiles so a leaked ".<name>-*.py.tmp" style temp from an
        # interrupted write is never compiled/exec'd.
        paths = [p for p in sorted(pipelines_dir.glob("*.py")) if not p.name.startswith(".")]
        for index, path in enumerate(paths):
            namespace = {
                "__name__": f"laurelin_pipelines.{index}_{path.stem}",
                "__file__": str(path),
                "__builtins__": __builtins__,
            }
            before = len(registry.all())
            try:
                code = compile(path.read_text(), str(path), "exec")
                exec(code, namespace)
            except Exception as exc:
                raise PipelineError(
                    f"Error in pipeline file {path}: {type(exc).__name__}: {exc}",
                    pipeline=path.stem,
                ) from exc
            # Attribute every spec this file just registered to the file, so
            # the Builder can resolve its API-recorded author. The delta is
            # exactly this file's specs: `register` is the registry's only
            # mutation point and dict insertion order is stable.
            for spec in registry.all()[before:]:
                spec.source_file = path.stem


def _collect_flow_files(pipelines_dir: Path, registry: TransformRegistry) -> None:
    """Add every ``*.flow.json`` to `registry`, Tier A only.

    Imported here rather than at module scope so `flow_files` (and through it
    `flow_ir`) is not a hard import cost on every consumer of this module.

    A malformed flow raises `PipelineError`, exactly as a `.py` that will not
    import does. Skipping it instead would make the dataset it produces
    silently vanish from the plan, and "the build stopped producing this table
    and nothing said so" is the failure this codebase is least able to notice.
    """
    from laurelin.transforms.flow_files import _stem, flow_paths, flow_spec, load_flow

    for path in flow_paths(pipelines_dir):
        try:
            registry.register(flow_spec(load_flow(path)))
        except Exception as exc:
            # Unlike the `.py` branch, the message here is genuinely
            # first-party: `FlowRefused` and `register`'s duplicate errors are
            # both Laurelin's own sentences, and nothing arbitrary was
            # executed to produce them. It still travels as `PipelineError` so
            # every caller keeps one exception type to catch.
            raise PipelineError(
                f"Error in flow {_stem(path)!r}: {exc}", pipeline=_stem(path),
            ) from exc
