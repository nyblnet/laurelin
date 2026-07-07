"""Laurelin command-line interface (the `laurelin` entry point)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from laurelin.core.config import Workspace, WorkspaceNotFound

app = typer.Typer(
    name="laurelin",
    help="Laurelin: an open, ontology-driven data platform.",
    no_args_is_help=True,
    add_completion=False,
)
datasets_app = typer.Typer(help="Inspect workspace datasets.", no_args_is_help=True)
app.add_typer(datasets_app, name="datasets")

WORKSPACE_OPTION = typer.Option(
    None,
    "--workspace",
    "-w",
    help="Workspace directory (default: $LAURELIN_WORKSPACE or walk up from cwd).",
)


def _find_workspace(path: Optional[Path]) -> Workspace:
    try:
        return Workspace.find(path)
    except WorkspaceNotFound as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


def _open_engine(workspace: Workspace):
    """Store + catalog for a workspace (deferred import keeps startup snappy)."""
    from laurelin.catalog import DatasetCatalog
    from laurelin.core.db import MetadataStore

    store = MetadataStore(workspace.metadata_path)
    return store, DatasetCatalog(workspace, store)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Plain aligned-column table output (no external deps)."""
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    typer.echo(fmt.format(*headers))
    typer.echo(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        typer.echo(fmt.format(*row))


@app.command()
def init(
    path: Path = typer.Argument(..., help="Directory to initialize as a workspace."),
    name: str = typer.Option("", "--name", help="Workspace name."),
    description: str = typer.Option("", "--description", help="Workspace description."),
) -> None:
    """Create a new Laurelin workspace."""
    workspace = Workspace.init(path, name=name, description=description)
    typer.echo(f"Initialized workspace '{workspace.name}' at {workspace.root}")


@app.command()
def serve(
    workspace: Optional[Path] = WORKSPACE_OPTION,
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8787, "--port", help="Bind port."),
) -> None:
    """Run the Laurelin API + UI server."""
    ws = _find_workspace(workspace)
    import uvicorn

    from laurelin.api import create_app

    typer.echo(f"Serving workspace '{ws.name}' ({ws.root}) on http://{host}:{port}")
    uvicorn.run(create_app(ws), host=host, port=port)


@app.command()
def build(
    targets: Optional[list[str]] = typer.Argument(
        None, help="Output dataset names to build (default: everything)."
    ),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Run pipeline transforms and materialize their output datasets."""
    ws = _find_workspace(workspace)
    from laurelin.transforms import Builder, collect_transforms

    store, catalog = _open_engine(ws)
    registry = collect_transforms(ws.pipelines_dir)
    builder = Builder(ws, catalog, store, registry)
    try:
        info = builder.build(list(targets) if targets else None)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Build {info.id}: {info.status.value}")
    _print_table(
        ["TRANSFORM", "OUTPUT", "STATUS", "ROWS", "VERSION"],
        [
            [
                t.transform_name,
                t.output_dataset,
                t.status.value,
                str(t.rows_written) if t.rows_written is not None else "-",
                str(t.output_version) if t.output_version is not None else "-",
            ]
            for t in info.tasks
        ],
    )
    for t in info.tasks:
        if t.error:
            typer.echo(f"  {t.transform_name}: {t.error}", err=True)
    if info.status.value != "succeeded":
        raise typer.Exit(1)


@datasets_app.command("list")
def datasets_list(workspace: Optional[Path] = WORKSPACE_OPTION) -> None:
    """List datasets in the workspace."""
    ws = _find_workspace(workspace)
    store, _ = _open_engine(ws)
    datasets = store.list_datasets()
    if not datasets:
        typer.echo("No datasets in workspace.")
        return
    _print_table(
        ["NAME", "LATEST", "DESCRIPTION"],
        [
            [
                d.name,
                str(d.latest_version) if d.latest_version is not None else "-",
                d.description,
            ]
            for d in datasets
        ],
    )


@datasets_app.command("show")
def datasets_show(
    name: str = typer.Argument(..., help="Dataset name."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
    rows: int = typer.Option(10, "--rows", help="Number of preview rows."),
) -> None:
    """Show a dataset's versions, schema, and a row preview."""
    ws = _find_workspace(workspace)
    store, catalog = _open_engine(ws)
    info = store.get_dataset(name)
    if info is None:
        typer.echo(f"Error: dataset {name!r} not found.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Dataset:     {info.name}")
    if info.description:
        typer.echo(f"Description: {info.description}")
    typer.echo(f"Created:     {info.created_at}")
    latest = info.latest_version
    typer.echo(f"Latest:      {latest if latest is not None else '(no versions)'}")

    versions = store.list_versions(name)
    if not versions:
        return

    typer.echo("\nVersions:")
    _print_table(
        ["VERSION", "ROWS", "SOURCE", "CREATED"],
        [[str(v.version), str(v.row_count), v.source, v.created_at] for v in versions],
    )

    latest_info = versions[-1]
    typer.echo("\nSchema:")
    _print_table(
        ["COLUMN", "TYPE"], [[c.name, c.type] for c in latest_info.schema_]
    )

    if rows > 0:
        preview = catalog.rows(name, limit=rows)
        if preview:
            columns = list(preview[0].keys())
            typer.echo(f"\nFirst {len(preview)} rows:")
            _print_table(
                columns,
                [[str(r.get(c)) if r.get(c) is not None else "" for c in columns] for r in preview],
            )


@app.command()
def upload(
    name: str = typer.Argument(..., help="Target dataset name."),
    file: Path = typer.Argument(..., help="CSV or Parquet file to ingest."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Upload a CSV or Parquet file as a new dataset version."""
    ws = _find_workspace(workspace)
    store, catalog = _open_engine(ws)
    try:
        version = catalog.upload_file(name, file)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    store.log_audit(
        "dataset_uploaded",
        {"dataset": name, "version": version.version, "file": str(file)},
    )
    typer.echo(
        f"Uploaded {file} -> {name} v{version.version} ({version.row_count} rows)"
    )


@app.command()
def demo(
    path: Path = typer.Argument(
        Path("demo-workspace"), help="Where to create the demo workspace."
    ),
    build: bool = typer.Option(
        True, "--build/--no-build", help="Run the pipeline build after generating."
    ),
) -> None:
    """Generate the aviation demo workspace."""
    from laurelin.demo import create_demo

    workspace = create_demo(path, build=build)
    typer.echo(f"Demo workspace created at {workspace.root}")
    if build:
        typer.echo("Pipeline built: clean_aircraft, clean_flights, flight_stats")
    typer.echo(f"Next: laurelin serve --workspace {workspace.root}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
