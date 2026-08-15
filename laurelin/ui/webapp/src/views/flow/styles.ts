// Styles for the flow builder, kept out of the view so the view reads as
// structure. Injected in a <style> the same way every other view in this app
// does it; the shared tokens (--bg-1, --border, --gold …) come from styles.css.

export const FLOW_STYLES = `
.fx-page { display: flex; flex-direction: column; gap: 12px; }

/* -------------------------------------------------------------- header */
.fx-head {
  display: flex; align-items: flex-end; gap: 16px;
  flex-wrap: wrap; padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.fx-head-left { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.fx-head-left h1 { margin: 0; font-size: 22px; }
.fx-head-left .faint { font-size: 12px; }
.fx-back {
  font-size: 11.5px; color: var(--text-faint); text-decoration: none;
  letter-spacing: 0.04em;
}
.fx-back:hover { color: var(--text-dim); }
.fx-head-right { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fx-dirty {
  font-size: 11px; color: var(--gold); letter-spacing: 0.06em;
  text-transform: uppercase;
}

/* -------------------------------------------------------------- notes */
.fx-note {
  font-size: 12.5px; line-height: 1.5; padding: 9px 12px;
  border-radius: 8px; border: 1px solid var(--border); background: var(--bg-1);
}
.fx-note-ok { color: var(--text-dim); }
.fx-note-bad { color: var(--red); background: rgba(224,102,95,0.08); border-color: #6b3330; }
.fx-note-warn { color: var(--gold); background: rgba(217,178,90,0.07); border-color: var(--gold-dim); }
.fx-note-gov { color: var(--text); background: rgba(91,155,213,0.08); border-color: #3a5b7d; }

/* -------------------------------------------------------------- list page */
.fx-onboard {
  border: 1px solid var(--border); border-radius: 12px;
  background: var(--bg-1); padding: 30px 32px; max-width: 640px;
}
.fx-onboard h2 { margin: 0 0 10px; font-size: 19px; }
.fx-onboard p { margin: 0 0 10px; font-size: 13.5px; line-height: 1.6; color: var(--text-dim); }
.fx-onboard button { margin-top: 10px; }
.fx-cards .card { display: flex; flex-direction: column; gap: 3px; }
.fx-card-head { display: flex; align-items: center; gap: 8px; justify-content: space-between; }

/* -------------------------------------------------------------- workbench */
.fx-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) 380px;
  gap: 18px; align-items: start;
}
@media (max-width: 1150px) { .fx-grid { grid-template-columns: minmax(0, 1fr); } }
.fx-col-head {
  font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--text-faint); margin-bottom: 8px;
}
.fx-steps { min-width: 0; }
.fx-panel {
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px; position: sticky; top: 14px;
}

/* -------------------------------------------------------------- step cards */
.fx-card {
  background: var(--bg-1); border: 1px solid var(--border);
  border-left: 3px solid var(--border-2);
  border-radius: 9px; padding: 10px 12px; cursor: pointer;
}
.fx-card:hover { border-color: var(--border-2); background: var(--bg-2); }
.fx-card.selected { background: var(--bg-2); border-color: var(--gold-dim); border-left-color: var(--gold); }
.fx-card.todo { border-left-color: var(--gold-dim); }
.fx-card.bad { border-left-color: var(--red); }
.fx-card.small { padding: 8px 10px; margin-top: 6px; }
.fx-card-top { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
.fx-num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 17px; height: 17px; border-radius: 50%;
  background: var(--bg-3); color: var(--text-dim);
  font-size: 10.5px; font-variant-numeric: tabular-nums;
}
.fx-icon { color: var(--gold-dim); flex: none; }
.fx-card-kind { color: var(--text); font-weight: 500; }
.fx-card-sum {
  margin-top: 4px; font-size: 12.5px; color: var(--text-dim);
  line-height: 1.45; word-break: break-word;
}
.fx-card-msg { margin-top: 6px; font-size: 11.5px; line-height: 1.45; }
.fx-card-msg.todo { color: var(--gold); }
.fx-card-msg.bad { color: var(--red); }
.fx-branch {
  margin-top: 9px; padding-left: 12px;
  border-left: 1px dashed var(--border-2);
}
.fx-branch-label {
  font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--text-faint);
}

.fx-connector { height: 18px; display: flex; justify-content: center; align-items: center; position: relative; }
.fx-connector::before {
  content: ""; position: absolute; top: 0; bottom: 0; width: 1px;
  background: var(--border-2);
}
.fx-plus {
  position: relative; z-index: 1;
  width: 20px; height: 20px; padding: 0; line-height: 1;
  border-radius: 50%; border: 1px solid var(--border-2);
  background: var(--bg); color: var(--text-dim); cursor: pointer;
  font-size: 13px;
}
.fx-plus:hover { color: var(--gold); border-color: var(--gold-dim); }

.fx-menu {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 6px; margin-bottom: 10px;
  border: 1px solid var(--border-2); border-radius: 9px;
  background: var(--bg-1); padding: 8px;
}
.fx-menu-item {
  display: flex; gap: 9px; align-items: flex-start; text-align: left;
  background: transparent; border: 1px solid transparent; border-radius: 7px;
  padding: 8px 9px; cursor: pointer; color: var(--text);
}
.fx-menu-item:hover { background: var(--bg-2); border-color: var(--border); }
.fx-menu-item strong { display: block; font-size: 12.5px; font-weight: 500; }
.fx-menu-item em { display: block; font-style: normal; font-size: 11.5px; color: var(--text-faint); line-height: 1.4; margin-top: 2px; }

/* -------------------------------------------------------------- forms */
.fx-form-head { display: flex; align-items: baseline; gap: 10px; }
.fx-form-title { font-size: 14px; font-weight: 500; }
.fx-form-blurb { font-size: 11.5px; color: var(--text-faint); margin: 4px 0 12px; line-height: 1.45; }
.fx-form .field { margin-bottom: 14px; }
.fx-form label { display: block; font-size: 11.5px; color: var(--text-dim); margin-bottom: 5px; }
.fx-in {
  background: var(--bg-2); color: var(--text);
  border: 1px solid var(--border-2); border-radius: 6px;
  padding: 4px 7px; font-size: 12px; font-family: inherit; max-width: 100%;
}
.fx-in:focus { outline: none; border-color: var(--gold-dim); }
.fx-wide { width: 100%; }
.fx-narrow { width: 74px; }
.fx-missing { border-color: var(--red); color: var(--red); }
.fx-missing-row span { color: var(--red); }
.fx-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.fx-aggrow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.fx-kw { font-size: 11.5px; color: var(--text-faint); }
.fx-checklist {
  max-height: 230px; overflow-y: auto; border: 1px solid var(--border);
  border-radius: 7px; padding: 7px 9px; background: var(--bg-2);
  display: flex; flex-direction: column; gap: 2px;
}
.fx-add {
  background: transparent; border: 1px dashed var(--border-2); border-radius: 6px;
  color: var(--text-dim); font-size: 11.5px; padding: 3px 8px; cursor: pointer;
}
.fx-add:hover { color: var(--gold); border-color: var(--gold-dim); }
.fx-x {
  background: transparent; border: none; color: var(--text-faint);
  cursor: pointer; font-size: 13px; padding: 0 5px; line-height: 1.4;
}
.fx-x:hover { color: var(--red); }
.fx-swap {
  background: transparent; border: 1px solid var(--border-2); border-radius: 5px;
  color: var(--text-faint); font-size: 10.5px; padding: 2px 5px; cursor: pointer;
}
.fx-swap:hover { color: var(--gold); border-color: var(--gold-dim); }

/* -------------------------------------------------------------- expressions */
.fx-cond, .fx-row-inline, .fx-operand, .fx-list {
  display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
}
.fx-cond { margin-bottom: 5px; }
.fx-value { display: flex; }
.fx-listitem { display: inline-flex; align-items: center; gap: 2px; }
.fx-group {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 9px; background: rgba(255,255,255,0.015);
}
.fx-group.depth-1, .fx-group.depth-2, .fx-group.depth-3 { margin-top: 5px; }
.fx-group-head { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; }
.fx-group-body { padding-left: 9px; border-left: 1px solid var(--border-2); }
.fx-group-actions { display: flex; gap: 6px; margin-top: 4px; }
.fx-hint { font-size: 11px; color: var(--text-faint); }
.fx-null { font-size: 11.5px; color: var(--text-faint); font-style: italic; }
.fx-ifelse { display: flex; flex-direction: column; gap: 5px; align-items: flex-start; }
.fx-ifrow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.fx-calc {
  border: 1px solid var(--border); border-radius: 7px; padding: 4px 6px;
  background: rgba(255,255,255,0.015);
}

/* -------------------------------------------------------------- checks */
.fx-expect { margin-top: 14px; }
.fx-disclose {
  background: transparent; border: none; color: var(--text-dim);
  font-size: 12px; cursor: pointer; padding: 4px 0;
}
.fx-disclose:hover { color: var(--text); }
.fx-expect-body {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; background: var(--bg-1);
}
.fx-expect-body p { font-size: 11.5px; margin: 0 0 8px; line-height: 1.45; }
.fx-desc { margin-top: 14px; }
.fx-desc label { display: block; font-size: 11.5px; color: var(--text-dim); margin-bottom: 5px; }

/* -------------------------------------------------------------- sql + preview */
.fx-sql {
  border: 1px solid var(--border); border-radius: 9px;
  background: var(--bg-1); padding: 12px 14px;
}
.fx-sql-head { font-size: 11.5px; color: var(--text-faint); margin-bottom: 8px; }
.fx-sql pre {
  margin: 0 0 8px; font-size: 12px; line-height: 1.55; overflow-x: auto;
  color: var(--text-dim); white-space: pre-wrap; word-break: break-word;
}

.fx-preview {
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-1); padding: 12px 14px; margin-top: 4px;
}
.fx-preview-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 9px; }
.fx-banner {
  font-size: 11.5px; line-height: 1.5; color: var(--text-dim);
  border-left: 2px solid var(--gold-dim); padding: 4px 0 4px 10px;
  margin-bottom: 10px;
}
.fx-preview .table-wrap { max-height: 340px; overflow: auto; }

/* -------------------------------------------------------------- modal */
.fx-modal { max-width: 540px; }
.fx-modal h2 { margin: 0 0 12px; font-size: 17px; }
.fx-modal p { font-size: 13px; line-height: 1.6; margin: 0 0 10px; color: var(--text-dim); }
.fx-modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
`;
