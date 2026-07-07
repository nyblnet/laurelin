"""Transform declaration API: decorators, specs, registry, pipeline collection.

Pipeline files in a workspace's `pipelines/` directory declare transforms with
`@transform` / `@sql_transform`. `collect_transforms` executes those files with
an *active registry* installed so the decorators register into it.
"""

from __future__ import annotations

from contextlib import contextmanager
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
    kind: str = "python"  # "python" | "sql"
    fn: Optional[Callable] = None  # python: fn(**{param: pa.Table}) -> pa.Table
    query: Optional[str] = None  # sql: SELECT over input aliases as table names


class TransformRegistry:
    """Named collection of transform specs, indexed by name and output dataset."""

    def __init__(self) -> None:
        self._by_name: dict[str, TransformSpec] = {}
        self._by_output: dict[str, TransformSpec] = {}

    def register(self, spec: TransformSpec) -> None:
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

_active_registry: Optional[TransformRegistry] = None


@contextmanager
def use_registry(registry: TransformRegistry) -> Iterator[TransformRegistry]:
    """Make `registry` the active target for @transform / @sql_transform."""
    global _active_registry
    previous = _active_registry
    _active_registry = registry
    try:
        yield registry
    finally:
        _active_registry = previous


def _register(spec: TransformSpec) -> None:
    if _active_registry is not None:
        _active_registry.register(spec)


def transform(output: Output, **inputs: Input) -> Callable[[Callable], Callable]:
    """Declare a python transform: fn(**{param: pa.Table}) -> pa.Table."""

    def decorator(fn: Callable) -> Callable:
        spec = TransformSpec(
            name=fn.__name__,
            output=output,
            inputs=dict(inputs),
            kind="python",
            fn=fn,
            query=None,
        )
        fn.__transform_spec__ = spec  # type: ignore[attr-defined]
        _register(spec)
        return fn

    return decorator


def sql_transform(
    output: Output, inputs: dict[str, Input], query: str
) -> Callable[[Callable], Callable]:
    """Declare a SQL transform. The decorated function body is ignored; the
    query runs in DuckDB with each input alias registered as a table name."""

    def decorator(fn: Callable) -> Callable:
        spec = TransformSpec(
            name=fn.__name__,
            output=output,
            inputs=dict(inputs),
            kind="sql",
            fn=None,
            query=query,
        )
        fn.__transform_spec__ = spec  # type: ignore[attr-defined]
        _register(spec)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Pipeline collection
# ---------------------------------------------------------------------------

class PipelineError(RuntimeError):
    """A pipeline file failed to execute during collection."""


def collect_transforms(pipelines_dir: Path) -> TransformRegistry:
    """Execute every ``*.py`` in `pipelines_dir` (sorted) into a fresh registry.

    Each file runs in its own module namespace with the returned registry
    active, so `@transform` / `@sql_transform` decorators register into it.
    """
    registry = TransformRegistry()
    pipelines_dir = Path(pipelines_dir)
    if not pipelines_dir.is_dir():
        return registry
    with use_registry(registry):
        for index, path in enumerate(sorted(pipelines_dir.glob("*.py"))):
            namespace = {
                "__name__": f"laurelin_pipelines.{index}_{path.stem}",
                "__file__": str(path),
                "__builtins__": __builtins__,
            }
            try:
                code = compile(path.read_text(), str(path), "exec")
                exec(code, namespace)
            except Exception as exc:
                raise PipelineError(
                    f"Error in pipeline file {path}: {type(exc).__name__}: {exc}"
                ) from exc
    return registry
