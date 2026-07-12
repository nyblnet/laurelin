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
        help="Disable in-browser transform authoring (pipeline files can only be "
        "edited on disk). Recommended for untrusted multi-user deployments, since "
        "writing a pipeline file is code-execution-equivalent.",
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
        )
    else:
        from laurelin.api import create_app

        ws = _find_workspace(workspace)
        typer.echo(f"Serving workspace '{ws.name}' ({ws.root}) on http://{host}:{port}")
        application = create_app(
            ws, no_auth=no_auth, secure_cookies=secure_cookies, lock_pipelines=lock_pipelines
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
