// Fold this object type's edit overlay into a new version of its backing
// dataset.
//
// The thing to understand before reading the copy below: folding does not
// *save* the edits. They already survive — every read replays the overlay. What
// folding buys is speed and agreement: after it, the dataset itself contains the
// edited rows, so anything reading the dataset directly (transforms, dashboards,
// SQL) sees what the object view sees. That trade is the consequence worth
// naming, and it is what the confirmation says.
//
// Lives here rather than in Ontology.tsx because it carries two preflight
// queries and the override handshake, and Ontology.tsx reads well at its
// current size.

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, ApiError, api } from "../../api";
import { useAuth } from "../../auth";
import type {
  DatasetDetail,
  ObjectTypeDetail,
  TransformSummary,
  WritebackResult,
} from "../../types";
import { ErrorBox } from "../../ui";

// Kinds the server refuses to fold into, mirroring DatasetInfo.scans_at_source
// (laurelin/core/models.py). Iceberg is in the set: Laurelin scans it at the
// source like the rest, so there is no local version to rewrite.
const AT_SOURCE = ["federated", "iceberg", "clickhouse", "starrocks"];

/** "v8", or nothing at all — better to omit the version than to print "vnull". */
function vsuffix(version: number | null, lead: string): string {
  return version == null ? "" : `${lead}v${version}`;
}

export function WritebackPanel({
  type,
  onBusyChange,
}: {
  type: ObjectTypeDetail;
  onBusyChange?: (busy: boolean) => void;
}) {
  const auth = useAuth();
  const qc = useQueryClient();
  const backing = type.backing_dataset;
  // Editor on the type is the floor. Edit rights on the *dataset* are the real
  // gate and are checked below, because that is the rule the server enforces.
  const canEdit = auth.can("editor") && type.permissions?.can_edit !== false;

  // Whether the user has deliberately accepted the transform-backed override.
  // Never pre-checked, and never sent on the first attempt: the server made this
  // explicit precisely because the failure mode is invisible.
  const [override, setOverride] = useState(false);

  // Preflight A — the backing dataset. Same query key the dataset detail page
  // uses, so this is usually already cached.
  const ds = useQuery({
    queryKey: ["dataset", backing],
    queryFn: () => api.get<DatasetDetail>(`${API}/datasets/${backing}`),
    enabled: canEdit,
  });

  // Preflight B — lineage, to warn about a transform-produced backing *before*
  // the user presses the button. Deliberately a partial check: the server also
  // treats a dataset whose latest version came from a build as transform-
  // produced even with no lineage edge, so this can under-detect and the 400 is
  // still handled reactively. It must never over-claim.
  const transforms = useQuery({
    queryKey: ["transforms"],
    queryFn: () => api.get<TransformSummary[]>(`${API}/transforms`),
    enabled: canEdit,
  });
  const producer = transforms.data?.find((t) => t.output === backing)?.name;

  const m = useMutation({
    mutationFn: (allow: boolean) =>
      api.post<WritebackResult>(
        `${API}/ontology/object-types/${type.api_name}/writeback` +
          // The override rides in the URL — the route declares no body.
          (allow ? "?allow_transform_backed=true" : ""),
      ),
    onSuccess: () => {
      setOverride(false);
      qc.invalidateQueries({ queryKey: ["object-type", type.api_name] });
      // A fold is the only thing that creates prunable history, so the edit-log
      // panel below is stale the moment this succeeds.
      qc.invalidateQueries({ queryKey: ["edit-log", type.api_name] });
      qc.invalidateQueries({ queryKey: ["objects", type.api_name] });
      qc.invalidateQueries({ queryKey: ["object", type.api_name] });
      qc.invalidateQueries({ queryKey: ["dataset", backing] });
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });

  // A fold and a rebuild racing each other is wasted work — the fold's own
  // version check would abort one of them — so the page disables the other
  // button while this runs.
  useEffect(() => {
    onBusyChange?.(m.isPending);
  }, [m.isPending, onBusyChange]);

  const err = m.error instanceof ApiError ? m.error : null;
  const detail = err?.detail ?? "";
  const bad = err?.status === 400;
  const transformRefusal = bad && detail.includes("is produced by transform");
  const raceRefusal = bad && detail.includes("moved from version");
  const staleRefusal =
    bad &&
    (detail.includes("scanned at the source") ||
      detail.includes("has no versions to fold into"));
  // Preflight B only sees lineage edges; the server also counts a dataset whose
  // latest version came from a build. When it refuses, its message names the
  // transform, so take the name from there rather than saying "this transform".
  const namedByServer = /produced by transform '([^']+)'/.exec(detail)?.[1];
  const transformName = producer ?? namedByServer;

  // A stale refusal means preflight A was out of date — refetch it so the card
  // falls back to its explanatory state instead of showing a dead button.
  useEffect(() => {
    if (staleRefusal) qc.invalidateQueries({ queryKey: ["dataset", backing] });
  }, [staleRefusal, qc, backing]);

  if (!canEdit) return null;

  // Nothing to say until we know what we would be folding into.
  if (!ds.data) return null;

  const kind = ds.data.kind ?? "managed";
  let blocked: string | null = null;
  if (AT_SOURCE.includes(kind)) {
    blocked =
      `Edits to these objects live in an overlay and are replayed on every ` +
      `read. They cannot be folded into ${backing}, which is a ${kind} table ` +
      `Laurelin scans in place and has no version to write. Materialize the ` +
      `rows with a transform and bind this type to that.`;
  } else if (ds.data.latest_version == null) {
    blocked = `${backing} has no versions yet, so there is nothing to fold into.`;
  } else if (ds.data.permissions?.can_edit === false) {
    blocked =
      `Folding rewrites ${backing}. That needs edit rights on the dataset, ` +
      `not just on this object type.`;
  }

  if (blocked) {
    return (
      <div className="card" style={{ marginBottom: 16 }}>
        <label style={{ marginBottom: 4 }}>Fold edits into the dataset</label>
        <p className="hint" style={{ marginTop: 0, marginBottom: 0 }}>{blocked}</p>
      </div>
    );
  }

  function confirmText(): string {
    const lines = [
      `Fold edits into ${backing}?`,
      "",
      `• Writes a new version of ${backing} with the edits already applied. It is a full rewrite, not an append — an overlay delete has no expression as an appended row.`,
      "• The folded edits stop being replayed on read. The dataset and the object view become the same thing.",
      `• Everything reading ${backing} directly — transforms, dashboards, SQL — sees the edited rows from now on.`,
      "• The previous version stays in history, but this is not undoable from here.",
    ];
    if (transformName) {
      lines.push(
        `• ${backing} is produced by transform ${transformName}. The next build overwrites these rows and the folded edits vanish — silently, hours later, with nothing in any error log.`,
      );
    }
    lines.push(
      "",
      "This reads every row and writes them all back. On a large dataset it takes a while.",
    );
    return lines.join("\n");
  }

  const result = m.data;
  // Blocked until the override is deliberately accepted; the checkbox is the
  // second act, not a second dialog chained onto the first.
  const held = transformRefusal && !override;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div
        className="toolbar"
        style={{ justifyContent: "space-between", alignItems: "center" }}
      >
        <label style={{ margin: 0 }}>Fold edits into the dataset</label>
        <button
          className="small"
          disabled={m.isPending || held}
          onClick={() => {
            if (confirm(confirmText())) m.mutate(override);
          }}
        >
          {m.isPending ? "Folding…" : "Fold edits…"}
        </button>
      </div>

      <p className="hint" style={{ marginTop: 6 }}>
        Edits are kept in an overlay and replayed on every read. Folding writes
        them into a new version of <code>{backing}</code> and stops replaying
        them — after this, anything reading the dataset directly (transforms,
        dashboards, SQL) sees the edited rows.
      </p>

      {m.isPending && (
        <p className="hint" style={{ marginBottom: 0 }}>
          Rewriting {backing} — reading every row and writing a new version. This
          can take a few minutes; leaving the page will not stop it.
        </p>
      )}

      {m.isError && <ErrorBox error={m.error} />}

      {transformRefusal && (
        <label
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 8,
            marginTop: 8,
            textTransform: "none",
            letterSpacing: 0,
            fontWeight: 400,
            fontSize: 12.5,
          }}
        >
          <input
            type="checkbox"
            checked={override}
            onChange={(e) => setOverride(e.target.checked)}
          />
          <span>
            Fold anyway — I understand the next build of{" "}
            {transformName ?? "this transform"} will overwrite these rows.
          </span>
        </label>
      )}

      {raceRefusal && (
        <p className="hint" style={{ marginBottom: 0 }}>
          A build wrote a new version while this was running. Nothing was
          written; you can retry.
        </p>
      )}

      {err?.status === 403 && (
        <p className="hint" style={{ marginBottom: 0 }}>
          Folding rewrites {backing}; edit rights on the object type are not
          enough.
        </p>
      )}

      {(err?.status === 504 || err?.status === 503) && (
        <p className="hint" style={{ marginBottom: 0 }}>
          The fold exceeded the query limits for a build. Nothing was written —
          the version is committed only after the whole scan succeeds.
        </p>
      )}

      {result && (
        <p
          className={result.folded > 0 ? "hint ok" : "hint"}
          style={{ marginBottom: 0 }}
        >
          {result.folded === 0
            ? // The version in this branch already existed — saying "created"
              // would be a lie about what just happened.
              `Nothing to fold — no unfolded edits on this type. ${backing} is unchanged${vsuffix(result.version, " at ")}.`
            : `Folded ${result.folded} edit${result.folded === 1 ? "" : "s"} into ${backing}${vsuffix(result.version, " ")}` +
              (result.row_count != null
                ? ` — ${result.row_count.toLocaleString()} rows.`
                : ".") +
              (result.objects != null
                ? ` Object store rebuilt (${result.objects.toLocaleString()} objects).`
                : " This type has no materialization, so there was nothing to rebuild.")}
        </p>
      )}
    </div>
  );
}
