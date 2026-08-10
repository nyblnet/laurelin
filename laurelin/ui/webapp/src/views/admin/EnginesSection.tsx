// Admin > Delegated engines: Flight SQL endpoints a @remote_transform submits
// SQL to. Laurelin runs no cluster — it stores the reduced result with lineage
// and policy intact.
//
// Admin-only: an engine URI carries credentials, so configs are redacted in
// every response (a password comes back as "*****") and validated before they
// are stored.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import { Badge, EmptyState, ErrorBox, RedactedValue, Spinner } from "../../ui";
import { InlineError } from "./shared";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;

interface Engine {
  name: string;
  type: string;
  uri: string;
  options: Record<string, string>;
  created_by?: string;
}

function CreateEnginePanel({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [uri, setUri] = useState("");
  // Options are string→string; the common ones are username/password, so the
  // form offers those two rather than a raw key/value editor nobody wants.
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const create = useMutation({
    mutationFn: () => {
      const options: Record<string, string> = {};
      if (username.trim()) options.username = username.trim();
      if (password) options.password = password;
      return api.put(`${API}/engines/${name}`, { type: "flightsql", uri, options });
    },
    onSuccess: () => {
      setName("");
      setUri("");
      setUsername("");
      setPassword("");
      onCreated();
    },
  });

  const nameValid = NAME_RE.test(name);

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <label style={{ marginBottom: 8 }}>Register a Flight SQL engine</label>
      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
        <div className="field">
          <label>Name</label>
          <input className="mono" value={name} onChange={(e) => setName(e.target.value)}
            placeholder="trino" style={{ maxWidth: 160 }} />
        </div>
        <div className="field" style={{ flex: "1 1 280px" }}>
          <label>Flight SQL URI</label>
          <input className="mono" value={uri} onChange={(e) => setUri(e.target.value)}
            placeholder="grpc+tls://trino.internal:443" />
        </div>
      </div>
      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
        <div className="field">
          <label>Username (optional)</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label>Password (optional)</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
      </div>
      {create.isError && <InlineError err={create.error} />}
      {name !== "" && !nameValid && (
        <p className="hint">Lowercase letters, digits, `-` and `_`; start with a letter.</p>
      )}
      <div className="toolbar" style={{ justifyContent: "flex-end" }}>
        <button
          className="primary"
          disabled={!nameValid || uri.trim() === "" || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Saving…" : "Register engine"}
        </button>
      </div>
    </div>
  );
}

function TestButton({ name }: { name: string }) {
  // A "does it work" probe matters more here than anywhere: an engine's whole
  // job is to be reachable, and the alternative to testing it is finding out
  // when a 2am build fails.
  const test = useMutation({
    mutationFn: () =>
      api.post<{ ok: boolean; detail?: string; withheld?: boolean }>(
        `${API}/engines/${name}/test`,
        {},
      ),
  });
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <button className="small" disabled={test.isPending} onClick={() => test.mutate()}>
        {test.isPending ? "Testing…" : "Test"}
      </button>
      {test.data?.ok && <Badge tone="green">reachable</Badge>}
      {test.data && !test.data.ok && (
        <span
          // The driver's own words when they were safe to repeat. When they
          // were not, the server says so rather than showing a half-read
          // message — the full text is in the server log.
          title={
            test.data.withheld
              ? "The engine's error text was withheld: it could not be redacted safely, so none of it was sent. The full message is in the server log."
              : test.data.detail
          }
          className="mono"
          style={{ color: "var(--red)", fontSize: 12 }}
        >
          unreachable{test.data.withheld ? " (details withheld)" : ""}
        </span>
      )}
    </span>
  );
}

export function EnginesSection() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["engines"],
    queryFn: () => api.get<Engine[]>(`${API}/engines`),
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["engines"] });
  const remove = useMutation({
    mutationFn: (name: string) => api.del(`${API}/engines/${name}`),
    onSettled: invalidate,
  });

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ fontSize: 15, marginBottom: 4 }}>Delegated engines</h2>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        Flight SQL endpoints (Trino, Dremio, Databricks) that a{" "}
        <code>@remote_transform</code> runs on. Laurelin stores the reduced result.
      </p>

      <CreateEnginePanel onCreated={invalidate} />

      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}
      {remove.isError && <InlineError err={remove.error} />}
      {q.data &&
        (q.data.length === 0 ? (
          <EmptyState>No engines configured.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>URI</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {q.data.map((e) => (
                  <tr key={e.name}>
                    <td className="mono">{e.name}</td>
                    <td className="mono dim">
                      {/* The URI is redacted server-side, and when its shape
                          could not be parsed it is withheld entirely — which
                          has to look different from an engine with no URI. */}
                      <RedactedValue value={e.uri} />
                    </td>
                    <td>
                      <span className="toolbar" style={{ gap: 8, justifyContent: "flex-end" }}>
                        <TestButton name={e.name} />
                        <button
                          className="small"
                          onClick={() => {
                            if (confirm(`Delete engine ${e.name}?`)) remove.mutate(e.name);
                          }}
                        >
                          Delete
                        </button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </section>
  );
}
