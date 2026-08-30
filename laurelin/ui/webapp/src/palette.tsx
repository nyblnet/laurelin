// Hand-rolled command palette: Ctrl/Cmd+K, navigation only. Zero dependencies
// (the single-file bundle rule is absolute), and deliberately nav-only for
// now: searching datasets/artifacts needs debounced per-caller fetches and
// belongs to a later milestone.
//
// Governance by construction: the item list is `visibleNavGroups(auth)` — the
// exact role-filtered structure the sidebar renders — so the palette can never
// offer a door the sidebar hides. Do not build a second list here.

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { visibleNavGroups } from "./Layout";
import type { Role } from "./types";

export interface PaletteItem {
  to: string;
  label: string;
  group: string;
}

/** The flat, role-filtered destination list. Exported so tests can pin that
 *  the palette offers exactly what the sidebar shows. */
export function paletteItems(auth: {
  can: (role: Role) => boolean;
  isSuperadmin: boolean;
  multi: boolean;
}): PaletteItem[] {
  return visibleNavGroups(auth).flatMap((g) =>
    g.items.map((n) => ({ to: n.to, label: n.label, group: g.label })),
  );
}

export function CommandPalette() {
  const auth = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Focus returns to wherever the user was when the palette closes.
  const restoreRef = useRef<HTMLElement | null>(null);

  const items = useMemo(() => paletteItems(auth), [auth]);
  const q = query.trim().toLowerCase();
  const hits = q
    ? items.filter(
        (n) => n.label.toLowerCase().includes(q) || n.group.toLowerCase().includes(q),
      )
    : items;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) {
      restoreRef.current = document.activeElement as HTMLElement | null;
      setQuery("");
      setCursor(0);
      inputRef.current?.focus();
    } else if (restoreRef.current) {
      restoreRef.current.focus?.();
      restoreRef.current = null;
    }
  }, [open]);

  function go(to: string) {
    setOpen(false);
    navigate(to);
  }

  if (!open) return null;

  return (
    <div className="palette-backdrop" onClick={() => setOpen(false)}>
      <div
        className="palette"
        role="dialog"
        aria-label="Go to page"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          className="palette-input"
          placeholder="Go to…"
          aria-label="Filter pages"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setCursor(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setCursor((c) => Math.min(c + 1, hits.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setCursor((c) => Math.max(c - 1, 0));
            } else if (e.key === "Enter" && hits[cursor]) {
              e.preventDefault();
              go(hits[cursor].to);
            }
          }}
        />
        <ul className="palette-list">
          {hits.length === 0 ? (
            <li className="palette-none faint">No matching page.</li>
          ) : (
            hits.map((n, i) => (
              <li key={n.to}>
                <button
                  type="button"
                  className={`palette-item${i === cursor ? " selected" : ""}`}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => go(n.to)}
                >
                  <span className="palette-group">{n.group}</span>
                  <span className="palette-label">{n.label}</span>
                </button>
              </li>
            ))
          )}
        </ul>
        <div className="palette-hint faint">↑↓ to choose · Enter to go · Esc to close</div>
      </div>
    </div>
  );
}
