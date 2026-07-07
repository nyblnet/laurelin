// Auth context: loads /auth/status, exposes the current user + role, and the
// login / setup / logout operations. The app is gated on this — see App.tsx.

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
import type { AuthStatus, Role, User } from "./types";

interface AuthContextValue {
  loading: boolean;
  authRequired: boolean;
  setupRequired: boolean;
  user: User | null;
  role: Role;
  /** viewer < editor < admin */
  can: (role: Role) => boolean;
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  setup: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Called by the query layer on a 401 to force re-auth. */
  onUnauthorized: () => void;
}

const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<AuthStatus>({
    auth_required: true,
    setup_required: false,
    user: null,
  });

  const refresh = useCallback(async () => {
    const s = await api.get<AuthStatus>(`${API}/auth/status`);
    setStatus(s);
  }, []);

  useEffect(() => {
    refresh()
      .catch(() => setStatus({ auth_required: true, setup_required: false, user: null }))
      .finally(() => setLoading(false));
  }, [refresh]);

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

  const onUnauthorized = useCallback(() => {
    setStatus((s) => (s.user ? { ...s, user: null } : s));
  }, []);

  // In no-auth (dev) mode the server treats everyone as admin.
  const role: Role = status.auth_required ? (status.user?.role ?? "viewer") : "admin";

  const value = useMemo<AuthContextValue>(
    () => ({
      loading,
      authRequired: status.auth_required,
      setupRequired: status.setup_required,
      user: status.user,
      role,
      can: (needed: Role) => RANK[role] >= RANK[needed],
      refresh,
      login,
      setup,
      logout,
      onUnauthorized,
    }),
    [loading, status, role, refresh, login, setup, logout, onUnauthorized],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
