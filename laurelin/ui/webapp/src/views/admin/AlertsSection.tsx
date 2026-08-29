// Admin > Alert webhooks: outbound delivery for health transitions.
//
// Off by default, none configured — outbound delivery is an exfiltration
// surface and defaults closed. The URL is a credential (a Slack-style URL
// carries its secret in the path), so it is WRITE-ONLY: the server never
// echoes it back, reads render as "configured — withheld" (the EnginesSection
// convention), never as a blank an admin would re-type over.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { AlertWebhook, Failure } from "../../types";
import {
  Badge,
  EmptyState,
  ErrorBox,
  FailureBadge,
  FailureNote,
  Spinner,
  fmtTime,
} from "../../ui";
import { InlineError } from "./shared";

const NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const EVENTS = [
  "dataset_failing",
  "dataset_overdue",
  "dataset_stale",
  "dataset_healthy",
] as const;

function CreateWebhookPanel({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [datasets, setDatasets] = useState("");
  const [events, setEvents] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: () =>
      api.put(`${API}/alerts/webhooks/${encodeURIComponent(name)}`, {
        url,
        datasets: datasets
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        events,
        // Deliberately created disabled: configuring a destination and
        // opening the tap are two separate admin decisions.
        enabled: false,
      }),
    onSuccess: () => {
      setName("");
      setUrl("");
      setDatasets("");
      setEvents([]);
      onSaved();
    },
  });

  const nameValid = NAME_RE.test(name);
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <label style={{ marginBottom: 8 }}>Add a webhook</label>
      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
        <div className="field">
          <label>Name</label>
          <input
            className="mono"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="oncall-slack"
            style={{ maxWidth: 160 }}
          />
        </div>
        <div className="field" style={{ flex: "1 1 280px" }}>
          <label>URL (write-only — it will read back withheld)</label>
          <input
            className="mono"
            type="password"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://hooks.example.com/…"
            autoComplete="off"
          />
        </div>
      </div>
      <div className="toolbar" style={{ gap: 12, flexWrap: "wrap" }}>
        <div className="field" style={{ flex: "1 1 280px" }}>
          <label>Datasets (comma-separated; empty = all)</label>
          <input
            className="mono"
            value={datasets}
            onChange={(e) => setDatasets(e.target.value)}
            placeholder="orders_clean, revenue_daily"
          />
        </div>
        <div className="field">
          <label>Events (none checked = all)</label>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {EVENTS.map((ev) => (
              <label key={ev} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12 }}>
                <input
                  type="checkbox"
                  checked={events.includes(ev)}
                  onChange={(e) =>
                    setEvents(
                      e.target.checked
                        ? [...events, ev]
                        : events.filter((x) => x !== ev),
                    )
                  }
                />
                {ev.replace("dataset_", "")}
              </label>
            ))}
          </div>
        </div>
      </div>
      {name !== "" && !nameValid && (
        <p className="hint">Lowercase letters, digits, `-` and `_`; start with a letter.</p>
      )}
      <InlineError err={create.error} />
      <div className="toolbar" style={{ justifyContent: "flex-end" }}>
        <button
          className="primary"
          disabled={!nameValid || url.trim() === "" || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Saving…" : "Save webhook (disabled)"}
        </button>
      </div>
    </div>
  );
}

function TestButton({ name }: { name: string }) {
  const test = useMutation({
    mutationFn: () =>
      api.post<{ ok: boolean; status?: number; failure?: Failure }>(
        `${API}/alerts/webhooks/${encodeURIComponent(name)}/test`,
        {},
      ),
  });
  return (
    <span style={{ display: "grid", gap: 6, justifyItems: "start" }}>
      <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
        <button className="small" disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? "Sending…" : "Test"}
        </button>
        {test.data?.ok && <Badge tone="green">delivered {test.data.status}</Badge>}
        {test.data && !test.data.ok && test.data.failure && (
          <FailureBadge failure={test.data.failure} />
        )}
      </span>
      {test.data && !test.data.ok && test.data.failure && (
        <FailureNote failure={test.data.failure} />
      )}
      <InlineError err={test.error} />
    </span>
  );
}

export function AlertsSection() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["alert-webhooks"] });
  const q = useQuery({
    queryKey: ["alert-webhooks"],
    queryFn: () => api.get<AlertWebhook[]>(`${API}/alerts/webhooks`),
  });

  const toggle = useMutation({
    mutationFn: (w: AlertWebhook) =>
      api.put(`${API}/alerts/webhooks/${encodeURIComponent(w.name)}`, {
        // No `url` key: the server keeps the stored secret. Sending the
        // withheld marker back would overwrite the credential with garbage.
        datasets: w.datasets,
        events: w.events,
        enabled: !w.enabled,
      }),
    onSettled: invalidate,
  });

  const remove = useMutation({
    mutationFn: (name: string) =>
      api.del(`${API}/alerts/webhooks/${encodeURIComponent(name)}`),
    onSettled: invalidate,
  });

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ fontSize: 15, marginBottom: 4 }}>Alert webhooks</h2>
      <p className="dim" style={{ fontSize: 12.5, marginTop: 0 }}>
        Outbound POSTs when a dataset turns failing, overdue or stale (and once
        on recovery). <b>Off by default.</b> The payload is exactly the
        viewer-level health record — codes, names and timestamps; never
        expectation messages, measured values, row counts or cursor values.
      </p>

      <CreateWebhookPanel onSaved={invalidate} />

      {q.isLoading && <Spinner />}
      {q.isError && <ErrorBox error={q.error} />}
      {(toggle.isError || remove.isError) && (
        <InlineError err={toggle.error ?? remove.error} />
      )}
      {q.data &&
        (q.data.length === 0 ? (
          <EmptyState>No webhooks configured — nothing leaves this server.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>URL</th>
                  <th>Scope</th>
                  <th>Status</th>
                  <th>Last delivery</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {q.data.map((w) => (
                  <tr key={w.name}>
                    <td className="mono">{w.name}</td>
                    <td>
                      {/* Write-only by design: "configured — withheld", never
                          blank (blank reads as unset and invites a re-type). */}
                      <span
                        className="faint"
                        title="The URL is a credential; the server stores it and never sends it back. To change it, save the webhook again with a new URL."
                      >
                        configured — withheld
                      </span>
                    </td>
                    <td className="dim" style={{ fontSize: 12 }}>
                      {w.datasets.length === 0 ? "all datasets" : w.datasets.join(", ")}
                      {" · "}
                      {w.events.length === 0
                        ? "all events"
                        : w.events.map((e) => e.replace("dataset_", "")).join(", ")}
                    </td>
                    <td>
                      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <Badge tone={w.enabled ? "green" : "neutral"}>
                          {w.enabled ? "enabled" : "disabled"}
                        </Badge>
                        <button
                          className="small"
                          disabled={toggle.isPending}
                          onClick={() => toggle.mutate(w)}
                        >
                          {w.enabled ? "Disable" : "Enable"}
                        </button>
                      </div>
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {w.last_delivery_at == null ? (
                        <span className="faint">never</span>
                      ) : w.last_delivery_failure ? (
                        <span title={fmtTime(w.last_delivery_at)}>
                          <FailureBadge failure={w.last_delivery_failure} />
                        </span>
                      ) : (
                        <span className="dim" title={fmtTime(w.last_delivery_at)}>
                          HTTP {w.last_delivery_status}
                        </span>
                      )}
                    </td>
                    <td>
                      <span className="toolbar" style={{ gap: 8, justifyContent: "flex-end" }}>
                        <TestButton name={w.name} />
                        <button
                          className="small"
                          onClick={() => {
                            if (confirm(`Delete webhook ${w.name}?`)) remove.mutate(w.name);
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
