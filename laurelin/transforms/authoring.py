"""Read/write access to a workspace's pipeline files (``pipelines/*.py``).

This backs in-browser transform authoring. Writing a pipeline file is
**code-execution-equivalent**: the file is ``exec``'d during every build and
metadata collection. Access to these operations is therefore gated at
``editor`` and can be disabled entirely (``--lock-pipelines`` /
``LAURELIN_LOCK_PIPELINES``) for hardened multi-tenant deployments. Nothing
here sandboxes the executed code; that is a separate (roadmap) concern.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

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


def _collect_one(path: Path) -> tuple[list[str], Optional[str]]:
    """Return (transform names declared in this file, error string or None)."""
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
        return ([], f"{type(exc).__name__}: {exc}")
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
            transforms, error = _collect_one(path)
            out.append(
                {
                    "name": path.stem,
                    "transforms": transforms,
                    "error": error,
                    "bytes": path.stat().st_size,
                }
            )
        return out

    def read(self, name: str) -> dict:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Pipeline file not found: {name!r}")
        return {"name": path.stem, "content": path.read_text()}

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
        collect_error = own_error
        if collect_error is None:
            try:
                collect_transforms(self.dir)
            except PipelineError as exc:
                collect_error = str(exc)
        return {"name": path.stem, "transforms": transforms, "collect_error": collect_error}

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Pipeline file not found: {name!r}")
        path.unlink()

    def generate_sql_transform(
        self, sql: str, output: str, dataset_names: list[str], name: Optional[str] = None
    ) -> dict:
        """Create a new pipeline file wrapping ``sql`` as a ``@sql_transform``
        producing ``output``. Inputs are the datasets whose names appear in the
        query (excluding the output itself). Returns the created file name."""
        out = _validate_module_name(output)  # output is also the transform/func name
        module = _validate_module_name(name) if name else out
        path = self._path(module)
        if path.exists():
            raise ValueError(f"Pipeline file {module!r} already exists")

        inputs = [
            ds
            for ds in sorted(set(dataset_names))
            if ds != out and re.search(rf"\b{re.escape(ds)}\b", sql)
        ]
        inputs_src = ", ".join(f'"{ds}": Input("{ds}")' for ds in inputs)
        content = (
            "from laurelin.transforms import sql_transform, Input, Output\n\n\n"
            f"@sql_transform(\n"
            f'    output=Output("{out}"),\n'
            f"    inputs={{{inputs_src}}},\n"
            f'    query="""\n{sql.strip()}\n""",\n'
            f")\n"
            f"def {out}():\n"
            f"    ...\n"
        )
        return self.write(module, content) | {"name": module}
