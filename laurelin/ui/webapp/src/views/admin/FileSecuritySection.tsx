// Admin > Workspace files on disk: the modes of the files that hold this
// workspace's secrets.
//
// This panel exists because the server's repair is deliberately partial.
// Laurelin creates metadata.db, its -wal/-shm siblings and laurelin.yml at
// 0600, and strips world access and group write from a database it inherited
// from an older version — but it leaves group *read* alone, because a group
// can be a set of principals somebody provisioned on purpose (see
// laurelin/core/fileperms.py). That residual is a judgement only the operator
// can make, and a WARNING in a log nobody tails is not how a judgement gets
// made. So the number is on the screen.

import { useQuery } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { FileSecurityEntry, WorkspaceFileSecurity } from "../../types";
import { Badge, DataTable, ErrorBox, Spinner, type Column } from "../../ui";

// Group WRITE is red, not gold, and it does not say "readable". A group member
// with write on metadata.db can edit the users table and make themselves an
// admin — that is not a share, it is an admin grant. This badge said "readable
// by its group" over a 0660 file while exactly that happened.
function toneFor(e: FileSecurityEntry): "green" | "gold" | "red" {
  if (e.world_accessible || e.group_writable) return "red";
  if (e.group_accessible) return "gold";
  return "green";
}

function verdictFor(e: FileSecurityEntry): string {
  if (e.world_accessible) return "readable by every local user";
  if (e.group_writable) return "WRITABLE by its group";
  if (e.group_accessible) return "readable by its group";
  return "owner only";
}

export function FileSecuritySection() {
  const query = useQuery({
    queryKey: ["workspace-file-security"],
    queryFn: () =>
      api.get<WorkspaceFileSecurity>(`${API}/workspace/file-security`),
  });

  const data = query.data;
  const rows: FileSecurityEntry[] = data
    ? [data.directory, ...data.files]
    : [];
  const exposed = rows.some((r) => r.world_accessible || r.group_accessible);
  // World access surviving a repair means the chmod itself failed. Group
  // access surviving is the documented, deliberate outcome.
  const advisoryOnly =
    exposed && !rows.some((r) => r.world_accessible || r.group_writable);

  const columns: Column<FileSecurityEntry>[] = [
    {
      label: "Path",
      render: (e) => (
        <code>{e.name === "." ? "workspace directory" : e.name}</code>
      ),
    },
    { label: "Mode", render: (e) => <code>{e.mode ?? "—"}</code> },
    {
      label: "Reachable by",
      render: (e) => <Badge tone={toneFor(e)}>{verdictFor(e)}</Badge>,
    },
  ];

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ fontSize: 15, marginBottom: 4 }}>Workspace files on disk</h2>
      <div className="subtitle" style={{ marginBottom: 12 }}>
        metadata.db holds live session tokens, password hashes and every
        connector credential in the clear. These are the modes it actually has
        right now, read from the filesystem — not what Laurelin intended.
      </div>

      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorBox error={query.error} />
      ) : data ? (
        <>
          {data.store_is_remote && (
            <div className="card" style={{ marginBottom: 12 }}>
              The metadata store is <b>{data.dialect}</b>, so it keeps no file
              on this host — there is nothing here for Laurelin to protect, and
              nothing it can protect. Securing it is the database server's job.
              The workspace directory below still holds dataset files.
            </div>
          )}

          <DataTable columns={columns} rows={rows} rowKey={(e) => e.name} />

          {/* A surviving group bit is an advisory (gold); a file Laurelin was
              refused permission to chmod is a real failure (red). Both arrive
              as `note`, so the mode decides which one this is — flagging a
              deliberate group share in the same red as a failed syscall would
              teach operators to ignore both. */}
          {data.note && (
            <div
              className={advisoryOnly ? "card attention" : "error-box"}
              style={{ marginTop: 12 }}
            >
              {data.note}
            </div>
          )}

          {exposed && (
            <div className="card" style={{ marginTop: 12 }}>
              Anything above that is not <b>owner only</b> was set outside
              Laurelin, or predates the version that started creating these
              files privately. Laurelin removes world access by itself; it will
              not remove group access, because a group can be a set of
              principals you chose. To close it yourself:{" "}
              <code>chmod 600 metadata.db</code>, or set{" "}
              <code>LAURELIN_STRICT_FILE_MODE=1</code> and restart to have
              Laurelin do it on every open.
            </div>
          )}

          {data.strict_mode && (
            <div className="subtitle" style={{ marginTop: 12 }}>
              <code>LAURELIN_STRICT_FILE_MODE=1</code> is set: group access is
              stripped as well as world access.
            </div>
          )}
        </>
      ) : null}
    </section>
  );
}
