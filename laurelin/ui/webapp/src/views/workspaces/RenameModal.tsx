// Edit a workspace's display name and description (PATCH). The slug is
// immutable and shown read-only.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { WorkspaceSummary } from "../../types";
import { InlineError } from "../admin/shared";

export function RenameModal({
  workspace,
  onClose,
}: {
  workspace: WorkspaceSummary;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(workspace.name ?? "");
  const [description, setDescription] = useState(workspace.description ?? "");

  const save = useMutation({
    mutationFn: () =>
      api.patch<WorkspaceSummary>(
        `${API}/workspaces/${encodeURIComponent(workspace.slug)}`,
        { name: name.trim(), description: description.trim() },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      onClose();
    },
  });

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 480 }}
      >
        <h2 style={{ fontSize: 16, marginBottom: 12 }}>
          Edit <span className="mono">{workspace.slug}</span>
        </h2>

        <div className="field">
          <label>Name</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={workspace.slug}
            autoComplete="off"
          />
        </div>
        <div className="field">
          <label>Description</label>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this workspace is for"
            autoComplete="off"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !save.isPending) save.mutate();
            }}
          />
        </div>

        <InlineError err={save.error} />

        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button
            className="button primary"
            disabled={save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save changes"}
          </button>
          <button className="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
