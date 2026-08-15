"""Read/write access to a workspace's pipeline files (``pipelines/*.py``).

This backs in-browser transform authoring. Writing a pipeline file is
**code-execution-equivalent**: the file is ``exec``'d during every build and
metadata collection. Access to these operations is therefore gated at
``editor`` and can be disabled entirely (``--lock-pipelines`` /
``LAURELIN_LOCK_PIPELINES``) for hardened multi-tenant deployments. Nothing
here sandboxes the executed code; that is a separate (roadmap) concern.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from laurelin.core.failure import Failure, FailureCode, Phase
from laurelin.transforms.api import (
    PipelineError,
    TransformRegistry,
    collect_transforms,
    use_registry,
)

_FILE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_module_name(name: str) -> str:
    """Accept a bare module name (``aviation``) or a ``.py`` filename; return the
    bare name. Rejects anything with path separators, dots, or bad characters —
    no traversal outside ``pipelines/``."""
    stem = name[:-3] if name.endswith(".py") else name
    if not _FILE_RE.match(stem):
        raise ValueError(
            f"Invalid pipeline name {name!r}: must match ^[a-z][a-z0-9_]*$ (a "
            "single module name, no paths or dots)"
        )
    return stem


def _collect_one(path: Path) -> tuple[list[str], Optional[Failure]]:
    """Return (transform names declared in this file, Failure or None).

    R1: this used to return ``f"{type(exc).__name__}: {exc}"``. The file is
    ``exec``-ed, so that string is whatever an arbitrary library chose to say
    while a pipeline imported it — and it was served on a VIEWER-gated route.
    """
    registry = TransformRegistry()
    try:
        with use_registry(registry):
            namespace = {
                "__name__": f"laurelin_pipeline_probe.{path.stem}",
                "__file__": str(path),
                "__builtins__": __builtins__,
            }
            exec(compile(path.read_text(), str(path), "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller
        return ([], Failure.from_exception(
            exc, code=FailureCode.TRANSFORM_FAILED, phase=Phase.compile,
            subject=f"pipeline:{path.stem}", driver="python",
        ))
    return ([s.name for s in registry.all()], None)


class PipelineFiles:
    def __init__(self, pipelines_dir: Path):
        self.dir = Path(pipelines_dir)

    def _path(self, name: str) -> Path:
        return self.dir / f"{_validate_module_name(name)}.py"

    def list(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("*.py")):
            if path.name.startswith("."):
                continue  # never surface leaked temp / hidden files
            transforms, _failure = _collect_one(path)
            # The listing drops the failure entirely, for everyone. A pipeline
            # that will not import is an *authoring* fact; the detail belongs on
            # the editor-gated detail route (`read`), not on a list one level
            # down. `failed` is a boolean, which is as much as a list needs.
            out.append(
                {
                    "name": path.stem,
                    "transforms": transforms,
                    "failed": _failure is not None,
                    "bytes": path.stat().st_size,
                }
            )
        return out

    def read(self, name: str) -> dict:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Pipeline file not found: {name!r}")
        _transforms, failure = _collect_one(path)
        return {
            "name": path.stem,
            "content": path.read_text(),
            "failure": failure.as_dict() if failure else None,
        }

    def write(self, name: str, content: str) -> dict:
        """Validate and (atomically) write a pipeline file.

        Raises ValueError (→ 400) on a syntax error, without writing. On a
        successful write, returns the transforms this file declares plus any
        cross-file collection error (e.g. a duplicate output) as a non-fatal
        ``collect_error`` so the editor can surface it without losing work.
        """
        path = self._path(name)
        try:
            compile(content, str(path), "exec")
        except SyntaxError as exc:
            raise ValueError(f"Syntax error: {exc}") from None

        self.dir.mkdir(parents=True, exist_ok=True)
        # Temp suffix is deliberately NOT ".py": if a hard crash leaks the temp
        # file before the rename, it must never be picked up by the "*.py"
        # collection glob and exec'd. collect_transforms also skips dotfiles.
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{path.stem}-", suffix=".py.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        transforms, own_error = _collect_one(path)
        collect_error: Optional[Failure] = own_error
        if collect_error is None:
            try:
                collect_transforms(self.dir)
            except PipelineError as exc:
                # PipelineError is Laurelin's own class, but its *message* is
                # only first-party on some branches: a duplicate output or a
                # cycle is our sentence, while a file that will not import
                # produces `f"...{type(exc).__name__}: {exc}"` over whatever an
                # arbitrary library raised, plus the server's absolute path.
                # Which is why it goes through `from_exception` like any other
                # third-party failure — the message is logged and not stored.
                collect_error = Failure.from_exception(
                    exc, code=FailureCode.TRANSFORM_FAILED, phase=Phase.compile,
                    subject=f"pipeline:{path.stem}", driver="python",
                )
        return {
            "name": path.stem,
            "transforms": transforms,
            "collect_error": collect_error.as_dict() if collect_error else None,
        }

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Pipeline file not found: {name!r}")
        path.unlink()

    def exists_py(self, name: str) -> bool:
        """Whether a Python pipeline of this name exists.

        Flow ejection asks before writing: a `.py` and a `.flow.json` of the
        same name would both register a transform producing the same dataset,
        and `TransformRegistry.register` refuses that — 409-ing every route
        that collects the registry, not just the two files involved.
        """
        return self._path(name).exists()

    def generate_sql_transform(
        self,
        sql: str,
        output: str,
        dataset_names: list[str],
        name: Optional[str] = None,
        inputs: Optional[list[str]] = None,
        params: Optional[list] = None,
    ) -> dict:
        """Create a new pipeline file wrapping ``sql`` as a ``@sql_transform``
        producing ``output``. Returns the created file name.

        ``inputs``, when given, is authoritative and the regex below is not
        run. Flow ejection supplies it from the IR, where the set of source
        datasets is a structural fact rather than a guess. The regex path
        remains for the workbench's "save this query as a transform", where
        there is no IR to ask — and it *is* a guess: a word-boundary match
        cannot tell a table name from the same word inside a string literal or
        a column alias. Fixing that means parsing arbitrary SQL, which is a
        separate project; the caller is at least now required to pass a list
        already filtered to what its author may view.
        """
        out = _validate_module_name(output)  # output is also the transform/func name
        module = _validate_module_name(name) if name else out
        path = self._path(module)
        if path.exists():
            raise ValueError(f"Pipeline file {module!r} already exists")

        if inputs is None:
            inputs = [
                ds
                for ds in sorted(set(dataset_names))
                if ds != out and re.search(rf"\b{re.escape(ds)}\b", sql)
            ]
        inputs_src = ", ".join(f'"{ds}": Input("{ds}")' for ds in sorted(set(inputs)))
        params_src, needs_datetime = _py_params(params or [])
        content = (
            ("import datetime\n\n" if needs_datetime else "")
            + "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
            f"@sql_transform(\n"
            f'    output=Output("{out}"),\n'
            f"    inputs={{{inputs_src}}},\n"
            f"    query={_py_string(sql.strip())},\n"
            + (f"    params={params_src},\n" if params else "")
            + f")\n"
            f"def {out}():\n"
            f"    ...\n"
        )
        return self.write(module, content) | {"name": module}


def _py_params(params: list) -> tuple[str, bool]:
    """Render bound parameter values as a Python list literal.

    Returns the source and whether the file needs ``import datetime``.

    Why ``repr`` and not a hand-rolled renderer: the same reason ``_py_string``
    below uses it. ``repr`` of a ``str``/``int``/``float``/``bool``/``None``/
    ``date``/``datetime`` is a valid Python literal that evaluates back to an
    equal object, for every value including the ones that broke the old SQL
    interpolation (quotes, triple quotes, backslashes). Nothing here is a
    *SQL* renderer — these values never enter query text, they are handed to
    ``con.execute(query, params)`` — so there is no escaping question to get
    wrong, only a Python-source one, and ``repr`` answers that one exactly.

    The type list is closed and matches ``flow_ir.LIT_TYPES``: anything else
    raises rather than being rendered as something that might not parse.
    """
    needs_datetime = False
    for value in params:
        if isinstance(value, _dt.datetime) or isinstance(value, _dt.date):
            needs_datetime = True
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError(
                f"Cannot write a bound value of type {type(value).__name__} "
                "into a pipeline file."
            )
    return "[" + ", ".join(repr(v) for v in params) + "]", needs_datetime


def _py_string(sql: str) -> str:
    """Render ``sql`` as a Python string expression that round-trips byte-exactly.

    WHY not an f-string into a triple-quoted literal, which is what this
    replaced: two measured defects on this tree, one loud and one silent.

    * **loud** — ``generate_sql_transform('SELECT \"\"\" FROM raw', 'outa',
      ['raw'])`` raised ``ValueError: Syntax error: unterminated triple-quoted
      string literal (detected at line 12) (outa.py, line 9)``. An author who
      wrote SQL was shown a *Python* line number for a file they never saw.

    * **silent, and worse** — SQL containing a backslash was CORRUPTED with no
      error at any layer. Authored
      ``SELECT * FROM raw WHERE path = 'C:\\temp\\new' AND re = '\\d+'``; after
      write plus ``collect_transforms`` the stored query held a real TAB (from
      ``\\t``) and a real NEWLINE (from ``\\n``). ``IDENTICAL: False``. The
      build ran SQL the author never wrote, and every layer reported success.

    ``write()``'s compile-before-save guard catches the first and *cannot*
    catch the second — the corrupted file is perfectly valid Python. That
    asymmetry is why this is a correctness fix and not an ergonomics one.

    ``repr()`` emits a valid Python literal for every input (verified over
    triple quotes, mixed quotes, backslashes, NUL and ESC), and rendering
    per-line keeps the query readable in the CodeMirror editor rather than
    collapsing a 30-line query onto one escaped line::

        query=(
            'SELECT a\\n'
            'FROM r\\n'
            "WHERE b = 'C:\\\\temp'"
        ),
    """
    lines = sql.split("\n")
    body = "\n".join(
        "        " + repr(line + ("\n" if i < len(lines) - 1 else ""))
        for i, line in enumerate(lines)
    )
    return "(\n" + body + "\n    )"
