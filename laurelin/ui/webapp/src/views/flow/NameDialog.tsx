// The naming dialog shared by both Pipelines tabs.
//
// It used to live inside Flows.tsx, with a comment explaining that
// `window.prompt` — which is what the Python tab used for the same job —
// cannot show the rule, cannot say a name is already taken, and cannot say
// the one thing that matters (that the name is permanent). The comment was
// right and the Python tab shipped the prompt anyway, so one screen asked for
// a name in two different ways depending on which tab you were standing on.
// Extracting it here is what makes "delete window.prompt" a reuse rather than
// a second implementation. It lives under views/flow/ rather than in ui.tsx
// because it is a Pipelines dialog, not a shell primitive, and because
// Flows.tsx ↔ Pipelines.tsx ↔ Transforms.tsx are already a module cycle that
// importing across the tabs would deepen.

import { useState } from "react";
import type { ReactNode } from "react";
import { ErrorBox, Modal, NAME_RULE_NO_HYPHEN } from "../../ui";

/** Datasets and pipelines: lowercase, no hyphen. Both tabs, one regex. */
export const NAME_RE = /^[a-z][a-z0-9_]*$/;

/**
 * Name a pipeline (Visual tab: "New pipeline", "Duplicate") or a pipeline
 * file (Python tab: "New pipeline file").
 *
 * `noun` is what the thing being named is called, and it is not cosmetic: a
 * `.py` file genuinely declares SEVERAL pipelines, so calling the file a
 * "pipeline" would be false — which is also why the Python tab's delete
 * confirm says "the datasets" in the plural. Visual tab: "pipeline". Python
 * tab: "pipeline file".
 */
export function NameDialog({
  title,
  intro,
  confirmLabel,
  noun = "pipeline",
  placeholder = "late_orders",
  hint,
  taken,
  takenDatasets = [],
  busy,
  error,
  onCancel,
  onSubmit,
}: {
  title: string;
  intro: ReactNode;
  confirmLabel: string;
  /** What the named thing is, in prose: "pipeline", "pipeline file". */
  noun?: string;
  placeholder?: string;
  /** Overrides the default "…and it is also the dataset's name" sentence,
   *  which is true of a pipeline and false of a pipeline file. */
  hint?: ReactNode;
  taken: string[];
  /** Existing dataset names. A pipeline's name is also the name of the
   *  dataset it builds, so a name a dataset already owns would be refused by
   *  the server with a 409 — this refuses it at the dialog, with the reason. */
  takenDatasets?: string[];
  busy?: boolean;
  error?: unknown;
  onCancel: () => void;
  onSubmit: (name: string) => void;
}) {
  const [value, setValue] = useState("");

  const name = value.trim();
  const problem = !name
    ? null
    : !NAME_RE.test(name)
      ? NAME_RULE_NO_HYPHEN
      : taken.includes(name)
        ? `There is already a ${noun} called ${name}.`
        : takenDatasets.includes(name)
          ? `There is already a dataset called ${name}, and a pipeline shares its name with the dataset it builds. Pick another name.`
          : null;
  const ok = !!name && !problem;

  return (
    <Modal label={title} onClose={onCancel} width={540}>
      <div className="fx-modal">
        <h2>{title}</h2>
        <p>{intro}</p>
        <div className="field">
          <label htmlFor="fx-name-input">Name</label>
          <input
            id="fx-name-input"
            className="fx-in fx-wide"
            value={value}
            placeholder={placeholder}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && ok && !busy) onSubmit(name);
            }}
          />
          {problem ? (
            <div className="hint bad">{problem}</div>
          ) : (
            <div className="hint">
              {hint ?? (
                <>
                  {NAME_RULE_NO_HYPHEN} This is also the name of the dataset it produces, and it
                  cannot be changed later.
                </>
              )}
            </div>
          )}
        </div>
        {error != null && <ErrorBox error={error} />}
        <div className="fx-modal-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!ok || busy}
            onClick={() => onSubmit(name)}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  );
}
