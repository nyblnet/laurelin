"""Workspace export and import — the capability that makes leaving real.

Public API, and nothing else is public:

    from laurelin.export import (
        ExportOptions, ImportOptions,
        preview_manifest, stream_export, export_workspace,
        read_manifest, import_workspace, target_is_pristine,
        governance_fingerprint, diff_fingerprints,
    )

Every one of these is a library call: no CLI, no HTTP, no FastAPI import
anywhere under ``laurelin/export``. The CLI and the API routes are thin
wrappers, which is what lets the round-trip proof run as a unit test instead of
needing a server.

Shapes, in the order a caller meets them::

    preview_manifest(workspace, store, options=None, storage=None) -> ExportManifest
    stream_export(workspace, store, out, options=None, storage=None) -> ExportManifest
    export_workspace(workspace, store, destination, options=None, storage=None)
        -> ExportManifest
    read_manifest(archive) -> ExportManifest
    target_is_pristine(store, workspace) -> (bool, dict[table, rows])
    import_workspace(archive, workspace, store, options=None, storage=None)
        -> ImportReport
    governance_fingerprint(store, catalog, principals, datasets=None,
                           object_types=None) -> dict
    diff_fingerprints(source, target) -> list[dict]

``archive`` is a path or any readable binary stream; ``out`` is any writable
binary stream and is never seeked.
"""

from laurelin.core.storage import Storage  # re-exported so callers need one import
from laurelin.export.manifest import (
    DATA_STATE_KEY,
    FORMAT_VERSION,
    NEEDS_CREDENTIALS_KEY,
    TABLE_POLICY,
    DatasetPlan,
    DataState,
    ExportManifest,
    ExportOptions,
    ExportRefused,
    ExportTrailer,
    ImportRefused,
    NeedsCredentials,
    PipelineWarning,
    PrincipalRef,
    TableClass,
    TableSpec,
    Withheld,
    exported_tables,
)
from laurelin.export.pipeline_scan import scan_pipelines
from laurelin.export.reader import (
    Collision,
    ImportOptions,
    ImportReport,
    acknowledge_pipelines,
    import_state,
    import_workspace,
    needs_credentials,
    pipelines_acknowledged,
    read_manifest,
    require_pipelines_acknowledged,
    target_is_pristine,
)
from laurelin.export.secrets import SECRET_KEY_RE, SECRET_KEYS, strip_secrets
from laurelin.export.verify import (
    contains,
    diff_fingerprints,
    governance_fingerprint,
    widenings,
)
from laurelin.export.writer import (
    build_manifest,
    export_workspace,
    preview_manifest,
    stream_export,
)

__all__ = [
    "Collision",
    "DATA_STATE_KEY",
    "DataState",
    "DatasetPlan",
    "ExportManifest",
    "ExportOptions",
    "ExportRefused",
    "ExportTrailer",
    "FORMAT_VERSION",
    "ImportOptions",
    "ImportRefused",
    "ImportReport",
    "NEEDS_CREDENTIALS_KEY",
    "NeedsCredentials",
    "PipelineWarning",
    "PrincipalRef",
    "SECRET_KEYS",
    "SECRET_KEY_RE",
    "Storage",
    "TABLE_POLICY",
    "TableClass",
    "TableSpec",
    "Withheld",
    "acknowledge_pipelines",
    "build_manifest",
    "contains",
    "widenings",
    "diff_fingerprints",
    "export_workspace",
    "exported_tables",
    "governance_fingerprint",
    "import_state",
    "import_workspace",
    "needs_credentials",
    "pipelines_acknowledged",
    "preview_manifest",
    "read_manifest",
    "require_pipelines_acknowledged",
    "scan_pipelines",
    "strip_secrets",
    "stream_export",
    "target_is_pristine",
]
