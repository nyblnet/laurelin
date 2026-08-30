// Pipelines — the one door for "data → new dataset", with two idioms as tabs:
//
//   Visual — the no-code step builder (views/Flows.tsx). The front door.
//   Python — the code editor over pipelines/*.py (views/Transforms.tsx).
//
// The two halves keep their own files and behavior; this file is only the
// seam that makes them one nav item. Both are editor-gated as a whole: a
// visual pipeline authors a transform that runs as the system, and reading
// exec'd Python is editor-gated (the Transforms rationale from R2), so the
// merged item inherits the stricter of its parts — which is both of them.
//
// Convert-to-Python ("eject", one-way, warned before the click) lands on the
// Python tab with the generated file open: /pipelines?tab=python&file=<name>.

import { Link, useLocation, useSearchParams } from "react-router-dom";
import { FlowsView } from "./Flows";
import { TransformsView } from "./Transforms";

export function PipelinesView() {
  const location = useLocation();
  const [params] = useSearchParams();
  // The tab switch only exists at the index; /pipelines/:name is the visual
  // builder's detail page and renders without tab chrome.
  const atIndex = /^\/pipelines\/?$/.test(location.pathname);
  if (atIndex && params.get("tab") === "python") return <TransformsView />;
  return <FlowsView />;
}

/** Rendered by both tab index screens, under their shared "Pipelines" header. */
export function PipelinesTabs({ active }: { active: "visual" | "python" }) {
  return (
    <div className="pl-tabs" role="tablist" aria-label="Pipeline authoring surface">
      <Link
        role="tab"
        aria-selected={active === "visual"}
        className={`pl-tab${active === "visual" ? " active" : ""}`}
        to="/pipelines"
      >
        Visual
      </Link>
      <Link
        role="tab"
        aria-selected={active === "python"}
        className={`pl-tab${active === "python" ? " active" : ""}`}
        to="/pipelines?tab=python"
      >
        Python
      </Link>
    </div>
  );
}
