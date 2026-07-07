/* Laurelin single-page UI. Vanilla JS, no external resources, no innerHTML of
   user data — all content is built via DOM nodes / textContent. */
"use strict";

/* ---------------------------------------------------------------- helpers */

const API = "/api/v1";

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "class") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (k === "checked" || k === "disabled" || k === "required") {
        if (v) node.setAttribute(k, "");
      } else {
        node.setAttribute(k, v);
      }
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const child of children.flat(Infinity)) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fmtNum(n) {
  return n == null ? "—" : Number(n).toLocaleString("en-US");
}

function fmtTime(ts) {
  if (!ts) return "—";
  return String(ts).replace("T", " ").slice(0, 19);
}

function fmtValue(v) {
  if (v == null) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/* Sessions ride on the httpOnly `laurelin_session` cookie; fetch's default
   credentials mode ("same-origin") sends it automatically. No tokens in JS. */
async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers);
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(path, Object.assign({}, options, { headers }));
  } catch (e) {
    throw { status: 0, detail: "Network error — is the Laurelin server running? (" + e.message + ")" };
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error body */ }
    // Global 401 handling: any unauthorized response while inside the app
    // means the session died (expired / user disabled) — drop back to login.
    // While an auth screen is showing (login/setup), 401s are expected and
    // handled locally by the form.
    if (res.status === 401 && !document.body.classList.contains("auth-mode")) {
      onSessionExpired();
    }
    throw { status: res.status, detail };
  }
  if (res.status === 204) return null;
  return res.json();
}

function errorBox(err) {
  const box = h("div", { class: "error-box" });
  if (err && err.status === 403) {
    const detail = err.detail && err.detail !== "Forbidden" ? " — " + err.detail : "";
    box.append(h("div", {}, "Insufficient permissions (403)" + detail));
  } else {
    const status = err && err.status ? "Error " + err.status : "Error";
    box.append(h("div", {}, status + ": " + ((err && err.detail) || String(err))));
  }
  return box;
}

function emptyState(text) {
  return h("div", { class: "empty" }, text);
}

function loading() {
  return h("div", { class: "loading" }, "Loading…");
}

function dataTable(columns, rows, opts = {}) {
  const thead = h("thead", {}, h("tr", {}, columns.map((c) => h("th", {}, c.label))));
  const tbody = h("tbody", {},
    rows.map((row) => {
      const tr = h("tr", {}, columns.map((c) => c.render(row)));
      if (opts.onRowClick) {
        tr.classList.add("clickable");
        tr.addEventListener("click", () => opts.onRowClick(row, tr));
      }
      if (opts.isSelected && opts.isSelected(row)) tr.classList.add("selected");
      return tr;
    }),
  );
  return h("div", { class: "table-wrap" }, h("table", {}, thead, tbody));
}

/* ---------------------------------------------------------------- routing */

const mainEl = document.getElementById("main");

function parseRoute() {
  const parts = (location.hash || "#/datasets").slice(2).split("/").filter(Boolean)
    .map(decodeURIComponent);
  return { view: parts[0] || "datasets", args: parts.slice(1) };
}

const VIEWS = {
  datasets: renderDatasets,
  pipeline: renderPipeline,
  ontology: renderOntology,
  audit: renderAudit,
  admin: renderAdmin,
};

let renderSeq = 0;

async function render() {
  if (document.body.classList.contains("auth-mode")) return; // login/setup showing
  const seq = ++renderSeq;
  const route = parseRoute();
  for (const a of document.querySelectorAll("#nav a")) {
    a.classList.toggle("active", a.dataset.view === route.view);
  }
  mainEl.replaceChildren(loading());
  const fn = VIEWS[route.view] || renderDatasets;
  try {
    const content = await fn(route.args);
    if (seq !== renderSeq) return; // a newer navigation superseded this one
    mainEl.replaceChildren(content);
  } catch (err) {
    if (seq !== renderSeq) return;
    mainEl.replaceChildren(h("h1", {}, "Laurelin"), errorBox(err));
  }
}

window.addEventListener("hashchange", render);

/* ------------------------------------------------------- workspace footer */

async function loadWorkspaceInfo() {
  const foot = document.getElementById("workspace-info");
  try {
    const [ws, health] = await Promise.all([api(API + "/workspace"), api("/health")]);
    foot.replaceChildren(
      h("div", { class: "ws-name" }, ws.name || "workspace"),
      h("div", { class: "mono" }, ws.root || ""),
      h("div", {}, "laurelin " + (health.version || "")),
    );
  } catch {
    foot.replaceChildren(h("div", {}, "server unreachable"));
  }
}

/* ---------------------------------------------------------- datasets view */

async function renderDatasets(args) {
  if (args.length) return renderDatasetDetail(args[0]);

  const datasets = await api(API + "/datasets");
  const wrap = h("div", {},
    h("h1", {}, "Datasets"),
    h("div", { class: "subtitle" }, "Versioned Parquet datasets in this workspace."),
  );
  if (!datasets.length) {
    wrap.append(emptyState("No datasets yet. Create one via the API or `laurelin upload`."));
    return wrap;
  }

  // Latest row counts live on version records; fetch details in parallel.
  const details = await Promise.all(datasets.map((d) =>
    api(API + "/datasets/" + encodeURIComponent(d.name)).catch(() => null),
  ));
  const latestRows = new Map();
  details.forEach((det, i) => {
    if (!det || !Array.isArray(det.versions)) return;
    const latest = det.versions.find((v) => v.version === det.latest_version) ||
      det.versions[det.versions.length - 1];
    if (latest) latestRows.set(datasets[i].name, latest.row_count);
  });

  wrap.append(dataTable([
    { label: "Name", render: (d) => h("td", { class: "mono" }, d.name) },
    { label: "Description", render: (d) => h("td", { class: "dim" }, d.description || "—") },
    { label: "Latest", render: (d) => h("td", { class: "num" }, d.latest_version == null ? "—" : "v" + d.latest_version) },
    { label: "Rows", render: (d) => h("td", { class: "num" }, fmtNum(latestRows.get(d.name))) },
    { label: "Created", render: (d) => h("td", { class: "dim nowrap" }, fmtTime(d.created_at)) },
  ], datasets, {
    onRowClick: (d) => { location.hash = "#/datasets/" + encodeURIComponent(d.name); },
  }));
  return wrap;
}

async function renderDatasetDetail(name) {
  const [detail, schema] = await Promise.all([
    api(API + "/datasets/" + encodeURIComponent(name)),
    api(API + "/datasets/" + encodeURIComponent(name) + "/schema").catch(() => []),
  ]);

  const wrap = h("div", {},
    h("div", { class: "crumb" }, h("a", { href: "#/datasets" }, "Datasets"), h("span", {}, " / " + name)),
    h("h1", { class: "mono" }, name),
    h("div", { class: "subtitle" }, detail.description || "No description."),
  );

  wrap.append(h("h2", {}, "Schema"));
  wrap.append(schema.length
    ? dataTable([
        { label: "Column", render: (c) => h("td", { class: "mono" }, c.name) },
        { label: "Type", render: (c) => h("td", { class: "mono dim" }, c.type) },
      ], schema)
    : emptyState("No schema yet — this dataset has no versions."));

  wrap.append(h("h2", {}, "Versions"));
  const versions = (detail.versions || []).slice().sort((a, b) => b.version - a.version);
  wrap.append(versions.length
    ? dataTable([
        { label: "Version", render: (v) => h("td", { class: "num" }, "v" + v.version) },
        { label: "Rows", render: (v) => h("td", { class: "num" }, fmtNum(v.row_count)) },
        { label: "Source", render: (v) => h("td", {}, h("span", { class: "badge" }, v.source)) },
        { label: "Build", render: (v) => h("td", { class: "mono dim" }, v.build_id ? v.build_id.slice(0, 8) : "—") },
        { label: "Created", render: (v) => h("td", { class: "dim nowrap" }, fmtTime(v.created_at)) },
      ], versions)
    : emptyState("No versions yet."));

  wrap.append(h("h2", {}, "Row preview"));
  const previewHost = h("div", {});
  wrap.append(previewHost);
  if (versions.length) {
    renderRowPreview(previewHost, name, 0);
  } else {
    previewHost.append(emptyState("Nothing to preview."));
  }

  if (canEdit()) {
    wrap.append(h("h2", {}, "Upload"));
    wrap.append(h("div", { class: "hint" },
      "Append a new version from a CSV or Parquet file: ",
      h("code", {}, "laurelin upload " + name + " path/to/file.csv"),
      " — or POST multipart to ",
      h("code", {}, "/api/v1/datasets/" + name + "/upload"),
      ".",
    ));
  }
  return wrap;
}

const PAGE_SIZE = 50;

async function renderRowPreview(host, name, offset) {
  host.replaceChildren(loading());
  let data;
  try {
    data = await api(API + "/datasets/" + encodeURIComponent(name) +
      "/rows?limit=" + PAGE_SIZE + "&offset=" + offset);
  } catch (err) {
    host.replaceChildren(errorBox(err));
    return;
  }
  const rows = data.rows || [];
  const total = data.row_count != null ? data.row_count : rows.length;
  if (!rows.length && offset === 0) {
    host.replaceChildren(emptyState("Dataset is empty."));
    return;
  }
  const cols = rows.length ? Object.keys(rows[0]) : [];
  const table = dataTable(
    cols.map((c) => ({ label: c, render: (r) => h("td", { class: "mono" }, fmtValue(r[c])) })),
    rows,
  );
  const last = Math.min(offset + rows.length, total);
  const pager = h("div", { class: "pager" },
    h("button", { class: "small", disabled: offset === 0,
      onClick: () => renderRowPreview(host, name, Math.max(0, offset - PAGE_SIZE)) }, "‹ Prev"),
    h("button", { class: "small", disabled: last >= total,
      onClick: () => renderRowPreview(host, name, offset + PAGE_SIZE) }, "Next ›"),
    h("span", { class: "range" }, `${offset + 1}–${last} of ${fmtNum(total)}`),
  );
  host.replaceChildren(table, pager);
}

/* ---------------------------------------------------------- pipeline view */

async function renderPipeline() {
  const [lineage, transforms, builds] = await Promise.all([
    api(API + "/lineage"),
    api(API + "/transforms").catch(() => []),
    api(API + "/builds").catch(() => []),
  ]);

  const wrap = h("div", {},
    h("h1", {}, "Pipeline"),
    h("div", { class: "subtitle" }, "Transform DAG and build history."),
  );

  wrap.append(h("h2", {}, "Lineage"));
  wrap.append((lineage.nodes || []).length
    ? h("div", { class: "graph-wrap" }, lineageSvg(lineage))
    : emptyState("No lineage yet — run a build to populate the graph."));

  wrap.append(h("h2", {}, "Transforms"));
  wrap.append(transforms.length
    ? dataTable([
        { label: "Name", render: (t) => h("td", { class: "mono" }, t.name) },
        { label: "Kind", render: (t) => h("td", {}, h("span", { class: "badge" }, t.kind)) },
        { label: "Inputs", render: (t) => h("td", { class: "mono dim" }, (t.inputs || []).join(", ") || "—") },
        { label: "Output", render: (t) => h("td", { class: "mono" }, t.output) },
      ], transforms)
    : emptyState("No transforms registered. Add *.py files under pipelines/."));

  const historyHost = h("div", {});
  wrap.append(h("h2", {}, "Builds"));
  if (canEdit()) {
    const buildBtn = h("button", { class: "primary" }, "Run build");
    const buildMsg = h("span", { class: "dim" });
    buildBtn.addEventListener("click", async () => {
      buildBtn.disabled = true;
      buildMsg.textContent = "Building…";
      try {
        const info = await api(API + "/builds", { method: "POST", body: JSON.stringify({}) });
        buildMsg.textContent = "Build " + (info.id || "").slice(0, 8) + " " + info.status;
        render();
      } catch (err) {
        buildBtn.disabled = false;
        buildMsg.textContent = "";
        wrap.insertBefore(errorBox(err), historyHost);
      }
    });
    wrap.append(h("div", { class: "toolbar" }, buildBtn, buildMsg));
  } else {
    wrap.append(h("div", { class: "ro-note" },
      "Read-only role — builds are visible here, but only editors and admins can run them."));
  }
  wrap.append(historyHost);
  if (!builds.length) {
    historyHost.append(emptyState("No builds yet."));
  } else {
    const sorted = builds.slice().sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
    for (const b of sorted.slice(0, 20)) historyHost.append(buildItem(b));
  }
  return wrap;
}

function buildItem(build) {
  const tasks = build.tasks || [];
  const taskList = h("div", { class: "build-tasks" },
    tasks.map((t) => h("div", { class: "task-row" },
      h("span", { class: "t-name" }, t.transform_name),
      h("span", { class: "badge " + t.status }, t.status),
      h("span", { class: "mono dim" }, "→ " + t.output_dataset +
        (t.output_version != null ? " v" + t.output_version : "") +
        (t.rows_written != null ? " · " + fmtNum(t.rows_written) + " rows" : "")),
      t.error ? h("span", { class: "t-err" }, t.error) : null,
    )),
  );
  if (tasks.length) taskList.style.display = "none";

  const head = h("div", { class: "build-head" },
    h("span", { class: "badge " + build.status }, build.status),
    h("span", { class: "bid" }, (build.id || "").slice(0, 8)),
    h("span", { class: "dim nowrap" }, fmtTime(build.started_at)),
    h("span", { class: "faint" }, tasks.length + " task" + (tasks.length === 1 ? "" : "s")),
    build.error ? h("span", { class: "t-err" }, build.error) : null,
  );
  head.addEventListener("click", () => {
    taskList.style.display = taskList.style.display === "none" ? "" : "none";
  });
  return h("div", { class: "build-item" }, head, tasks.length ? taskList : null);
}

/* Layered left→right layout: layer = longest path from any source node. */
function lineageSvg(lineage) {
  const nodes = lineage.nodes || [];
  const edges = (lineage.edges || []).filter((e) => e.from !== e.to);
  const incoming = new Map(nodes.map((n) => [n.id, []]));
  for (const e of edges) {
    if (incoming.has(e.to) && incoming.has(e.from)) incoming.get(e.to).push(e.from);
  }
  const layerOf = new Map();
  const visiting = new Set();
  function layer(id) {
    if (layerOf.has(id)) return layerOf.get(id);
    if (visiting.has(id)) return 0; // cycle guard
    visiting.add(id);
    const preds = incoming.get(id) || [];
    const value = preds.length ? Math.max(...preds.map(layer)) + 1 : 0;
    visiting.delete(id);
    layerOf.set(id, value);
    return value;
  }
  nodes.forEach((n) => layer(n.id));

  const layers = [];
  for (const n of nodes) {
    const l = layerOf.get(n.id) || 0;
    (layers[l] = layers[l] || []).push(n);
  }
  for (let i = 0; i < layers.length; i++) layers[i] = layers[i] || [];
  const NODE_W = 156, NODE_H = 30, GAP_X = 84, GAP_Y = 18, PAD = 16;
  const pos = new Map();
  layers.forEach((col, li) => {
    col.sort((a, b) => a.id.localeCompare(b.id));
    col.forEach((n, i) => {
      pos.set(n.id, {
        x: PAD + li * (NODE_W + GAP_X),
        y: PAD + i * (NODE_H + GAP_Y),
        node: n,
      });
    });
  });
  const width = PAD * 2 + layers.length * NODE_W + Math.max(0, layers.length - 1) * GAP_X;
  const height = PAD * 2 + Math.max(1, ...layers.map((c) => c.length)) * (NODE_H + GAP_Y) - GAP_Y;

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width, height,
    role: "img", "aria-label": "Lineage graph",
  });
  const marker = svgEl("marker", {
    id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4,
    markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
  }, svgEl("path", { d: "M0,0 L8,4 L0,8 Z", fill: "#5c6572" }));
  svg.append(svgEl("defs", {}, marker));

  for (const e of edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) continue;
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
    const x2 = b.x, y2 = b.y + NODE_H / 2;
    const mx = (x1 + x2) / 2;
    svg.append(svgEl("path", {
      class: "gedge",
      d: `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2 - 3} ${y2}`,
      "marker-end": "url(#arrow)",
    }));
  }

  for (const { x, y, node } of pos.values()) {
    const isTransform = node.type === "transform";
    const g = svgEl("g", { class: "gnode gnode-" + (isTransform ? "transform" : "dataset") });
    g.append(svgEl("rect", {
      x, y, width: NODE_W, height: NODE_H,
      rx: isTransform ? NODE_H / 2 : 5,
    }));
    const label = node.id.length > 20 ? node.id.slice(0, 19) + "…" : node.id;
    const text = svgEl("text", {
      x: x + NODE_W / 2, y: y + NODE_H / 2 + 4, "text-anchor": "middle",
    }, label);
    g.append(text, svgEl("title", {}, node.id + " (" + node.type + ")"));
    if (!isTransform) {
      g.style.cursor = "pointer";
      g.addEventListener("click", () => { location.hash = "#/datasets/" + encodeURIComponent(node.id); });
    }
    svg.append(g);
  }
  return svg;
}

/* ---------------------------------------------------------- ontology view */

async function renderOntology(args) {
  const types = await api(API + "/ontology/object-types");
  const selected = args[0] || null;

  const wrap = h("div", {},
    h("h1", {}, "Ontology"),
    h("div", { class: "subtitle" }, "Object types, links and actions over your data."),
  );
  if (!types.length) {
    wrap.append(emptyState("No object types defined. Add YAML files under ontology/."));
    return wrap;
  }

  wrap.append(h("div", { class: "cards" }, types.map((t) =>
    h("div", {
      class: "card" + (t.api_name === selected ? " selected" : ""),
      onClick: () => { location.hash = "#/ontology/" + encodeURIComponent(t.api_name); },
    },
      h("div", { class: "card-title" }, t.display_name || t.api_name),
      h("div", { class: "card-desc" }, t.description || " "),
      h("div", { class: "card-meta" },
        Object.keys(t.properties || {}).length + " props · " + t.backing_dataset),
    ),
  )));

  if (selected) {
    const type = types.find((t) => t.api_name === selected);
    if (!type) {
      wrap.append(errorBox({ status: 404, detail: "Unknown object type: " + selected }));
      return wrap;
    }
    const host = h("div", {});
    wrap.append(h("h2", {}, type.display_name || type.api_name), host);
    renderObjectBrowser(host, type);
  }
  return wrap;
}

const OBJ_PAGE = 25;

function renderObjectBrowser(host, type) {
  const state = { search: "", offset: 0, pk: null };
  const tableHost = h("div", {});
  const detailHost = h("div", {});
  let searchTimer = null;
  const searchInput = h("input", {
    type: "search", placeholder: "Search " + (type.display_name || type.api_name) + "…",
    onInput: (e) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.search = e.target.value.trim();
        state.offset = 0;
        loadObjects();
      }, 250);
    },
  });
  host.append(
    h("div", { class: "toolbar" }, searchInput),
    h("div", { class: "split" }, tableHost, detailHost),
  );

  const props = Object.keys(type.properties || {});
  const shown = props.slice(0, 6);

  let loadSeq = 0;

  async function loadObjects() {
    const seq = ++loadSeq;
    tableHost.replaceChildren(loading());
    let data;
    try {
      const q = new URLSearchParams({ limit: OBJ_PAGE, offset: state.offset });
      if (state.search) q.set("search", state.search);
      data = await api(API + "/ontology/objects/" + encodeURIComponent(type.api_name) + "?" + q);
    } catch (err) {
      if (seq === loadSeq) tableHost.replaceChildren(errorBox(err));
      return;
    }
    if (seq !== loadSeq) return; // a newer search superseded this response
    const objects = data.objects || [];
    const total = data.total != null ? data.total : objects.length;
    if (!objects.length) {
      tableHost.replaceChildren(emptyState(state.search ? "No matches for “" + state.search + "”." : "No objects."));
      return;
    }
    const table = dataTable(
      shown.map((p) => ({ label: p, render: (o) => h("td", { class: "mono" }, fmtValue(o[p])) })),
      objects,
      {
        onRowClick: (o) => { state.pk = o.__pk; loadObjects(); loadDetail(); },
        isSelected: (o) => String(o.__pk) === String(state.pk),
      },
    );
    const last = Math.min(state.offset + objects.length, total);
    const pager = h("div", { class: "pager" },
      h("button", { class: "small", disabled: state.offset === 0,
        onClick: () => { state.offset = Math.max(0, state.offset - OBJ_PAGE); loadObjects(); } }, "‹ Prev"),
      h("button", { class: "small", disabled: last >= total,
        onClick: () => { state.offset += OBJ_PAGE; loadObjects(); } }, "Next ›"),
      h("span", { class: "range" }, `${state.offset + 1}–${last} of ${fmtNum(total)}`),
    );
    tableHost.replaceChildren(table, pager);
  }

  async function loadDetail() {
    if (state.pk == null) { detailHost.replaceChildren(); return; }
    detailHost.replaceChildren(loading());
    try {
      const [obj, typeDetail] = await Promise.all([
        api(API + "/ontology/objects/" + encodeURIComponent(type.api_name) + "/" + encodeURIComponent(state.pk)),
        api(API + "/ontology/object-types/" + encodeURIComponent(type.api_name)),
      ]);
      detailHost.replaceChildren(objectDetailPanel(type, obj, typeDetail, () => { loadObjects(); loadDetail(); }));
    } catch (err) {
      detailHost.replaceChildren(errorBox(err));
    }
  }

  loadObjects();
}

function objectDetailPanel(type, obj, typeDetail, refresh) {
  const panel = h("div", { class: "panel" },
    h("h3", {}, obj.__title || obj.__pk),
    h("div", { class: "faint mono", style: "margin-bottom:10px" }, type.api_name + " · pk " + obj.__pk),
  );

  const dl = h("dl", { class: "kv" });
  for (const [key, val] of Object.entries(obj)) {
    if (key.startsWith("__")) continue;
    dl.append(h("dt", {}, key), h("dd", {}, fmtValue(val) || "—"));
  }
  panel.append(dl);

  const links = typeDetail.links || typeDetail.link_types || [];
  if (links.length) {
    panel.append(h("h2", {}, "Linked objects"));
    for (const link of links) {
      const holder = h("div", { style: "margin-bottom:10px" },
        h("div", { class: "dim", style: "margin-bottom:4px" },
          (link.display_name || link.api_name) + " ",
          h("span", { class: "faint mono" }, "(" + link.api_name + ")")),
      );
      panel.append(holder);
      api(API + "/ontology/objects/" + encodeURIComponent(type.api_name) + "/" +
          encodeURIComponent(obj.__pk) + "/links/" + encodeURIComponent(link.api_name))
        .then((res) => {
          const objs = res.objects || [];
          if (!objs.length) { holder.append(h("div", { class: "faint" }, "none")); return; }
          holder.append(dataTable([
            { label: "Object", render: (o) => h("td", { class: "mono" }, o.__title || o.__pk) },
            { label: "pk", render: (o) => h("td", { class: "mono dim" }, o.__pk) },
          ], objs.slice(0, 15)));
          if (objs.length > 15) holder.append(h("div", { class: "faint" }, "… and " + (objs.length - 15) + " more"));
        })
        .catch((err) => holder.append(h("div", { class: "msg-err" }, "link error: " + err.detail)));
    }
  }

  const actions = (typeDetail.actions || []).filter((a) => a.object_type === type.api_name);
  if (actions.length) {
    panel.append(h("h2", {}, "Actions"));
    for (const action of actions) panel.append(actionForm(action, obj, refresh));
  }
  return panel;
}

function actionForm(action, obj, refresh) {
  const fields = [];
  const form = h("form", { class: "action-form" },
    h("div", { class: "af-title" },
      (action.display_name || action.api_name) + " ",
      h("span", { class: "badge gold" }, action.kind)),
    action.description ? h("div", { class: "af-desc" }, action.description) : null,
  );

  for (const [pname, pdef] of Object.entries(action.parameters || {})) {
    let input;
    if (pdef.type === "boolean") {
      input = h("input", { type: "checkbox" });
    } else if (pdef.type === "integer") {
      input = h("input", { type: "number", step: "1", required: pdef.required });
    } else if (pdef.type === "float") {
      input = h("input", { type: "number", step: "any", required: pdef.required });
    } else {
      input = h("input", { type: "text", required: pdef.required, placeholder: pdef.type });
    }
    fields.push({ name: pname, def: pdef, input });
    form.append(h("div", { class: "field" },
      h("label", {}, pname, pdef.required ? h("span", { class: "req" }, " *") : null,
        pdef.description ? h("span", { class: "faint" }, " — " + pdef.description) : null),
      input,
    ));
  }

  if (!canEdit()) {
    for (const f of fields) f.input.disabled = true;
    form.append(h("div", { class: "ro-note" },
      "Read-only role — applying actions requires the editor role."));
    form.addEventListener("submit", (e) => e.preventDefault());
    return form;
  }

  const msg = h("div", {});
  const submit = h("button", { class: "small primary", type: "submit" }, "Apply");
  form.append(h("div", { class: "toolbar", style: "margin:4px 0 0" }, submit), msg);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const parameters = {};
    for (const f of fields) {
      if (f.def.type === "boolean") {
        parameters[f.name] = f.input.checked;
      } else {
        const raw = f.input.value;
        if (raw === "" && !f.def.required) continue;
        parameters[f.name] = (f.def.type === "integer" || f.def.type === "float") ? Number(raw) : raw;
      }
    }
    const body = { parameters };
    if (action.kind !== "create" && obj) body.pk = String(obj.__pk);
    submit.disabled = true;
    msg.className = "";
    msg.textContent = "";
    try {
      const edit = await api(API + "/ontology/actions/" + encodeURIComponent(action.api_name) + "/apply", {
        method: "POST", body: JSON.stringify(body),
      });
      msg.className = "msg-ok";
      msg.textContent = "Applied — edit " + (edit.id || "").slice(0, 8) + " (" + edit.kind + " " + edit.pk_value + ")";
      if (refresh) refresh();
    } catch (err) {
      msg.className = "msg-err";
      msg.textContent = err.detail || String(err);
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

/* ------------------------------------------------------------- audit view */

async function renderAudit() {
  const events = await api(API + "/audit?limit=200");
  const wrap = h("div", {},
    h("h1", {}, "Audit"),
    h("div", { class: "subtitle" }, "Every mutation, recorded in metadata.db."),
  );
  if (!events.length) {
    wrap.append(emptyState("No audit events yet."));
    return wrap;
  }
  wrap.append(dataTable([
    { label: "Time", render: (e) => h("td", { class: "dim nowrap mono" }, fmtTime(e.timestamp)) },
    { label: "Actor", render: (e) => h("td", { class: "mono" }, e.actor) },
    { label: "Action", render: (e) => h("td", {}, h("span", { class: "badge gold" }, e.action)) },
    {
      label: "Details",
      render: (e) => {
        const json = JSON.stringify(e.details || {});
        return h("td", {}, h("span", { class: "cell-json", title: json }, json));
      },
    },
  ], events));
  return wrap;
}

/* ------------------------------------------------------------- admin view */

const ROLES = ["viewer", "editor", "admin"];

function roleBadge(role) {
  return h("span", { class: "badge role-" + role }, role);
}

async function renderAdmin() {
  if (!isAdmin()) {
    return h("div", {},
      h("h1", {}, "Admin"),
      errorBox({ status: 403, detail: "this view requires the admin role" }),
    );
  }
  const wrap = h("div", {},
    h("h1", {}, "Admin"),
    h("div", { class: "subtitle" }, "Users, roles and API tokens for this workspace."),
  );
  const usersHost = h("div", {});
  const tokensHost = h("div", {});
  wrap.append(h("h2", {}, "Users"), usersHost, h("h2", {}, "API tokens"), tokensHost);
  loadUsersPanel(usersHost);
  loadTokensPanel(tokensHost);
  return wrap;
}

async function loadUsersPanel(host) {
  host.replaceChildren(loading());
  let users;
  try {
    users = await api(API + "/users");
  } catch (err) {
    host.replaceChildren(errorBox(err));
    return;
  }

  const self = auth.user ? auth.user.username : null;
  const msg = h("div", {});
  const refresh = () => loadUsersPanel(host);
  const fail = (err) => msg.replaceChildren(errorBox(err));

  const table = dataTable([
    { label: "Username", render: (u) => h("td", { class: "mono" },
        u.username, u.username === self ? h("span", { class: "faint" }, " (you)") : null) },
    { label: "Role", render: (u) => {
        const sel = h("select", { disabled: u.username === self },
          ROLES.map((r) => {
            const opt = h("option", { value: r }, r);
            if (r === u.role) opt.selected = true;
            return opt;
          }));
        sel.addEventListener("change", async () => {
          msg.replaceChildren();
          try {
            await api(API + "/users/" + encodeURIComponent(u.username), {
              method: "PATCH", body: JSON.stringify({ role: sel.value }),
            });
            refresh();
          } catch (err) {
            sel.value = u.role;
            fail(err);
          }
        });
        return h("td", {}, sel);
      } },
    { label: "Status", render: (u) => h("td", {},
        h("span", { class: "badge " + (u.disabled ? "failed" : "succeeded") },
          u.disabled ? "disabled" : "active")) },
    { label: "Created", render: (u) => h("td", { class: "dim nowrap" }, fmtTime(u.created_at)) },
    { label: "", render: (u) => {
        const isSelf = u.username === self;
        const toggle = h("button", { class: "small", disabled: isSelf }, u.disabled ? "Enable" : "Disable");
        toggle.addEventListener("click", async () => {
          msg.replaceChildren();
          try {
            await api(API + "/users/" + encodeURIComponent(u.username), {
              method: "PATCH", body: JSON.stringify({ disabled: !u.disabled }),
            });
            refresh();
          } catch (err) { fail(err); }
        });
        const del = h("button", { class: "small", disabled: isSelf }, "Delete");
        del.addEventListener("click", async () => {
          if (!window.confirm('Delete user "' + u.username + '"? Their sessions and access are removed. This cannot be undone.')) return;
          msg.replaceChildren();
          try {
            await api(API + "/users/" + encodeURIComponent(u.username), { method: "DELETE" });
            refresh();
          } catch (err) { fail(err); }
        });
        return h("td", { class: "nowrap" }, h("div", { class: "row-actions" }, toggle, del));
      } },
  ], users);

  // Create-user form.
  const cuName = h("input", { type: "text", placeholder: "username", autocomplete: "off", spellcheck: "false" });
  const cuPass = h("input", { type: "password", placeholder: "password (min 8 chars)", autocomplete: "new-password" });
  const cuRole = h("select", {}, ROLES.map((r) => h("option", { value: r }, r)));
  const cuBtn = h("button", { class: "small primary", type: "submit" }, "Create user");
  const cuMsg = h("div", {});
  const cuForm = h("form", { class: "form-row" }, cuName, cuPass, cuRole, cuBtn);
  cuForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    cuMsg.replaceChildren();
    const username = cuName.value.trim();
    if (!USERNAME_RE.test(username)) {
      cuMsg.replaceChildren(h("div", { class: "msg-err" }, "Username must be 2–32 chars: lowercase letters, digits, . _ -"));
      return;
    }
    if (cuPass.value.length < 8) {
      cuMsg.replaceChildren(h("div", { class: "msg-err" }, "Password must be at least 8 characters."));
      return;
    }
    cuBtn.disabled = true;
    try {
      await api(API + "/users", {
        method: "POST",
        body: JSON.stringify({ username, password: cuPass.value, role: cuRole.value }),
      });
      refresh();
    } catch (err) {
      cuBtn.disabled = false;
      cuMsg.replaceChildren(h("div", { class: "msg-err" }, err.detail || String(err)));
    }
  });

  host.replaceChildren(
    table,
    msg,
    h("div", { class: "panel", style: "margin-top:12px" },
      h("h3", {}, "Create user"), cuForm, cuMsg),
  );
}

async function loadTokensPanel(host) {
  host.replaceChildren(loading());
  let tokens;
  try {
    tokens = await api(API + "/tokens");
  } catch (err) {
    host.replaceChildren(errorBox(err));
    return;
  }

  const msg = h("div", {});
  const refresh = () => loadTokensPanel(host);

  const table = tokens.length
    ? dataTable([
        { label: "Name", render: (t) => h("td", {}, t.name) },
        { label: "User", render: (t) => h("td", { class: "mono dim" }, t.username || "—") },
        { label: "Created", render: (t) => h("td", { class: "dim nowrap" }, fmtTime(t.created_at)) },
        { label: "Last used", render: (t) => h("td", { class: "dim nowrap" }, fmtTime(t.last_used_at)) },
        { label: "Id", render: (t) => h("td", { class: "mono faint" }, String(t.id).slice(0, 8)) },
        { label: "", render: (t) => {
            const btn = h("button", { class: "small" }, "Revoke");
            btn.addEventListener("click", async () => {
              if (!window.confirm('Revoke token "' + t.name + '"? Clients using it immediately lose access.')) return;
              msg.replaceChildren();
              try {
                await api(API + "/tokens/" + encodeURIComponent(t.id), { method: "DELETE" });
                refresh();
              } catch (err) { msg.replaceChildren(errorBox(err)); }
            });
            return h("td", {}, btn);
          } },
      ], tokens)
    : emptyState("No API tokens yet. Tokens authenticate scripts and CLI clients via Authorization: Bearer.");

  const ctName = h("input", { type: "text", placeholder: "token name, e.g. ci-deploy", autocomplete: "off", spellcheck: "false" });
  const ctBtn = h("button", { class: "small primary", type: "submit" }, "Create token");
  const ctMsg = h("div", {});
  const ctForm = h("form", { class: "form-row" }, ctName, ctBtn);
  ctForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    ctMsg.replaceChildren();
    const name = ctName.value.trim();
    if (!name) {
      ctMsg.replaceChildren(h("div", { class: "msg-err" }, "Token name is required."));
      return;
    }
    ctBtn.disabled = true;
    try {
      const created = await api(API + "/tokens", { method: "POST", body: JSON.stringify({ name }) });
      showTokenModal(created, refresh);
    } catch (err) {
      ctBtn.disabled = false;
      ctMsg.replaceChildren(h("div", { class: "msg-err" }, err.detail || String(err)));
    }
  });

  host.replaceChildren(
    table,
    msg,
    h("div", { class: "panel", style: "margin-top:12px" },
      h("h3", {}, "Create token"), ctForm, ctMsg),
  );
}

/* Shows a freshly created token exactly once, with a copy button. */
function showTokenModal(created, onClose) {
  const tokenInput = h("input", { type: "text", readonly: true, value: created.token || "" });
  tokenInput.addEventListener("click", () => tokenInput.select());
  const copyBtn = h("button", { class: "small primary" }, "Copy");
  copyBtn.addEventListener("click", async () => {
    let ok = false;
    try {
      await navigator.clipboard.writeText(created.token || "");
      ok = true;
    } catch { /* clipboard API unavailable (e.g. plain http) */ }
    if (!ok) {
      try { tokenInput.select(); ok = document.execCommand("copy"); } catch { /* ignore */ }
    }
    copyBtn.textContent = ok ? "Copied" : "Copy failed — select it manually";
  });
  const close = () => {
    overlay.remove();
    if (onClose) onClose();
  };
  const overlay = h("div", {
    class: "modal-overlay",
    onClick: (e) => { if (e.target === overlay) close(); },
  },
    h("div", { class: "modal" },
      h("h3", {}, "API token created"),
      h("div", { class: "dim", style: "font-size:12px" }, "Token “" + (created.name || "") + "”"),
      h("div", { class: "token-reveal" }, tokenInput, copyBtn),
      h("div", { class: "modal-warn" }, "Copy it now — you won't see this token again."),
      h("div", { class: "toolbar", style: "margin:14px 0 0" },
        h("span", { class: "spacer" }),
        h("button", { onClick: close }, "Done")),
    ),
  );
  document.body.append(overlay);
  tokenInput.select();
}

/* ------------------------------------------------------------------- auth */

const auth = { required: true, setupRequired: false, user: null };
const authScreenEl = document.getElementById("auth-screen");
const USERNAME_RE = /^[a-z0-9_.-]{2,32}$/;

function currentRole() {
  if (auth.required === false) return "admin"; // --no-auth: implicit admin
  return auth.user ? auth.user.role : null;
}
function canEdit() {
  const r = currentRole();
  return r === "editor" || r === "admin";
}
function isAdmin() {
  return currentRole() === "admin";
}

function treeGlyph(size) {
  const svg = svgEl("svg", {
    viewBox: "0 0 24 24", width: size, height: size,
    "aria-hidden": "true", class: "auth-glyph",
  });
  svg.append(
    svgEl("path", { d: "M12 21v-8", stroke: "var(--gold)", "stroke-width": "1.6", fill: "none", "stroke-linecap": "round" }),
    svgEl("path", {
      d: "M12 13 C 6 13 4 8 5 4 C 10 5 12 8 12 13 C 12 8 14 5 19 4 C 20 8 18 13 12 13 Z",
      fill: "var(--gold)", opacity: "0.9",
    }),
    svgEl("circle", { cx: 12, cy: 21, r: 1.2, fill: "var(--gold)" }),
  );
  return svg;
}

function authCard(heading, ...content) {
  return h("div", { class: "auth-card" },
    treeGlyph(36),
    h("div", { class: "auth-title" }, "Laurelin"),
    h("div", { class: "auth-sub" }, "ontology data platform"),
    heading ? h("div", { class: "auth-heading" }, heading) : null,
    content,
  );
}

function authField(label, input, note) {
  return h("div", { class: "auth-field" }, h("label", {}, label), input, note || null);
}

function showAuthScreen(card) {
  document.body.classList.add("auth-mode");
  authScreenEl.hidden = false;
  authScreenEl.replaceChildren(card);
}

function hideAuthScreen() {
  document.body.classList.remove("auth-mode");
  authScreenEl.hidden = true;
  authScreenEl.replaceChildren();
}

function onSessionExpired() {
  auth.user = null;
  showLogin("Your session has expired — please sign in again.");
}

function showLogin(message) {
  const userIn = h("input", {
    type: "text", autocomplete: "username", autocapitalize: "none",
    spellcheck: "false", required: true,
  });
  const passIn = h("input", { type: "password", autocomplete: "current-password", required: true });
  const err = h("div", { class: "auth-error" + (message ? " show" : "") }, message || "");
  const btn = h("button", { class: "primary auth-submit", type: "submit" }, "Sign in");

  // A <form> with a submit button gives us enter-to-submit for free.
  const form = h("form", { class: "auth-form" },
    authField("Username", userIn),
    authField("Password", passIn),
    err, btn,
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.classList.remove("show");
    err.textContent = "";
    btn.disabled = true;
    btn.textContent = "Signing in…";
    try {
      const user = await api(API + "/auth/login", {
        method: "POST",
        body: JSON.stringify({ username: userIn.value.trim(), password: passIn.value }),
      });
      auth.user = user;
      auth.setupRequired = false;
      enterApp();
    } catch (e2) {
      btn.disabled = false;
      btn.textContent = "Sign in";
      if (e2.status === 401) err.textContent = "Invalid username or password.";
      else if (e2.status === 429) err.textContent = "Too many failed attempts — wait 30 seconds and try again.";
      else err.textContent = e2.detail || "Login failed.";
      err.classList.add("show");
      passIn.value = "";
      passIn.focus();
    }
  });

  showAuthScreen(authCard("Sign in to continue", form));
  userIn.focus();
}

function showSetup() {
  const userIn = h("input", {
    type: "text", autocomplete: "username", autocapitalize: "none",
    spellcheck: "false", placeholder: "e.g. admin",
  });
  const passIn = h("input", { type: "password", autocomplete: "new-password", placeholder: "min 8 characters" });
  const confIn = h("input", { type: "password", autocomplete: "new-password", placeholder: "repeat password" });
  const userNote = h("div", { class: "field-note" }, "2–32 chars: lowercase letters, digits, . _ -");
  const passNote = h("div", { class: "field-note" }, "At least 8 characters.");
  const confNote = h("div", { class: "field-note" }, "");
  const err = h("div", { class: "auth-error" });
  const btn = h("button", { class: "primary auth-submit", type: "submit", disabled: true }, "Create admin account");

  const touched = { user: false, pass: false, conf: false };
  function validate() {
    const okUser = USERNAME_RE.test(userIn.value.trim());
    const okPass = passIn.value.length >= 8;
    const okConf = passIn.value !== "" && confIn.value === passIn.value;
    userNote.className = "field-note" + (touched.user ? (okUser ? " ok" : " bad") : "");
    passNote.className = "field-note" + (touched.pass ? (okPass ? " ok" : " bad") : "");
    if (touched.conf) {
      confNote.textContent = okConf ? "Passwords match." : "Passwords do not match.";
      confNote.className = "field-note" + (okConf ? " ok" : " bad");
    } else {
      confNote.textContent = "";
      confNote.className = "field-note";
    }
    btn.disabled = !(okUser && okPass && okConf);
    return okUser && okPass && okConf;
  }
  userIn.addEventListener("input", () => { touched.user = true; validate(); });
  passIn.addEventListener("input", () => {
    touched.pass = true;
    if (confIn.value) touched.conf = true;
    validate();
  });
  confIn.addEventListener("input", () => { touched.conf = true; validate(); });

  const form = h("form", { class: "auth-form" },
    authField("Username", userIn, userNote),
    authField("Password", passIn, passNote),
    authField("Confirm password", confIn, confNote),
    err, btn,
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!validate()) return;
    err.classList.remove("show");
    err.textContent = "";
    btn.disabled = true;
    btn.textContent = "Creating…";
    const username = userIn.value.trim();
    const password = passIn.value;
    try {
      await api(API + "/auth/setup", {
        method: "POST", body: JSON.stringify({ username, password }),
      });
    } catch (e2) {
      if (e2.status === 409) {
        // Someone else created the first account meanwhile.
        showLogin("An account already exists — sign in instead.");
        return;
      }
      btn.disabled = false;
      btn.textContent = "Create admin account";
      err.textContent = e2.detail || "Setup failed.";
      err.classList.add("show");
      return;
    }
    auth.setupRequired = false;
    try {
      const user = await api(API + "/auth/login", {
        method: "POST", body: JSON.stringify({ username, password }),
      });
      auth.user = user;
      enterApp();
    } catch {
      showLogin("Account created — please sign in.");
    }
  });

  showAuthScreen(authCard("First run — create the admin account", form,
    h("div", { class: "auth-foot" },
      "This account gets the admin role; add more users later under Admin.")));
  userIn.focus();
}

function renderUserFoot() {
  const el = document.getElementById("user-info");
  if (auth.required === false) {
    el.replaceChildren(h("div", { class: "user-line" },
      h("span", { class: "badge" }, "auth disabled"),
      h("span", { class: "faint" }, "dev mode"),
    ));
    return;
  }
  if (!auth.user) {
    el.replaceChildren();
    return;
  }
  const logoutBtn = h("button", { class: "small logout-btn" }, "Sign out");
  logoutBtn.addEventListener("click", doLogout);
  el.replaceChildren(
    h("div", { class: "user-line" },
      h("span", { class: "user-name" }, auth.user.username),
      roleBadge(auth.user.role),
    ),
    logoutBtn,
  );
}

async function doLogout() {
  try {
    await api(API + "/auth/logout", { method: "POST" });
  } catch { /* session may already be gone; cookie is cleared server-side */ }
  auth.user = null;
  renderUserFoot();
  showLogin("Signed out.");
}

function enterApp() {
  hideAuthScreen();
  document.querySelector('#nav a[data-view="admin"]').hidden = !isAdmin();
  renderUserFoot();
  loadWorkspaceInfo();
  render();
}

/* -------------------------------------------------------------- bootstrap */

async function boot() {
  // The old bearer-token mechanism is gone; drop any stale stored token.
  try { localStorage.removeItem("laurelin_token"); } catch { /* private mode */ }

  let status;
  try {
    status = await api(API + "/auth/status");
  } catch (err) {
    showAuthScreen(authCard(null,
      h("div", { class: "auth-error show" },
        (err && err.detail) || "Cannot reach the Laurelin server."),
      h("div", { class: "auth-foot" }, "Start it with “laurelin serve”, then reload."),
    ));
    return;
  }
  auth.required = status.auth_required !== false;
  auth.setupRequired = !!status.setup_required;
  auth.user = status.user || null;

  if (auth.required && auth.setupRequired) showSetup();
  else if (auth.required && !auth.user) showLogin();
  else enterApp();
}

boot();
