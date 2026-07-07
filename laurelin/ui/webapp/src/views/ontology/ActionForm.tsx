import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { API, api } from "../../api";
import type { ActionDef } from "../../types";
import { ErrorBox } from "../../ui";

/** Coerce a raw form value to the JSON type the parameter expects. */
function coerce(type: string, raw: string | boolean): unknown {
  if (type === "boolean") return Boolean(raw);
  if (type === "integer" || type === "float") {
    if (raw === "" || raw == null) return undefined;
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  return raw;
}

function inputType(paramType: string): "number" | "checkbox" | "text" {
  if (paramType === "integer" || paramType === "float") return "number";
  if (paramType === "boolean") return "checkbox";
  return "text";
}

/**
 * A single action rendered as a form card. On submit it POSTs to the apply
 * endpoint; `create` actions omit the pk, others target the selected object.
 */
export function ActionForm({
  action,
  type,
  selectedPk,
  canEdit,
}: {
  action: ActionDef;
  type: string;
  selectedPk: string | null;
  canEdit: boolean;
}) {
  const qc = useQueryClient();
  const canApply = canEdit;
  const params = Object.entries(action.parameters);

  // Raw form state keyed by parameter name. Strings for text/number inputs,
  // booleans for checkboxes.
  const [values, setValues] = useState<Record<string, string | boolean>>({});

  const mutation = useMutation({
    mutationFn: () => {
      const parameters: Record<string, unknown> = {};
      for (const [name, def] of params) {
        const raw = values[name] ?? (def.type === "boolean" ? false : "");
        const c = coerce(def.type, raw);
        if (c !== undefined) parameters[name] = c;
      }
      const body = {
        pk: action.kind === "create" ? undefined : (selectedPk ?? undefined),
        parameters,
      };
      return api.post(`${API}/ontology/actions/${action.api_name}/apply`, body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["objects", type] });
      if (selectedPk) {
        qc.invalidateQueries({ queryKey: ["object", type, selectedPk] });
      }
      setValues({});
    },
  });

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="card-title">{action.display_name || action.api_name}</div>
      {action.description && (
        <div className="dim" style={{ fontSize: 12.5, marginBottom: 10 }}>
          {action.description}
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canApply) mutation.mutate();
        }}
      >
        {params.map(([name, def]) => {
          const kind = inputType(def.type);
          const labelNode = (
            <label htmlFor={`${action.api_name}-${name}`}>
              {name}
              {def.required ? <span style={{ color: "var(--gold)" }}> *</span> : null}
            </label>
          );
          if (kind === "checkbox") {
            return (
              <div className="field" key={name}>
                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    textTransform: "none",
                    letterSpacing: 0,
                  }}
                >
                  <input
                    id={`${action.api_name}-${name}`}
                    type="checkbox"
                    style={{ width: "auto" }}
                    disabled={!canApply}
                    checked={Boolean(values[name])}
                    onChange={(e) =>
                      setValues((v) => ({ ...v, [name]: e.target.checked }))
                    }
                  />
                  {name}
                  {def.required ? <span style={{ color: "var(--gold)" }}> *</span> : null}
                </label>
                {def.description && <div className="hint">{def.description}</div>}
              </div>
            );
          }
          return (
            <div className="field" key={name}>
              {labelNode}
              <input
                id={`${action.api_name}-${name}`}
                type={kind}
                step={def.type === "float" ? "any" : undefined}
                required={def.required && canApply}
                disabled={!canApply}
                value={(values[name] as string) ?? ""}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [name]: e.target.value }))
                }
              />
              {def.description && <div className="hint">{def.description}</div>}
            </div>
          );
        })}

        {mutation.isError && <ErrorBox error={mutation.error} />}
        {mutation.isSuccess && (
          <div className="hint ok" style={{ marginBottom: 8 }}>
            Applied successfully.
          </div>
        )}

        {canApply ? (
          <button type="submit" className="primary small" disabled={mutation.isPending}>
            {mutation.isPending ? "Applying…" : "Apply"}
          </button>
        ) : (
          <div className="hint">
            Applying actions requires edit access to this object type.
          </div>
        )}
      </form>
    </div>
  );
}
