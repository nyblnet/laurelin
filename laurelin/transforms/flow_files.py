"""Read/write access to a workspace's flow files (``pipelines/*.flow.json``).

Flows live in ``pipelines/`` **as files**, next to the Python pipelines, rather
than as rows in ``metadata.db``. That is a deliberate reversal of the earlier
recommendation, and the reason is that a file in this directory inherits four
governance surfaces for nothing:

===================  ==========================================================
Workspace export     ``writer.py`` already tars every file in ``pipelines/``
                     whose suffix is in a tuple. One entry added.
Workspace import     ``reader.py``: the same tuple.
Credential scanning  ``pipeline_scan.py``: the same tuple, and the scanner
                     already walks whole file bytes rather than named columns.
Acknowledgement      Free. The imported-pipelines gate wraps
                     ``collect_transforms``, and flow collection lives *inside*
                     it.
===================  ==========================================================

A metadata table would have had to re-derive every one of those, including
picking which JSON columns get scanned for credentials — and omitting one
silently regresses coverage. Files are also git-diffable.

Unlike a ``.py`` pipeline, a ``.flow.json`` is **never executed**.
``collect_transforms`` globs ``*.py`` for the ``exec`` path and ``*.flow.json``
for this one, and this one only ever parses JSON. That is the point: a flow
author does not get code execution, which is the entire security premise of the
no-code builder.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from laurelin.transforms.flow_ir import FLOW_SUFFIX, FlowDef, FlowRefused, validate_flow_name

#: File suffixes that make up a workspace's pipeline directory, for export,
#: import and credential scanning. ``Path("x.flow.json").suffix`` is ``".json"``
#: — not ``".flow.json"`` — which is why this is the tuple those call sites
#: need rather than ``FLOW_SUFFIX`` itself.
PIPELINE_FILE_SUFFIXES: tuple[str, ...] = (".py", ".json")


def flow_paths(pipelines_dir: Path) -> list[Path]:
    """Every flow file in the directory, sorted, skipping dotfiles.

    Dotfiles are skipped for the same reason ``collect_transforms`` skips them:
    an interrupted atomic write can leak a temp file, and a leaked temp must
    never be collected as a real flow.
    """
    pipelines_dir = Path(pipelines_dir)
    if not pipelines_dir.is_dir():
        return []
    return sorted(
        p for p in pipelines_dir.glob(f"*{FLOW_SUFFIX}")
        if not p.name.startswith(".")
    )


def _stem(path: Path) -> str:
    """``/w/pipelines/orders.flow.json`` -> ``orders``.

    ``Path.stem`` gives ``orders.flow``; the flow's name is the whole thing
    before ``.flow.json``, because flow name == transform name == output
    dataset name and that has to be a legal dataset name.
    """
    return path.name[: -len(FLOW_SUFFIX)]


def load_flow(path: Path) -> FlowDef:
    """Parse and Tier-A-validate one flow file.

    The filename is authoritative for the flow's name: a file whose body claims
    a different name would otherwise register a transform producing a dataset
    that no file appears to own.
    """
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        # Our sentence, about our file format, with no path in it. `exc.lineno`
        # is a position in a JSON document the author's client wrote, not a
        # Python traceback, so it is useful rather than confusing.
        raise FlowRefused(
            f"Flow file {_stem(path)!r} is not valid JSON (line {exc.lineno})."
        ) from None
    return FlowDef.from_json(raw, name=_stem(path))


def collect_flows(pipelines_dir: Path) -> list[FlowDef]:
    """Every flow in the directory, in filename order. Tier A only.

    Tier B (schema binding) is *not* run here. This is called on every registry
    collection, which happens per API request, and resolving a source-scanned
    dataset's schema hits a remote system. A flow whose upstream schema has
    drifted therefore fails its **build**, loudly, rather than making every
    request to the workspace pay a round trip.
    """
    return [load_flow(p) for p in flow_paths(pipelines_dir)]


class FlowFiles:
    """CRUD over ``pipelines/*.flow.json``."""

    def __init__(self, pipelines_dir: Path):
        self.dir = Path(pipelines_dir)

    def _path(self, name: str) -> Path:
        # validate_flow_name rejects anything with a path separator, a dot or
        # an uppercase letter, so there is no traversal out of pipelines/.
        return self.dir / f"{validate_flow_name(name)}{FLOW_SUFFIX}"

    def list(self) -> list[dict]:
        out = []
        for path in flow_paths(self.dir):
            name = _stem(path)
            try:
                flow = load_flow(path)
            except FlowRefused:
                # A listing reports *that* a flow is broken, not how. Same
                # split as PipelineFiles.list: the detail belongs on the
                # editor-gated detail route, and `failed` is as much as a list
                # needs.
                out.append({
                    "name": name, "output": name, "sources": [], "nodes": 0,
                    "author": "", "description": "", "failed": True,
                })
                continue
            out.append({
                "name": flow.name,
                "output": flow.output,
                "sources": flow.source_datasets(),
                "nodes": len(flow.nodes),
                "author": flow.author,
                "description": flow.description,
                "failed": False,
            })
        return out

    def read(self, name: str) -> dict:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Flow not found: {name!r}")
        try:
            flow = load_flow(path)
        except FlowRefused as exc:
            # `error` rather than a `Failure`: FlowRefused's message is
            # first-party by construction (see its docstring) and naming the
            # offending step is the entire value of reading a broken flow.
            # `Failure` deliberately carries no message because it wraps
            # *third-party* text; there is none here.
            #
            # `flow` is echoed back only if the file is still parseable JSON,
            # so the builder can open a structurally-invalid flow and repair it
            # rather than being locked out of its own file.
            return {
                "name": name,
                "flow": _json_or_none(path),
                "error": str(exc),
                "node": exc.node,
            }
        return {"name": flow.name, "flow": flow.as_json(), "error": None, "node": ""}

    def write(self, name: str, raw: dict, author: str) -> FlowDef:
        """Validate (Tier A) and atomically write a flow. Raises FlowRefused.

        Validation happens **before** the write, so a flow that will not
        compile is refused rather than saved — the same guard
        ``PipelineFiles.write`` applies by compiling Python before saving, and
        for the same reason.
        """
        body = dict(raw or {})
        # The author is recorded by the server from the authenticated caller,
        # never taken from the body: the recorded author is whose read access
        # the *build* is checked against, so a client that could set it could
        # pick whose rights to borrow.
        body["author"] = author
        flow = FlowDef.from_json(body, name=name)

        path = self._path(name)
        self.dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(flow.as_json(), indent=2, sort_keys=False) + "\n"
        # Temp suffix is deliberately NOT ".flow.json", and the name starts
        # with a dot: a temp leaked by a hard crash must not be picked up by
        # the collection glob.
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{name}-", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return flow

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Flow not found: {name!r}")
        path.unlink()

    def exists(self, name: str) -> bool:
        return self._path(name).exists()


def _json_or_none(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - the caller only wants "can I echo this"
        return None


def flow_spec(flow: FlowDef):
    """Turn a validated flow into a ``TransformSpec`` for the one build path.

    Imported lazily by ``collect_transforms``. ``inputs`` are derived from the
    IR's ``source`` nodes — structurally, never by scanning generated SQL — and
    the alias is the dataset name, which is what lets a flow's preview and its
    build execute the same statement: ``Builder`` registers inputs by alias and
    ``catalog.query`` registers datasets by name, so the two coincide.
    """
    from laurelin.transforms.api import Input, Output, TransformSpec

    return TransformSpec(
        name=flow.name,
        output=Output(flow.output, description=flow.description),
        inputs={ds: Input(ds) for ds in flow.source_datasets()},
        kind="flow",
        flow=flow,
    )


def eject_sql(flow: FlowDef, catalog) -> tuple[str, list[str], list]:
    """Compiled SQL, IR-derived inputs and bound values, for ``POST /flows/{name}/eject``.

    Ejecting is a **one-way door**: it writes ``pipelines/{name}.py`` and
    deletes the flow, and the visual builder cannot reopen it. Import (Python →
    flow) is permanently out of scope for v1, because that direction is where
    the silent-overwrite data loss lives.

    The inputs come from the IR rather than from
    ``generate_sql_transform``'s word-boundary regex over the SQL text, which
    cannot tell a table name from a string literal.

    The values come out **still bound**. This used to refuse outright — "a
    Python pipeline has no way to carry them separately from the query text" —
    which was true of `TransformSpec` at the time and excluded every flow
    containing a single filter constant, on a feature whose defining operation
    is filtering. Measured: `source clean_flights -> filter status is not
    'cancelled'`, about as ordinary as a pipeline gets, could not be ejected,
    and the only explanation was a tooltip on a disabled menu item. Giving
    `sql_transform` a `params` list was a smaller change than that exclusion
    was a product hole, and it makes the *Python* path bind its values too.
    """
    compiled = compile_flow_now(catalog, flow)
    return compiled.sql, compiled.inputs, compiled.params


def flow_schemas(catalog, flow: FlowDef) -> dict[str, list[str]]:
    """The live schema of every dataset this flow reads.

    This mapping is what makes an identifier legal (``flow_compile``), so it is
    rebuilt immediately before each compile rather than cached: a schema
    captured at authoring time and trusted at build time is a schema that can
    have drifted.
    """
    return {ds: catalog.column_names(ds) for ds in flow.source_datasets()}


def flow_column_kinds(catalog, flow: FlowDef) -> dict[str, dict[str, str]]:
    """The coarse type of every column this flow can read, per source dataset.

    Rebuilt alongside ``flow_schemas`` and for the same reason: a type captured
    at authoring time is a type that can have drifted, and the check this feeds
    (``flow_compile._check_expr_types``) is only worth having if it is checking
    what the data holds now.
    """
    from laurelin.transforms.flow_compile import kind_of

    out: dict[str, dict[str, str]] = {}
    for ds in flow.source_datasets():
        out[ds] = {
            name: kind_of(arrow_type)
            for name, arrow_type in catalog.column_types(ds).items()
        }
    return out


def compile_flow_now(catalog, flow: FlowDef, **kwargs):
    """Compile ``flow`` against the schema *and* the types it has right now.

    One function so the three callers that must agree — ``PUT /flows/{name}``,
    ``POST /flows/preview`` and ``Builder._execute_flow`` — cannot end up
    compiling with different amounts of information. A flow that saves because
    the save path skipped the type check, and then fails its build because the
    build path did not, is the exact experience this feature exists to remove.
    """
    from laurelin.transforms.flow_compile import compile_flow

    return compile_flow(
        flow, flow_schemas(catalog, flow),
        column_kinds=flow_column_kinds(catalog, flow), **kwargs,
    )


__all__ = [
    "FlowFiles",
    "PIPELINE_FILE_SUFFIXES",
    "collect_flows",
    "compile_flow_now",
    "eject_sql",
    "flow_column_kinds",
    "flow_paths",
    "flow_schemas",
    "flow_spec",
    "load_flow",
]
