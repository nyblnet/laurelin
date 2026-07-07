# Laurelin web app

The Laurelin UI: React + TypeScript, built with Vite into a single
self-contained `../static/index.html` (all JS/CSS inlined — no CDN, no external
requests) that the API server mounts at `/`. The committed bundle means
`pip install laurelin` ships a working UI with **no Node toolchain required**;
Node is only needed to modify the UI.

## Develop

```bash
cd laurelin/ui/webapp
npm install
# Terminal 1: run the API against a workspace
laurelin serve --workspace ../../../demo-workspace --no-auth --port 8787
# Terminal 2: Vite dev server (proxies /api and /health to :8787)
npm run dev
```

## Build (regenerate the shipped bundle)

```bash
npm run build      # tsc -b && vite build -> ../static/index.html
```

Commit the regenerated `laurelin/ui/static/index.html` along with your source
changes.

## Layout

```
src/
├── main.tsx / App.tsx      app entry, auth gate, router, QueryClient
├── auth.tsx                auth context (status, login, setup, logout, role)
├── api.ts / types.ts       typed fetch client + API types
├── ui.tsx / styles.css     shared primitives + design system
├── Layout.tsx / brand.tsx  sidebar shell + wordmark
├── screens/                login + first-run setup
└── views/                  Datasets, Pipeline, Ontology, Workbench, Audit, Admin
```
