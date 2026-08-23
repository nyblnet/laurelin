"""Laurelin command-line interface (the `laurelin` entry point)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from laurelin.core import fileperms, serialize
from laurelin.core.config import Workspace, WorkspaceNotFound
from laurelin.core.roles import Role

app = typer.Typer(
    name="laurelin",
    help="Laurelin: an open, ontology-driven data platform.",
    no_args_is_help=True,
    add_completion=False,
)
datasets_app = typer.Typer(help="Inspect workspace datasets.", no_args_is_help=True)
app.add_typer(datasets_app, name="datasets")
users_app = typer.Typer(help="Manage workspace users.", no_args_is_help=True)
app.add_typer(users_app, name="users")
tokens_app = typer.Typer(help="Manage workspace API tokens.", no_args_is_help=True)
app.add_typer(tokens_app, name="tokens")

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
    root: Optional[Path] = typer.Option(
        None,
        "--root",
        help="Serve MANY workspaces from this root directory (multi-workspace "
        "mode). Mutually exclusive with --workspace. Global users + a workspace "
        "registry live in <root>/control.db; each workspace is <root>/<slug>.",
    ),
    control_db: Optional[str] = typer.Option(
        None,
        "--control-db",
        help="With --root: a postgresql:// URL for the control plane (users, "
        "workspaces, membership) instead of <root>/control.db. Recommended for "
        "real multi-tenant deployments. Also settable via "
        "LAURELIN_CONTROL_DATABASE_URL.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8787, "--port", help="Bind port."),
    no_auth: bool = typer.Option(
        False,
        "--no-auth",
        help="Disable authentication (local development only): every request "
        "acts as an implicit admin.",
    ),
    secure_cookies: bool = typer.Option(
        False,
        "--secure-cookies",
        help="Set the Secure flag on session cookies (use behind HTTPS).",
    ),
    lock_pipelines: bool = typer.Option(
        False,
        "--lock-pipelines",
        help="Disable in-browser Python pipeline authoring (pipeline files can "
        "only be edited on disk). Recommended for untrusted multi-user "
        "deployments, since writing a pipeline file is code-execution-equivalent. "
        "Flows and Explore — no-code authoring that compiles to bound SQL and "
        "cannot reach exec — stay available; add --lock-flows to close those too.",
    ),
    lock_flows: bool = typer.Option(
        False,
        "--lock-flows",
        help="Disable no-code flow authoring (Flows and Explore saves). Combine "
        "with --lock-pipelines for a total authoring lockdown.",
    ),
) -> None:
    """Run the Laurelin API + UI server (single workspace, or --root for many)."""
    import uvicorn

    if no_auth:
        typer.echo("Warning: --no-auth disables authentication; every request is an admin.")

    if root is not None:
        if workspace is not None:
            typer.echo("Error: pass either --workspace or --root, not both.", err=True)
            raise typer.Exit(1)
        from laurelin.api import create_server_app

        typer.echo(f"Serving workspaces under {root} on http://{host}:{port}")
        application = create_server_app(
            root,
            control_url=control_db,
            no_auth=no_auth,
            secure_cookies=secure_cookies,
            lock_pipelines=lock_pipelines,
            lock_flows=lock_flows,
        )
    else:
        from laurelin.api import create_app

        ws = _find_workspace(workspace)
        typer.echo(f"Serving workspace '{ws.name}' ({ws.root}) on http://{host}:{port}")
        application = create_app(
            ws,
            no_auth=no_auth,
            secure_cookies=secure_cookies,
            lock_pipelines=lock_pipelines,
            lock_flows=lock_flows,
        )
    uvicorn.run(application, host=host, port=port)


@app.command()
def build(
    targets: Optional[list[str]] = typer.Argument(
        None, help="Output dataset names to build (default: everything)."
    ),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Run pipeline transforms and materialize their output datasets."""
    ws = _find_workspace(workspace)
    from laurelin.export import ImportRefused, require_pipelines_acknowledged
    from laurelin.transforms import Builder, collect_transforms

    # collect_transforms EXECs every .py in pipelines/. If they arrived in an
    # import, an admin has to say they read them first — the CLI is the same
    # front door as the API here, not a way around it.
    try:
        require_pipelines_acknowledged(ws)
    except ImportRefused as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(2)

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
    # `as_author` is R2's one deliberate opt-out, and this is what it is for.
    # The CLI is not a privilege crossing: possession of the workspace directory
    # *is* the credential here, so the operator gets the endpoint an HTTP editor
    # would not. Saying so with a greppable context manager rather than by
    # reaching past the serializer is the whole point — `tests/test_audience.py`
    # asserts this name appears nowhere under `laurelin/api/`.
    with serialize.as_author(Role.admin):
        for t in info.tasks:
            if t.failure is not None:
                # R1: the operator gets Laurelin's sentence plus the
                # `detail_ref` that finds the driver's own words in the server
                # log. There is no longer a "more" to print — the driver's text
                # was never stored. `laurelin serve` logs it under this ref.
                typer.echo(
                    f"  {t.transform_name}: "
                    f"{serialize.detail_for(t.failure, Role.admin)}",
                    err=True,
                )
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
def mcp(
    url: str = typer.Option(
        "http://127.0.0.1:8787",
        "--url",
        envvar="LAURELIN_URL",
        help="Base URL of the running Laurelin server.",
    ),
    token: str = typer.Option(
        "",
        "--token",
        envvar="LAURELIN_TOKEN",
        help="API token (create one in the UI under your account).",
    ),
    workspace: str = typer.Option(
        "",
        "--mcp-workspace",
        envvar="LAURELIN_MCP_WORKSPACE",
        help="Workspace slug on a multi-workspace server.",
    ),
) -> None:
    """Serve this workspace to AI agents over MCP (stdio).

    Every tool call goes through the normal REST API with the given token, so
    RBAC, ACLs, row-level security, markings, and audit all apply. Configure
    your agent with: laurelin mcp --url http://host:8787 --token <token>
    """
    from laurelin.mcp import LaurelinClient, build_server

    if not token and not os.environ.get("LAURELIN_NO_AUTH"):
        typer.echo(
            "Warning: no --token given; only works against a --no-auth server.",
            err=True,
        )
    client = LaurelinClient(url, token=token, workspace=workspace)
    try:
        build_server(client).run()
    except RuntimeError as exc:  # missing optional dependency
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


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


# -- portability: export / import / verify-governance -------------------------
#
# Three top-level verbs rather than a sub-app, matching build/upload/demo: a
# `laurelin portability export` would bury the one command whose existence is
# the product claim.
#
# Exit codes are load-bearing and differ from the rest of the CLI:
#   0  it worked
#   1  it failed
#   2  it REFUSED, and the message names the flag that overrides the refusal
# A refusal is not an error — it is the tool declining to write something
# misleading — and a runbook that cannot tell the two apart will retry the
# wrong one.


def _refused(exc: Exception) -> None:
    typer.echo(f"Refused: {exc}", err=True)
    raise typer.Exit(2)


def _detect_mode(ws: Workspace):
    """Whether this workspace belongs to a multi-workspace server, and its members.

    Worth detecting rather than asking about: in multi mode a user's effective
    role for the workspace lives in ``control.db::workspace_members``
    (auth_routes.py:126) and admin bypasses every gate, so an export taken
    without it cannot reproduce a single access decision. If the CLI assumed
    "single" it would produce that governance-incomplete archive silently,
    which is the failure this whole feature exists to prevent.
    """
    from laurelin.core.control import ControlStore

    url = os.environ.get("LAURELIN_CONTROL_DATABASE_URL")
    if url:
        control = ControlStore(url)
    else:
        candidate = ws.root.parent / "control.db"
        if not candidate.exists():
            return "single", None, None
        control = ControlStore(candidate)
    slug = ws.root.name
    try:
        if control.get_workspace(slug) is None:
            return "single", None, None
    except Exception:  # noqa: BLE001 - an unreachable control plane is not this workspace's
        return "single", None, None
    return "multi", control, slug


def _fingerprint_now(ws: Workspace, store, catalog, principals=None) -> dict:
    """The decision matrix for every account in the workspace, plus anonymous."""
    from laurelin.core.auth import AuthService
    from laurelin.export import governance_fingerprint
    from laurelin.ontology import load_ontology

    if principals is None:
        principals = [*AuthService(store).list_users(), None]
    try:
        ontology = load_ontology(ws.ontology_dir)
        object_types = {o.api_name: o.backing_dataset for o in ontology.object_types}
    except ValueError:
        object_types = {}
    return governance_fingerprint(store, catalog, principals, object_types=object_types)


def _export_summary(manifest) -> None:
    """One line per outcome, on stderr so `laurelin export -` stays pipeable.

    Incompleteness is announced in four places (manifest, this line, the
    import-time 409, and the UI) because any single one of them is skippable,
    and a migration that quietly left three datasets behind is exactly the kind
    of subtly-broken outcome that only surfaces months later.
    """
    by_state: dict[str, list] = {}
    for plan in manifest.datasets:
        by_state.setdefault(plan.data_state, []).append(plan)
    parts = []
    for state in sorted(by_state):
        plans = by_state[state]
        size = sum(p.bytes for p in plans)
        kinds = ",".join(sorted({p.kind for p in plans}))
        suffix = f" ({size / 1e6:.1f} MB)" if size else ""
        parts.append(f"{kinds}: {len(plans)} {state}{suffix}")
    typer.echo(" | ".join(parts) or "no datasets", err=True)
    typer.echo(
        f"withheld secrets: {len(manifest.withheld)} "
        f"(listed in manifest.json; re-supply each one at the destination)",
        err=True,
    )
    if manifest.content_warnings:
        typer.echo(
            f"credential-shaped content carried as-is: "
            f"{len(manifest.content_warnings)} place(s)",
            err=True,
        )


@app.command("export")
def export_cmd(
    output: Optional[str] = typer.Argument(
        None, help="Archive path, or '-' for stdout (pipe it into `laurelin import -`)."
    ),
    workspace: Optional[Path] = WORKSPACE_OPTION,
    metadata_only: bool = typer.Option(
        False,
        "--metadata-only",
        help="Omit data/** — governance, ontology and pipelines only.",
    ),
    audit: bool = typer.Option(
        True, "--audit/--no-audit", help="Carry the audit log (default: carried)."
    ),
    gzip: Optional[bool] = typer.Option(
        None,
        "--gzip/--no-gzip",
        help="Default: on for --metadata-only, off otherwise (Parquet is already "
        "compressed; gzip over it buys ~2% for real CPU).",
    ),
    include_membership: Optional[bool] = typer.Option(
        None,
        "--include-membership/--no-membership",
        help="Multi-workspace mode only: carry workspace_members for this slug. "
        "One of the two is REQUIRED there.",
    ),
    allow_content_warnings: bool = typer.Option(
        False,
        "--allow-content-warnings",
        help="Carry pipelines, ontology YAML and authored config that look "
        "like they hold a credential.",
    ),
    allow_remote_data_plane: bool = typer.Option(
        False,
        "--allow-remote-data-plane",
        help="Attempt a full export when the data plane is an object store "
        "(unverified end to end).",
    ),
    fingerprint: bool = typer.Option(
        False,
        "--fingerprint",
        help="Compute the governance decision matrix and embed it in the "
        "manifest, so the destination can be proven to govern identically "
        "(`laurelin verify-governance --baseline <archive>`).",
    ),
    dataset: Optional[list[str]] = typer.Option(
        None,
        "--dataset",
        help="Repeatable. Restricts which datasets' DATA travels; governance is "
        "a closure and always travels whole.",
    ),
) -> None:
    """Export this workspace to a tar archive you can reconstruct elsewhere."""
    import sys

    from laurelin.export import ExportOptions, ExportRefused, export_workspace, stream_export

    ws = _find_workspace(workspace)
    store, catalog = _open_engine(ws)
    mode, control, slug = _detect_mode(ws)
    rows: tuple = ()
    if mode == "multi" and include_membership and control is not None and slug:
        rows = tuple({"slug": slug, **m} for m in control.list_members(slug))

    options = ExportOptions(
        metadata_only=metadata_only,
        include_audit=audit,
        gzip=gzip,
        allow_content_warnings=allow_content_warnings,
        datasets=tuple(dataset) if dataset else None,
        created_by=os.environ.get("USER", "cli"),
        mode=mode,
        multi_slug=slug,
        include_membership=include_membership,
        membership_rows=rows,
        allow_remote_data_plane=allow_remote_data_plane,
    )
    if fingerprint:
        options.governance_fingerprint = _fingerprint_now(ws, store, catalog)

    try:
        if output is None or output == "-":
            manifest = stream_export(ws, store, sys.stdout.buffer, options)
        else:
            manifest = export_workspace(ws, store, Path(output), options)
    except ExportRefused as exc:
        _refused(exc)
    except (OSError, ValueError) as exc:
        _cli_fail(exc)

    store.log_audit(
        "workspace_exported",
        {
            "metadata_only": metadata_only,
            "audit": audit,
            "membership": manifest.scope.membership,
            "datasets": len(manifest.datasets),
            "withheld": len(manifest.withheld),
            "destination": output or "-",
        },
        actor=options.created_by,
    )
    if output and output != "-":
        typer.echo(f"Wrote {output} (mode 0600)", err=True)
    _export_summary(manifest)


@app.command("import")
def import_cmd(
    archive: str = typer.Argument(..., help="Archive path, or '-' for stdin."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the report and write nothing."
    ),
    merge: bool = typer.Option(
        False, "--merge", help="Allow a non-empty target (two-phase; see --confirm)."
    ),
    confirm: Optional[str] = typer.Option(
        None, "--confirm", help="The sha256 of the report you read (phase 2)."
    ),
    rename_prefix: Optional[str] = typer.Option(
        None, "--rename-prefix", help="Prefix imported dataset names on collision."
    ),
    metadata_only: bool = typer.Option(
        False, "--metadata-only", help="Skip data/** even in a full archive."
    ),
    report: Optional[Path] = typer.Option(
        None, "--report", help="Where to write the report (default: <workspace>/import_report.json)."
    ),
) -> None:
    """Reconstruct a workspace from an archive. Binds no principal."""
    import sys

    from laurelin.export import ImportOptions, ImportRefused, import_workspace

    ws = _find_workspace(workspace)
    store, _ = _open_engine(ws)
    options = ImportOptions(
        dry_run=dry_run,
        merge=merge,
        confirm=confirm,
        rename_prefix=rename_prefix,
        metadata_only=metadata_only,
        actor=os.environ.get("USER", "cli"),
    )
    source = sys.stdin.buffer if archive == "-" else Path(archive)
    try:
        result = import_workspace(source, ws, store, options)
    except ImportRefused as exc:
        _refused(exc)
    except (OSError, ValueError) as exc:
        _cli_fail(exc)

    destination = report or (ws.root / "import_report.json")
    # Same opt-out, same reason: this file is written 0600 into the operator's
    # own filesystem, and a governance report with its subjects withheld is not
    # a governance report.
    with serialize.as_author(Role.admin):
        _write_private(
            destination, json.dumps(serialize.dump(result), indent=2)
        )
    _print_import_report(result, destination)


def _write_private(path: Path, text: str) -> None:
    """0600 from creation, never open()-then-chmod.

    The report names every principal the archive references and every place a
    credential has to be re-supplied; a 0644 window is a window.

    The implementation moved to ``core/fileperms.py`` so the export reader's
    two import-state writes could stop being an open()-then-chmod — they were
    the copies of this that never got the fix.
    """
    fileperms.write_private(path, text)


def _print_import_report(result, destination: Path) -> None:
    verb = "Would import" if not result.applied else "Imported"
    typer.echo(f"{verb} from origin {result.manifest.origin.origin_id[:19]}…")
    if result.rows_imported:
        _print_table(
            ["TABLE", "ROWS"],
            [[t, str(n)] for t, n in sorted(result.rows_imported.items())],
        )
    if result.rows_quarantined:
        typer.echo(
            "\nCarried but NOT applied — binding a principal is an explicit, "
            "audited admin act:"
        )
        _print_table(
            ["TABLE", "ROWS"],
            [[t, str(n)] for t, n in sorted(result.rows_quarantined.items())],
        )
    if result.principals:
        typer.echo("\nPrincipals the imported rules name (none were created):")
        _print_table(
            ["KIND", "NAME", "REFERENCED BY"],
            [
                [p.kind, p.name, ", ".join(p.referenced_by[:3])]
                for p in result.principals
            ],
        )
    if result.withheld:
        typer.echo("\nSecrets the export withheld — re-supply each one:")
        _print_table(
            ["TABLE", "ROW", "FIELD", "WHERE"],
            [[w.table, w.row, w.field, w.resupply] for w in result.withheld],
        )
    for collision in result.collisions:
        typer.echo(f"\n[{collision.severity}] {collision.kind} {collision.name}: "
                   f"{collision.detail}")
    if not result.applied:
        typer.echo(f"\nNothing was written. To apply: --merge --confirm {result.report_sha256}")
    else:
        typer.echo(
            "\nImported pipelines are parked: they are exec'd unsandboxed on "
            "every build, so an admin must acknowledge them before a build "
            "will run (POST /api/v1/workspace/import/acknowledge-pipelines)."
        )
    typer.echo(f"Report: {destination}")


@app.command("verify-governance")
def verify_governance(
    baseline: Path = typer.Option(
        ...,
        "--baseline",
        help="An archive exported with --fingerprint, or a fingerprint JSON file.",
    ),
    workspace: Optional[Path] = WORKSPACE_OPTION,
    principals: Optional[str] = typer.Option(
        None,
        "--principals",
        help="Comma-separated usernames to check (default: the baseline's own list).",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", help="Write this workspace's recomputed fingerprint here."
    ),
) -> None:
    """Prove this workspace governs identically to the one in a baseline.

    The proof is the product: a tarball helper has a download button, a
    portability guarantee has a matrix you can diff.
    """
    from laurelin.core.auth import AuthService
    from laurelin.export import ImportRefused, diff_fingerprints, read_manifest

    ws = _find_workspace(workspace)
    store, catalog = _open_engine(ws)

    raw = baseline.read_bytes() if baseline.exists() else b""
    if not raw:
        _cli_fail(FileNotFoundError(f"No such baseline: {baseline}"))
    if raw.lstrip()[:1] == b"{":
        loaded = json.loads(raw)
        source = loaded.get("governance_fingerprint", loaded)
    else:
        try:
            source = read_manifest(baseline).governance_fingerprint
        except ImportRefused as exc:
            _refused(exc)
    if not source.get("cells"):
        _cli_fail(
            ValueError(
                f"{baseline} carries no governance fingerprint. Re-export the "
                "source with `laurelin export --fingerprint`."
            )
        )

    auth = AuthService(store)
    wanted = (
        [p.strip() for p in principals.split(",") if p.strip()]
        if principals
        else list(source.get("principals", []))
    )
    resolved, missing = [], []
    for name in wanted:
        if name == "anonymous":
            resolved.append(None)
            continue
        user = auth.get_user(name)
        if user is None:
            missing.append(name)
        else:
            resolved.append(user)

    current = _fingerprint_now(ws, store, catalog, principals=resolved)
    if out is not None:
        _write_private(out, json.dumps(current, indent=2, sort_keys=True))

    if missing:
        typer.echo(
            "Principals in the baseline that do not exist here (an unbound "
            "import creates none — this is expected until you rebind):"
        )
        for name in missing:
            typer.echo(f"  - {name}")
        typer.echo("")

    differences = diff_fingerprints(source, current)
    if not differences:
        typer.echo(
            f"Identical: {len(source.get('cells', {}))} (principal, dataset) "
            f"cells answer the same on both sides."
        )
        return
    typer.echo(f"{len(differences)} cell(s) differ:")
    for entry in differences:
        key = entry.get("cell") or entry.get("object_type")
        detail = entry.get("changes") or [f"{entry.get('source')} -> {entry.get('target')}"]
        typer.echo(f"  {key}: {'; '.join(str(d) for d in detail)}")
    raise typer.Exit(1)


# -- users & tokens -----------------------------------------------------------


def _auth_service(workspace: Optional[Path]):
    from laurelin.core.auth import AuthService
    from laurelin.core.db import MetadataStore

    ws = _find_workspace(workspace)
    return AuthService(MetadataStore(ws.metadata_path))


def _cli_fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1)


@users_app.command("create")
def users_create(
    username: str = typer.Argument(..., help="Username (lowercase, 2-32 chars)."),
    role: str = typer.Option("viewer", "--role", help="viewer | editor | admin."),
    password: Optional[str] = typer.Option(
        None, "--password", help="Password (prompted securely when omitted)."
    ),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Create a user (the first user may be created this way)."""
    auth = _auth_service(workspace)
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        user = auth.create_user(username, password, role, actor="cli")
    except ValueError as exc:
        _cli_fail(exc)
    typer.echo(f"Created user '{user.username}' with role '{user.role.value}'")


@users_app.command("list")
def users_list(workspace: Optional[Path] = WORKSPACE_OPTION) -> None:
    """List users."""
    auth = _auth_service(workspace)
    users = auth.list_users()
    if not users:
        typer.echo("No users. Create one with `laurelin users create`.")
        return
    _print_table(
        ["USERNAME", "ROLE", "DISABLED", "CREATED"],
        [
            [u.username, u.role.value, "yes" if u.disabled else "no", u.created_at]
            for u in users
        ],
    )


@users_app.command("passwd")
def users_passwd(
    username: str = typer.Argument(..., help="Username."),
    password: Optional[str] = typer.Option(
        None, "--password", help="New password (prompted securely when omitted)."
    ),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Change a user's password."""
    auth = _auth_service(workspace)
    if password is None:
        password = typer.prompt(
            "New password", hide_input=True, confirmation_prompt=True
        )
    try:
        auth.update_user(username, password=password, actor="cli")
    except (KeyError, ValueError) as exc:
        _cli_fail(exc)
    typer.echo(f"Password updated for '{username}'")


@users_app.command("role")
def users_role(
    username: str = typer.Argument(..., help="Username."),
    role: str = typer.Argument(..., help="viewer | editor | admin."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Change a user's role."""
    auth = _auth_service(workspace)
    try:
        user = auth.update_user(username, role=role, actor="cli")
    except (KeyError, ValueError) as exc:
        _cli_fail(exc)
    typer.echo(f"'{user.username}' is now '{user.role.value}'")


@users_app.command("disable")
def users_disable(
    username: str = typer.Argument(..., help="Username."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Disable a user (their sessions and tokens stop working)."""
    auth = _auth_service(workspace)
    try:
        auth.update_user(username, disabled=True, actor="cli")
    except KeyError as exc:
        _cli_fail(exc)
    typer.echo(f"Disabled '{username}'")


@users_app.command("enable")
def users_enable(
    username: str = typer.Argument(..., help="Username."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Re-enable a disabled user."""
    auth = _auth_service(workspace)
    try:
        auth.update_user(username, disabled=False, actor="cli")
    except KeyError as exc:
        _cli_fail(exc)
    typer.echo(f"Enabled '{username}'")


@users_app.command("delete")
def users_delete(
    username: str = typer.Argument(..., help="Username."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Delete a user (and their sessions and API tokens)."""
    auth = _auth_service(workspace)
    try:
        auth.delete_user(username, actor="cli")
    except KeyError as exc:
        _cli_fail(exc)
    typer.echo(f"Deleted '{username}'")


@tokens_app.command("create")
def tokens_create(
    name: str = typer.Argument(..., help="Token name (e.g. 'ci-deploy')."),
    user: str = typer.Option(..., "--user", help="Username the token acts as."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Create an API token. The token is printed ONCE and never shown again."""
    auth = _auth_service(workspace)
    owner = auth.get_user(user)
    if owner is None:
        _cli_fail(KeyError(f"User not found: {user!r}"))
    try:
        token, record = auth.create_api_token(owner, name, actor="cli")
    except ValueError as exc:
        _cli_fail(exc)
    typer.echo(f"Created token '{record['name']}' (id {record['id']}) for '{user}'.")
    typer.echo("This token will not be shown again:")
    typer.echo(token)


@tokens_app.command("list")
def tokens_list(workspace: Optional[Path] = WORKSPACE_OPTION) -> None:
    """List API tokens (hashes only are stored; plaintext is never shown)."""
    auth = _auth_service(workspace)
    tokens = auth.list_api_tokens()
    if not tokens:
        typer.echo("No API tokens.")
        return
    _print_table(
        ["ID", "NAME", "USER", "CREATED", "LAST USED"],
        [
            [
                t["id"],
                t["name"],
                t["username"] or "-",
                t["created_at"],
                t["last_used_at"] or "-",
            ]
            for t in tokens
        ],
    )


@tokens_app.command("revoke")
def tokens_revoke(
    token_id: str = typer.Argument(..., help="Token id (see `laurelin tokens list`)."),
    workspace: Optional[Path] = WORKSPACE_OPTION,
) -> None:
    """Revoke an API token."""
    auth = _auth_service(workspace)
    try:
        auth.revoke_api_token(token_id, actor="cli")
    except KeyError as exc:
        _cli_fail(exc)
    typer.echo(f"Revoked token {token_id}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
