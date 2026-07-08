// Workspaces — the multi-workspace control plane (superadmin only).
//
// One server can host many isolated workspaces; this view registers, edits,
// unregisters, and manages membership for them. Every call here is a
// control-plane endpoint (superadmin) and is workspace-INDEPENDENT: it does not
// require an active workspace and never sends a workspace-scoped request or
// touches the active-workspace cookie.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../api";
import type { WorkspaceSummary } from "../types";
import { useAuth } from "../auth";
import {
  Badge,
  EmptyState,
  ErrorBox,
  PageHeader,
  Spinner,
  fmtNum,
  fmtTime,
} from "../ui";
import { InlineError } from "./admin/shared";
import { ManageMembersModal } from "./workspaces/ManageMembers";
import { RenameModal } from "./workspaces/RenameModal";

const SLUG_RE = /^[a-z0-9][a-z0-9_-]{1,47}$/;

// --------------------------------------------------------------- create panel

function CreateWorkspacePanel() {
  const qc = useQueryClient();
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.post<WorkspaceSummary>(`${API}/workspaces`, {
        slug: slug.trim(),
        name: name.trim() || undefined,
        description: description.trim() || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      setSlug("");
      setName("");
      setDescription("");
    },
  });

  const trimmed = slug.trim();
  const valid = SLUG_RE.test(trimmed);
  const showBad = trimmed.length > 0 && !valid;
  const canSubmit = valid && !create.isPending;

  return (
    <div className="card" style={{ marginTop: 24, maxWidth: 560 }}>
      <div className="card-title">Create workspace</div>
      <div className="field">
        <label>Slug</label>
        <input
          value={slug}
          onChange={(e) => setSlug(e.target.value.toLowerCase())}
          placeholder="research"
          autoComplete="off"
          spellCheck={false}
          onKeyDown={(e) => {
            if (e.key === "Enter" && canSubmit) create.mutate();
          }}
        />
        <div className={`hint${showBad ? " bad" : ""}`}>
          Lowercase letters, digits, <span className="mono">_ -</span>; 2–48
          characters; must start with a letter or digit. This is the workspace's
          permanent identifier.
        </div>
      </div>
      <div className="field">
        <label>Name <span className="faint">(optional)</span></label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Research"
          autoComplete="off"
        />
      </div>
      <div className="field">
        <label>Description <span className="faint">(optional)</span></label>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What this workspace is for"
          autoComplete="off"
          onKeyDown={(e) => {
            if (e.key === "Enter" && canSubmit) create.mutate();
          }}
        />
      </div>
      <button
        className="button primary"
        disabled={!canSubmit}
        onClick={() => create.mutate()}
      >
        {create.isPending ? "Creating…" : "Create workspace"}
      </button>
      <InlineError err={create.error} />
    </div>
  );
}

// --------------------------------------------------------------- view

export function WorkspacesView() {
  const auth = useAuth();
  const qc = useQueryClient();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [managing, setManaging] = useState<string | null>(null);

  const wsQuery = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.get<WorkspaceSummary[]>(`${API}/workspaces`),
    enabled: auth.isSuperadmin,
  });

  const remove = useMutation({
    mutationFn: (slug: string) =>
      api.del<{ ok: boolean }>(
        `${API}/workspaces/${encodeURIComponent(slug)}`,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces"] }),
  });

  const subtitle =
    "Every workspace hosted on this server. Each is isolated — its own datasets, pipelines, ontology, and access control.";

  if (!auth.isSuperadmin) {
    return (
      <>
        <PageHeader title="Workspaces" subtitle={subtitle} />
        <EmptyState>
          Managing workspaces requires superadmin access.
        </EmptyState>
      </>
    );
  }

  const workspaces = wsQuery.data ?? [];
  const renamingWs = workspaces.find((w) => w.slug === renaming) ?? null;
  const managingWs = workspaces.find((w) => w.slug === managing) ?? null;

  return (
    <>
      <PageHeader title="Workspaces" subtitle={subtitle} />

      {remove.error && <InlineError err={remove.error} />}

      {wsQuery.isLoading ? (
        <Spinner />
      ) : wsQuery.error ? (
        <ErrorBox error={wsQuery.error} />
      ) : workspaces.length === 0 ? (
        <EmptyState>No workspaces yet. Create one below.</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Slug</th>
                <th>Name</th>
                <th>Members</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {workspaces.map((w) => (
                <tr key={w.slug}>
                  <td className="mono">{w.slug}</td>
                  <td>
                    <div>{w.name || <span className="faint">—</span>}</div>
                    {w.description && (
                      <div className="dim" style={{ fontSize: 12 }}>
                        {w.description}
                      </div>
                    )}
                  </td>
                  <td>
                    <Badge tone="neutral">{fmtNum(w.members)}</Badge>
                  </td>
                  <td className="dim">{fmtTime(w.created_at)}</td>
                  <td>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      <button
                        className="button small"
                        onClick={() => setRenaming(w.slug)}
                      >
                        Edit
                      </button>
                      <button
                        className="button small"
                        onClick={() => setManaging(w.slug)}
                      >
                        Manage members
                      </button>
                      <button
                        className="button danger small"
                        disabled={remove.isPending}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Unregister workspace "${w.slug}"? It will be removed from this server, but its data files remain on disk.`,
                            )
                          ) {
                            remove.mutate(w.slug);
                          }
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <CreateWorkspacePanel />

      {renamingWs && (
        <RenameModal
          workspace={renamingWs}
          onClose={() => setRenaming(null)}
        />
      )}
      {managingWs && (
        <ManageMembersModal
          workspace={managingWs}
          onClose={() => setManaging(null)}
        />
      )}
    </>
  );
}
