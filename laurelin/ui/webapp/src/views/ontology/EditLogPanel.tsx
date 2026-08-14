// What this object type's edit log costs, and what pruning it would reclaim.
//
// The log was never trimmed. A workspace using the ontology as an application
// database accumulates edits forever: disk grows without limit and every
// rebuild replays more. Nothing in the product said so — which is the reason
// this panel exists at all. An operator watching a table grow needs the number
// before they need the button.
//
// Two things this copy has to keep straight, because getting either wrong turns
// a safe operation into an alarming one:
//
//  * Pruning deletes *history*, never data. Only edits already folded into a
//    dataset version can go, and the server re-proves that per edit — see
//    OntologyService.prune_plan. What is lost is the answer to "who changed
//    this row, and when".
//  * The size is the JSON payloads only. It is a floor on what the log
//    occupies, not a disk measurement, and it says so rather than rounding a
//    guess up into a number that looks authoritative.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import { useAuth } from "../../auth";
import type { ObjectTypeDetail } from "../../types";
import { ErrorBox } from "../../ui";

interface RetainedReason {
  reason: string;
  edits: number;
  bytes: number;
}

interface EditLogReport {
  object_type: string;
  backing_dataset: string;
  /** True when a row policy narrows the backing dataset for this caller: the
   *  counters describe a shared log and are operator numbers, so they follow
   *  the same rule the object-index block does and are withheld. */
  withheld: boolean;
  keep?: number;
  edits?: number;
  live?: number;
  folded?: number;
  payload_bytes?: number;
  prunable?: number;
  prunable_bytes?: number;
  retained?: RetainedReason[];
  pruned?: number;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function EditLogPanel({ type }: { type: ObjectTypeDetail }) {
  const qc = useQueryClient();
  const auth = useAuth();
  const canEdit = auth.can("editor") && type.permissions?.can_edit !== false;
  // Deleting the record of who changed what is an admin act, a rank above the
  // fold that made those edits redundant. The panel still *shows* the size to
  // an editor: knowing the log is 40 MB is not a privilege.
  const canPrune = auth.can("admin");
  const [keep, setKeep] = useState(0);

  const q = useQuery({
    queryKey: ["edit-log", type.api_name, keep],
    queryFn: () =>
      api.get<EditLogReport>(
        `${API}/ontology/object-types/${type.api_name}/edit-log?keep=${keep}`,
      ),
    enabled: canEdit,
  });

  const prune = useMutation({
    mutationFn: () =>
      api.post<EditLogReport>(
        `${API}/ontology/object-types/${type.api_name}/edit-log/prune` +
          `?keep=${keep}`,
        {},
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["edit-log", type.api_name] });
      qc.invalidateQueries({ queryKey: ["object-type", type.api_name] });
    },
  });

  if (!canEdit || !q.data) return null;
  const r = q.data;

  if (r.withheld) {
    return (
      <div className="card" style={{ marginBottom: 16 }}>
        <label style={{ marginBottom: 4 }}>Edit log</label>
        <p className="hint" style={{ marginTop: 0, marginBottom: 0 }}>
          The edit log is shared across every tenant of{" "}
          <code>{r.backing_dataset}</code>, so its size is an operator number
          and is not shown to a user whose row policy narrows that dataset.
        </p>
      </div>
    );
  }

  const edits = r.edits ?? 0;
  if (edits === 0) {
    return (
      <div className="card" style={{ marginBottom: 16 }}>
        <label style={{ marginBottom: 4 }}>Edit log</label>
        <p className="hint" style={{ marginTop: 0, marginBottom: 0 }}>
          No edits recorded for this object type.
        </p>
      </div>
    );
  }

  const prunable = r.prunable ?? 0;
  const retained = r.retained ?? [];

  function confirmText(): string {
    return [
      `Prune ${prunable} folded edit${prunable === 1 ? "" : "s"} from ${type.api_name}?`,
      "",
      "• These edits are already folded into a version of " +
        `${r.backing_dataset}, so the rows already carry them. No object ` +
        "changes and no data is lost.",
      "• What is deleted is the record of who made each change and when — " +
        `including the explanation of what changed in the version they were ` +
        "folded into.",
      "• Edits still being replayed on read are never touched, and neither is " +
        "any edit whose fold a later build may have overwritten.",
      "",
      "This cannot be undone from here.",
    ].join("\n");
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div
        className="toolbar"
        style={{ justifyContent: "space-between", alignItems: "center" }}
      >
        <label style={{ margin: 0 }}>Edit log</label>
        <span className="faint" style={{ fontSize: 12 }}>
          {edits.toLocaleString()} edit{edits === 1 ? "" : "s"} ·{" "}
          {(r.live ?? 0).toLocaleString()} replayed on every read ·{" "}
          {(r.folded ?? 0).toLocaleString()} folded
        </span>
      </div>

      <p className="hint" style={{ marginTop: 6 }}>
        {fmtBytes(r.payload_bytes ?? 0)} of edit payloads, and nothing trims
        them on its own. The figure counts the stored JSON only — row overhead
        and indexes are on top of it, so treat it as a floor rather than a disk
        measurement.
      </p>

      <div className="toolbar" style={{ gap: 8, alignItems: "center" }}>
        <span className="faint" style={{ fontSize: 12 }}>Keep newest folded</span>
        <input
          type="number"
          min={0}
          className="mono"
          value={keep}
          onChange={(e) => setKeep(Math.max(0, Number(e.target.value) || 0))}
          style={{ maxWidth: 90 }}
        />
        {canPrune && (
          <button
            className="small"
            disabled={prune.isPending || prunable === 0}
            onClick={() => {
              if (confirm(confirmText())) prune.mutate();
            }}
            title={
              prunable === 0
                ? "Nothing can be pruned safely right now"
                : "Delete folded edits that are no longer load-bearing"
            }
          >
            {prune.isPending ? "Pruning…" : "Prune…"}
          </button>
        )}
      </div>

      <p className={prunable > 0 ? "hint" : "hint faint"} style={{ marginTop: 8 }}>
        {prunable > 0
          ? `Pruning would delete ${prunable.toLocaleString()} folded edit${
              prunable === 1 ? "" : "s"
            } and reclaim ${fmtBytes(r.prunable_bytes ?? 0)}. The objects do not change.`
          : "Nothing can be pruned right now."}
      </p>

      {retained.length > 0 && (
        <ul className="hint" style={{ margin: "0 0 8px", paddingLeft: 18 }}>
          {retained.map((x) => (
            <li key={x.reason}>
              {x.edits.toLocaleString()} kept — {x.reason}.
            </li>
          ))}
        </ul>
      )}

      {!canPrune && (
        <p className="hint faint" style={{ marginBottom: 0 }}>
          Pruning deletes the record of who changed what, so it needs admin.
        </p>
      )}

      {prune.isError && <ErrorBox error={prune.error} />}
      {prune.data && (
        <p className="hint ok" style={{ marginBottom: 0 }}>
          Pruned {(prune.data.pruned ?? 0).toLocaleString()} edit
          {prune.data.pruned === 1 ? "" : "s"}.
        </p>
      )}
    </div>
  );
}
