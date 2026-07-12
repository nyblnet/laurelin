// Full-screen login and first-run setup, shown before the app mounts.

import { useState, type FormEvent } from "react";
import { ApiError } from "../api";
import { useAuth } from "../auth";
import { TreeGlyph } from "../brand";

const USERNAME_RE = /^[a-z0-9_.-]{2,32}$/;

function AuthShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="brand">
          <TreeGlyph size={30} />
          <span className="brand-name">Laurelin</span>
        </div>
        <div className="brand-tag">Ontology Data Platform</div>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function LoginScreen() {
  const auth = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await auth.login(username, password);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 429
            ? "Too many failed attempts — wait a moment and try again."
            : "Invalid username or password.",
        );
      } else {
        setError("Something went wrong. Try again.");
      }
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Sign in to continue">
      <form onSubmit={submit}>
        <div className="field">
          <label>Username</label>
          <input
            autoFocus
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error && <div className="error-box">{error}</div>}
        <button type="submit" className="primary" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {auth.oidc?.enabled && (
        <>
          <div className="auth-divider">or</div>
          <a className="btn sso-btn" href="/api/v1/auth/oidc/login">
            Sign in with {auth.oidc.provider_name || "SSO"}
          </a>
        </>
      )}
    </AuthShell>
  );
}

export function SetupScreen() {
  const auth = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const userValid = USERNAME_RE.test(username);
  const pwValid = password.length >= 8;
  const match = password === confirm;
  const ready = userValid && pwValid && match;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setError(null);
    setBusy(true);
    try {
      await auth.setup(username, password);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Someone else completed setup first — fall back to login.
        await auth.refresh();
      } else {
        setError(err instanceof ApiError ? err.detail : "Setup failed. Try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell
      title={
        auth.multi
          ? "First run — create the server administrator"
          : "First run — create the admin account"
      }
    >
      <form onSubmit={submit}>
        <div className="field">
          <label>Username</label>
          <input
            autoFocus
            autoComplete="username"
            placeholder="e.g. admin"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <div className={`hint ${username && !userValid ? "bad" : ""}`}>
            2–32 chars: lowercase letters, digits, . _ -
          </div>
        </div>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            autoComplete="new-password"
            placeholder="min 8 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <div className={`hint ${password && !pwValid ? "bad" : ""}`}>
            At least 8 characters.
          </div>
        </div>
        <div className="field">
          <label>Confirm password</label>
          <input
            type="password"
            autoComplete="new-password"
            placeholder="repeat password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          {confirm && !match && <div className="hint bad">Passwords don't match.</div>}
        </div>
        {error && <div className="error-box">{error}</div>}
        <button type="submit" className="primary" disabled={busy || !ready}>
          {busy ? "Creating…" : auth.multi ? "Create server administrator" : "Create admin account"}
        </button>
      </form>
      <div className="auth-note">
        {auth.multi
          ? "This account manages workspaces and users, and is admin in every workspace."
          : "This account gets the admin role; add more users later under Admin."}
      </div>
    </AuthShell>
  );
}
