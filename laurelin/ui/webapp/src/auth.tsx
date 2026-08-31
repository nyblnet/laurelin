// Auth context: loads /auth/status, exposes the current user + role, and the
// login / setup / logout operations. The app is gated on this — see App.tsx.
//
// In multi-workspace mode it also tracks the active workspace (persisted in the
// `laurelin_workspace` cookie, which the backend reads to scope every request)
// and derives the user's effective role from their membership in that workspace.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { API, ApiError, api } from "./api";
import type { AuthStatus, OidcStatus, Role, User, UserWorkspace } from "./types";

interface AuthContextValue {
  loading: boolean;
  authRequired: boolean;
  setupRequired: boolean;
  user: User | null;
  role: Role;
  /** viewer < editor < admin */
  can: (role: Role) => boolean;
  // Server lock posture (from /auth/status): which authoring surfaces an
  // operator disabled. Distinct from `can(...)` — a lock is server-wide and
  // no role or administrator can save through it without a restart.
  pipelinesLocked: boolean;
  flowsLocked: boolean;
  // Multi-workspace:
  multi: boolean;
  isSuperadmin: boolean;
  workspaces: UserWorkspace[];
  activeSlug: string | null;
  setActiveWorkspace: (slug: string) => void;
  // SSO:
  oidc: OidcStatus | undefined;
  saml: OidcStatus | undefined;
  /** Re-probe /auth/status and sync workspace state without a reload. In
   *  multi mode this re-reads the membership list and, when no valid active
   *  workspace cookie exists, activates the first workspace — so a
   *  superadmin who just created their first workspace lands in it instead
   *  of staying stranded on a stale "no workspace" state. Workspaces calls
   *  this after create/join; `refreshAuth` is the same function under the
   *  name the UX spec assigns it. */
  refresh: () => Promise<void>;
  refreshAuth: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  setup: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Called by the query layer on a 401 to force re-auth. */
  onUnauthorized: () => void;
  /** HTTP status of a FAILED bootstrap /auth/status call, or null when it did
   *  not fail. 401 never lands here — that one is a signed-out user and is
   *  handled as such. 0 means the request never reached the server. */
  bootstrapFailure: number | null;
  /** Re-run the bootstrap probe. The only control offered on the unreachable
   *  screen, because it is the only one that can help. */
  retryBootstrap: () => void;
}

const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };
const WS_COOKIE = "laurelin_workspace";

function readWsCookie(): string | null {
  const m = document.cookie.match(/(?:^|;\s*)laurelin_workspace=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function writeWsCookie(slug: string): void {
  document.cookie = `${WS_COOKIE}=${encodeURIComponent(slug)}; path=/; SameSite=Lax; Max-Age=31536000`;
}

// Exported for the node mount harness (tests/webapp_harness), which renders
// real views with a synthetic signed-in identity because AuthProvider only
// reaches a usable state through effects, and server rendering runs none.
// App code goes through <AuthProvider> and useAuth(), never this directly.
export const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<AuthStatus>({
    auth_required: true,
    setup_required: false,
    user: null,
  });
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  // The bootstrap call failed for a reason that is NOT "you are signed out".
  // `null` means it did not fail. See `bootstrapFailure` in the context.
  const [unreachable, setUnreachable] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    const s = await api.get<AuthStatus>(`${API}/auth/status`);
    // In multi mode pick/sync the active workspace BEFORE the app renders any
    // workspace-scoped queries, so the cookie is in place when they fire.
    if (s.multi && s.user) {
      const spaces = s.user.workspaces ?? [];
      let slug = readWsCookie();
      if (!slug || !spaces.some((w) => w.slug === slug)) {
        slug = spaces[0]?.slug ?? null;
      }
      if (slug) writeWsCookie(slug);
      setActiveSlug(slug);
    } else {
      setActiveSlug(null);
    }
    setStatus(s);
  }, []);

  // A failed /auth/status is not evidence of a signed-out user.
  //
  // The old line was `.catch(() => setStatus({auth_required: true, ...}))`,
  // which turned EVERY bootstrap failure into the login screen. Measured on a
  // `--no-auth` server with the call blocked at the transport layer: the app
  // rendered "Sign in to continue / USERNAME / PASSWORD / Sign in" — a
  // credential form that can never succeed, on a server with no accounts, with
  // no Retry and no other control on the page.
  //
  // `api.ts` already preserves what actually happened (`ApiError(0, "Network
  // error: ...")` for transport, the real status for 5xx); the catch threw it
  // away. 401 is the ONLY status that means signed out — everything else,
  // including 0, is a server the client could not reach.
  //
  // Deliberately ONE message for every non-401 case, with no special wording
  // for the no-auth server: when the call fails the client does not know the
  // server's auth mode — that is precisely the fact it is missing — so
  // claiming "this server runs with auth disabled" would be a guess.
  const bootstrap = useCallback(() => {
    setLoading(true);
    setUnreachable(null);
    refresh()
      .then(() => setUnreachable(null))
      .catch((e: unknown) => {
        const status = e instanceof ApiError ? e.status : 0;
        if (status === 401) {
          setStatus({ auth_required: true, setup_required: false, user: null });
        } else {
          setUnreachable(status);
        }
      })
      .finally(() => setLoading(false));
  }, [refresh]);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  const login = useCallback(
    async (username: string, password: string) => {
      await api.post(`${API}/auth/login`, { username, password });
      await refresh();
    },
    [refresh],
  );

  const setup = useCallback(
    async (username: string, password: string) => {
      await api.post(`${API}/auth/setup`, { username, password });
      await api.post(`${API}/auth/login`, { username, password });
      await refresh();
    },
    [refresh],
  );

  const logout = useCallback(async () => {
    try {
      await api.post(`${API}/auth/logout`);
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
    }
    setStatus((s) => ({ ...s, user: null }));
  }, []);

  const setActiveWorkspace = useCallback((slug: string) => {
    writeWsCookie(slug);
    // Reload so every cached query refetches against the newly selected
    // workspace — simplest correct way to swap the whole data context.
    window.location.reload();
  }, []);

  const onUnauthorized = useCallback(() => {
    setStatus((s) => (s.user ? { ...s, user: null } : s));
  }, []);

  const multi = !!status.multi;
  const isSuperadmin = !!status.user?.superadmin;
  const workspaces = status.user?.workspaces ?? [];
  const pipelinesLocked = !!status.authoring?.pipelines_locked;
  const flowsLocked = !!status.authoring?.flows_locked;

  // Effective role. No-auth dev mode → admin. Single mode → account role.
  // Multi mode → superadmin is admin, else the membership role in the active
  // workspace.
  let role: Role = "viewer";
  if (!status.auth_required) {
    role = "admin";
  } else if (!multi) {
    role = status.user?.role ?? "viewer";
  } else if (isSuperadmin) {
    role = "admin";
  } else {
    role = workspaces.find((w) => w.slug === activeSlug)?.role ?? "viewer";
  }

  const value = useMemo<AuthContextValue>(
    () => ({
      loading,
      authRequired: status.auth_required,
      setupRequired: status.setup_required,
      user: status.user,
      role,
      can: (needed: Role) => RANK[role] >= RANK[needed],
      multi,
      pipelinesLocked,
      flowsLocked,
      isSuperadmin,
      workspaces,
      activeSlug,
      setActiveWorkspace,
      oidc: status.oidc,
      saml: status.saml,
      bootstrapFailure: unreachable,
      retryBootstrap: bootstrap,
      refresh,
      refreshAuth: refresh,
      login,
      setup,
      logout,
      onUnauthorized,
    }),
    [loading, status, role, multi, isSuperadmin, pipelinesLocked, flowsLocked, workspaces, activeSlug, setActiveWorkspace, refresh, login, setup, logout, onUnauthorized, unreachable, bootstrap],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
