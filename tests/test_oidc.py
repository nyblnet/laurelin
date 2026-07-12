"""Tests for OIDC SSO. A fake provider mints a real RS256 id_token so the actual
signature / nonce / claim validation path runs — only the network calls (jwks,
token exchange, discovery) are stubbed."""

import time

import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.core.config import Workspace
from laurelin.core.oidc import OIDCConfig, OIDCProvider

ISSUER = "https://idp.example.com"
CLIENT_ID = "laurelin-client"

OIDC_ENV = {
    "LAURELIN_OIDC_ISSUER": ISSUER,
    "LAURELIN_OIDC_CLIENT_ID": CLIENT_ID,
    "LAURELIN_OIDC_CLIENT_SECRET": "s3cret",
    "LAURELIN_OIDC_ROLE_MAP": "laurelin-admins:admin,laurelin-editors:editor",
    "LAURELIN_OIDC_GROUPS_CLAIM": "groups",
    "LAURELIN_OIDC_PROVIDER_NAME": "TestIdP",
}


class FakeProvider(OIDCProvider):
    """OIDCProvider with network stubbed; records the nonce it was asked to embed
    and returns a caller-supplied id_token from the token exchange."""

    def __init__(self, config, key):
        super().__init__(config)
        self._key = key  # private JWK
        self.last_nonce = None
        self.id_token = None

    def discover(self):
        return {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        }

    def jwks(self):
        return JsonWebKey.import_key_set({"keys": [self._key.as_dict(is_private=False)]})

    def authorization_url(self, redirect_uri, state, nonce, code_challenge):
        self.last_nonce = nonce
        return super().authorization_url(redirect_uri, state, nonce, code_challenge)

    def exchange_code(self, code, redirect_uri, code_verifier):
        return {"id_token": self.id_token}

    def mint(self, *, groups, username="alice", nonce=None, **extra):
        header = {"alg": "RS256", "kid": self._key.thumbprint()}
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "abc123",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "nonce": nonce if nonce is not None else self.last_nonce,
            "preferred_username": username,
            "groups": groups,
            **extra,
        }
        return jwt.encode(header, claims, self._key).decode()


@pytest.fixture()
def key():
    return JsonWebKey.generate_key("RSA", 2048, is_private=True)


@pytest.fixture()
def app(tmp_path, monkeypatch, key):
    for k, v in OIDC_ENV.items():
        monkeypatch.setenv(k, v)
    ws = Workspace.init(tmp_path / "ws", name="oidc")
    app = create_app(ws)
    app.state.oidc_provider = FakeProvider(app.state.oidc_config, key)
    # An admin must exist so the server is out of setup mode.
    app.state.auth.create_first_admin("root", "trustno1!")
    return app


def _login_via_sso(app, groups, username="alice"):
    provider: FakeProvider = app.state.oidc_provider
    client = TestClient(app)
    # 1) kick off login -> 302 to the IdP; capture state + nonce
    r = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"{ISSUER}/authorize")
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(loc).query)["state"][0]
    # 2) IdP redirects back with a code; the token exchange returns our id_token
    provider.id_token = provider.mint(groups=groups, username=username)
    cb = client.get(
        f"/api/v1/auth/oidc/callback?code=abc&state={state}", follow_redirects=False
    )
    return client, cb


def test_status_reports_oidc_enabled(app):
    s = TestClient(app).get("/api/v1/auth/status").json()
    assert s["oidc"] == {"enabled": True, "provider_name": "TestIdP"}


def test_disabled_when_unconfigured(tmp_path):
    ws = Workspace.init(tmp_path / "ws2", name="noidc")
    c = TestClient(create_app(ws, no_auth=True))
    assert c.get("/api/v1/auth/status").json()["oidc"] == {"enabled": False}
    assert c.get("/api/v1/auth/oidc/login", follow_redirects=False).status_code == 404


def test_sso_login_provisions_user_and_maps_role(app):
    client, cb = _login_via_sso(app, groups=["laurelin-editors"])
    assert cb.status_code == 302 and cb.headers["location"] == "/"
    assert "laurelin_session" in cb.headers.get("set-cookie", "")
    # The session now authenticates as the JIT-provisioned editor.
    me = client.get("/api/v1/auth/me").json()
    assert me["username"] == "alice"
    assert me["role"] == "editor"


def test_role_map_picks_highest_group(app):
    client, cb = _login_via_sso(app, groups=["laurelin-editors", "laurelin-admins"])
    assert client.get("/api/v1/auth/me").json()["role"] == "admin"


def test_unmapped_groups_get_default_viewer(app):
    client, cb = _login_via_sso(app, groups=["random-team"], username="bob")
    assert client.get("/api/v1/auth/me").json()["role"] == "viewer"


def test_invalid_state_is_rejected(app):
    client = TestClient(app)
    r = client.get("/api/v1/auth/oidc/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 400


def test_nonce_mismatch_is_rejected(app):
    provider: FakeProvider = app.state.oidc_provider
    client = TestClient(app)
    r = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    # Mint with a WRONG nonce -> validation must fail.
    provider.id_token = provider.mint(groups=[], nonce="not-the-real-nonce")
    cb = client.get(f"/api/v1/auth/oidc/callback?code=x&state={state}", follow_redirects=False)
    assert cb.status_code == 400


def test_state_is_single_use(app):
    client, cb = _login_via_sso(app, groups=[])
    assert cb.status_code == 302
    # Replaying the same state (now consumed) must fail.
    from urllib.parse import parse_qs, urlsplit

    # do a fresh login to get a state, consume it, then replay
    r = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    provider: FakeProvider = app.state.oidc_provider
    provider.id_token = provider.mint(groups=[])
    first = client.get(f"/api/v1/auth/oidc/callback?code=x&state={state}", follow_redirects=False)
    assert first.status_code == 302
    replay = client.get(f"/api/v1/auth/oidc/callback?code=x&state={state}", follow_redirects=False)
    assert replay.status_code == 400
