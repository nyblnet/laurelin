// Renders the REAL shared primitives from ui.tsx — imported from the source
// tree, never copied — and prints one JSON object of markup strings for
// tests/test_ui_primitives.py to assert against. react-dom/server runs no
// effects, so what is pinned here is exactly the accessibility *markup*
// contract screen implementers build against: roles, aria attributes,
// tabbability, and the failure-copy sentences. Behavior that lives in
// effects and event handlers (focus trap, Escape, Enter activation) is
// necessarily pinned at source instead — see the companion test file.
//
// Bundled at test time with the webapp's own esbuild (no vitest, no second
// test framework — see tests/test_charts_render.py for why).
import { renderToStaticMarkup } from "react-dom/server";
import {
  DataTable,
  ErrorBox,
  FailureBadge,
  FailureNote,
  LiveStatus,
  Modal,
  RedactedValue,
  WITHHELD,
  Withheld,
} from "../../laurelin/ui/webapp/src/ui";
import { ApiError } from "../../laurelin/ui/webapp/src/api";
import type { Failure } from "../../laurelin/ui/webapp/src/types";

const out: Record<string, unknown> = {};

function render(el: JSX.Element): string {
  try {
    return renderToStaticMarkup(el);
  } catch (e) {
    return `CRASH: ${String(e)}`;
  }
}

// ----------------------------------------------------------------- modal

out.modal = render(
  <Modal label="Rename dataset" onClose={() => {}}>
    <h2>Rename</h2>
    <button>OK</button>
  </Modal>,
);

// ---------------------------------------------------------------- failures

const noRef: Failure = { code: "remote_failed", subject: "orders_sync" };
const withRef: Failure = {
  code: "remote_failed",
  subject: "orders_sync",
  detail_ref: "err-0123456789ab",
};

out.failure_note_viewer_noref = render(<FailureNote failure={noRef} role="viewer" />);
out.failure_note_editor_noref = render(<FailureNote failure={noRef} role="editor" />);
out.failure_note_admin_noref = render(<FailureNote failure={noRef} role="admin" />);
// No role passed: the component must not risk the your-role-sees-less lie.
out.failure_note_unknown_noref = render(<FailureNote failure={noRef} />);
out.failure_note_editor_ref = render(<FailureNote failure={withRef} role="editor" />);

// A task skipped because its upstream failed names the upstream — subject is
// in the viewer projection, so even the narrowest reader is told where to
// look — and carries no log pointer, because nothing here ran or raised.
out.blocked_note = render(
  <FailureNote
    failure={{ code: "blocked_by_upstream", subject: "upstream_fail" }}
    role="editor"
  />,
);

// The pipeline's code raising is attributed to the pipeline, not Laurelin.
out.transform_note = render(
  <FailureNote
    failure={{ code: "transform_failed", subject: "aviation.enrich", exc_class: "KeyError" }}
    role="editor"
  />,
);

out.failure_badge = render(
  <FailureBadge failure={{ code: "auth_rejected", subject: "warehouse" }} />,
);

// ---------------------------------------------------------------- withheld

out.withheld = render(<Withheld what="the connector's configuration" role="admin" />);
out.redacted = render(<RedactedValue value={WITHHELD} />);
out.redacted_plain = render(<RedactedValue value="postgres" />);

// --------------------------------------------------------------- datatable

const rows = [{ name: "flights" }, { name: "orders" }];
const cols = [{ label: "Name", render: (r: { name: string }) => r.name }];

out.table_clickable = render(
  <DataTable columns={cols} rows={rows} rowKey={(r) => r.name} onRowClick={() => {}} />,
);
out.table_plain = render(<DataTable columns={cols} rows={rows} rowKey={(r) => r.name} />);

// ---------------------------------------------------------------- feedback

out.errorbox_retry = render(
  <ErrorBox error={new ApiError(503, "the engine is briefly out of capacity")} onRetry={() => {}} />,
);
out.errorbox_plain = render(<ErrorBox error={new ApiError(503, "busy")} />);
out.live_status = render(<LiveStatus>1,204 rows</LiveStatus>);

process.stdout.write(JSON.stringify(out));
