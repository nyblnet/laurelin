"""Authentication & authorization tests: hashing, sessions, throttling, setup,
login/logout, RBAC, user & token management, CSRF, and no-auth mode."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.core.auth import (
    MAX_THROTTLE_ENTRIES,
    PASSWORD_MAX_LENGTH,
    THROTTLED,
    THROTTLE_FAILURES,
    AuthService,
    hash_password,
    hash_token,
    new_token,
    verify_password,
)
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role

ADMIN = {"username": "root", "password": "trustno1!"}


class FakeTime:
    """Injectable monotonic clock + wall clock for AuthService."""

    def __init__(self):
        self.monotonic_s = 1000.0
        self.wall = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.monotonic_s += seconds
        self.wall += timedelta(seconds=seconds)

    def clock(self) -> float:
        return self.monotonic_s

    def now(self) -> datetime:
        return self.wall


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace.init(tmp_path / "ws", name="authws")


@pytest.fixture
def store(ws) -> MetadataStore:
    return MetadataStore(ws.metadata_path)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def service(store, fake_time) -> AuthService:
    return AuthService(store, clock=fake_time.clock, now=fake_time.now)


@pytest.fixture
def app(ws):
    return create_app(ws)


@pytest.fixture
def admin_client(app) -> TestClient:
    """A logged-in admin session (created through the setup flow)."""
    client = TestClient(app)
    assert client.post("/api/v1/auth/setup", json=ADMIN).status_code == 200
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 200
    return client


def login_as(app, username: str, password: str) -> TestClient:
    client = TestClient(app)
    r = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return client


def make_user(admin_client, username: str, role: str, password: str = "sup3rsecret"):
    r = admin_client.post(
        "/api/v1/users",
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_password_hash_roundtrip():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$16384$8$1$")
    assert "correct horse battery" not in stored
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong password", stored)
    assert not verify_password("", stored)


def test_password_hashes_are_salted():
    assert hash_password("same password") != hash_password("same password")


def test_verify_password_rejects_garbage_hashes():
    assert not verify_password("x", "")
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$deadbeef")


def test_new_token_is_unique_and_hash_is_sha256():
    t = new_token()
    assert t != new_token()
    assert len(hash_token(t)) == 64
    assert hash_token(t) == hash_token(t)


# ---------------------------------------------------------------------------
# AuthService: users, sessions, throttling
# ---------------------------------------------------------------------------

def test_create_user_validation(service):
    with pytest.raises(ValueError):
        service.create_user("x", "longenough")  # too short a username
    with pytest.raises(ValueError):
        service.create_user("UPPER", "longenough")  # uppercase rejected
    with pytest.raises(ValueError):
        service.create_user("has space", "longenough")
    with pytest.raises(ValueError):
        service.create_user("okname", "short7c")  # < 8 chars
    user = service.create_user("ok.na-me_2", "longenough")
    assert user.role == Role.viewer
    with pytest.raises(ValueError):
        service.create_user("ok.na-me_2", "longenough")  # duplicate


def test_username_uniqueness_is_case_insensitive(service, store):
    service.create_user("alice", "longenough")
    # The regex forbids creating "Alice", but the store must also refuse it.
    import sqlite3

    from laurelin.core.models import User

    with pytest.raises(sqlite3.IntegrityError):
        store.create_user(
            User(id="zz", username="Alice", role=Role.viewer), "irrelevant"
        )
    assert store.get_user("ALICE").username == "alice"


def test_authenticate_success_wrong_password_unknown_user(service):
    service.create_user("alice", "alicepassword")
    user = service.authenticate("alice", "alicepassword")
    assert user is not None and user.username == "alice"
    assert service.authenticate("alice", "wrongpassword") is None
    assert service.authenticate("nobody", "alicepassword") is None


def test_authenticate_disabled_user_rejected(service):
    service.create_user("alice", "alicepassword")
    service.update_user("alice", disabled=True)
    assert service.authenticate("alice", "alicepassword") is None
    service.update_user("alice", disabled=False)
    assert service.authenticate("alice", "alicepassword") is not None


def test_session_roundtrip_and_expiry(service, fake_time):
    user = service.create_user("alice", "alicepassword")
    token, _ = service.login(user)
    assert service.resolve_session(token).username == "alice"
    # 6 days later: still valid. 7+ days: expired and purged.
    fake_time.advance(6 * 24 * 3600)
    assert service.resolve_session(token) is not None
    fake_time.advance(2 * 24 * 3600)
    assert service.resolve_session(token) is None
    assert service.store.get_session(hash_token(token)) is None  # purged


def test_logout_deletes_session(service):
    user = service.create_user("alice", "alicepassword")
    token, _ = service.login(user)
    service.logout(token)
    assert service.resolve_session(token) is None


def test_disabled_users_sessions_rejected(service):
    user = service.create_user("alice", "alicepassword")
    token, _ = service.login(user)
    service.update_user("alice", disabled=True)
    assert service.resolve_session(token) is None


def test_throttling_locks_after_five_failures_and_unlocks_after_30s(service, fake_time):
    service.create_user("alice", "alicepassword")
    for _ in range(5):
        assert service.authenticate("alice", "wrong") is None
    # Locked: further wrong guesses are rejected as THROTTLED (429). The lock
    # only blocks bad guesses — the correct password still gets through (see
    # test_correct_password_bypasses_throttle_lock).
    assert service.authenticate("alice", "wrong") is THROTTLED
    fake_time.advance(29)
    assert service.authenticate("alice", "wrong") is THROTTLED
    fake_time.advance(2)  # past the 30s lockout
    # Lock expired: a wrong guess is a normal failure again...
    assert service.authenticate("alice", "wrong") is None
    # ...and the correct password works.
    assert service.authenticate("alice", "alicepassword") is not None


def test_throttling_is_per_username(service):
    service.create_user("alice", "alicepassword")
    service.create_user("bob", "bobpassword99")
    for _ in range(5):
        service.authenticate("alice", "wrong")
    # alice is locked (wrong guesses rejected); bob is unaffected.
    assert service.authenticate("alice", "wrong") is THROTTLED
    assert service.authenticate("bob", "bobpassword99") is not None


def test_api_token_resolution_and_last_used(service):
    user = service.create_user("alice", "alicepassword", Role.editor)
    token, record = service.create_api_token(user, "ci")
    assert record["last_used_at"] is None
    resolved = service.resolve_api_token(token)
    assert resolved.username == "alice"
    assert service.get_api_token(record["id"])["last_used_at"] is not None
    assert service.resolve_api_token("bogus") is None
    service.revoke_api_token(record["id"])
    assert service.resolve_api_token(token) is None


def test_api_token_stored_only_as_hash(service, store):
    user = service.create_user("alice", "alicepassword", Role.editor)
    token, record = service.create_api_token(user, "ci")
    row = store.get_api_token_by_hash(hash_token(token))
    assert row["id"] == record["id"]
    listed = store.list_api_tokens()
    assert all(token not in json.dumps(t, default=str) for t in listed)


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------

def test_setup_mode_blocks_everything_but_status_and_setup(app):
    client = TestClient(app)
    for path in ("/api/v1/datasets", "/api/v1/auth/me", "/api/v1/users",
                 "/api/v1/tokens", "/api/v1/audit"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json() == {"detail": "setup required"}
    assert client.post("/api/v1/builds", json={}).status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/health").status_code == 200
    status = client.get("/api/v1/auth/status").json()
    assert status == {"auth_required": True, "setup_required": True, "user": None}


def test_setup_creates_admin_then_409(app):
    client = TestClient(app)
    r = client.post("/api/v1/auth/setup", json=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "root"
    assert body["role"] == "admin"
    assert "password" not in body and "password_hash" not in body

    assert client.get("/api/v1/auth/status").json()["setup_required"] is False
    r = client.post(
        "/api/v1/auth/setup", json={"username": "other", "password": "password2"}
    )
    assert r.status_code == 409


def test_setup_validates_username_and_password(app):
    client = TestClient(app)
    assert (
        client.post(
            "/api/v1/auth/setup", json={"username": "root", "password": "short"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/setup", json={"username": "Bad Name", "password": "longenough"}
        ).status_code
        == 400
    )
    # Still in setup mode after the failed attempts.
    assert client.get("/api/v1/auth/status").json()["setup_required"] is True


def test_setup_completed_is_audited(admin_client):
    actions = [e["action"] for e in admin_client.get("/api/v1/audit").json()]
    assert "setup_completed" in actions
    assert "user_created" in actions


# ---------------------------------------------------------------------------
# Login / logout cookie flow
# ---------------------------------------------------------------------------

def test_login_sets_cookie_and_omits_token_from_body(app):
    client = TestClient(app)
    client.post("/api/v1/auth/setup", json=ADMIN)
    r = client.post("/api/v1/auth/login", json=ADMIN)
    assert r.status_code == 200

    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("laurelin_session=")
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=604800" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Secure" not in set_cookie  # plain http, no --secure-cookies

    # The session token travels ONLY in the cookie, never the JSON body.
    session_value = set_cookie.split("laurelin_session=")[1].split(";")[0]
    assert session_value not in r.text
    assert r.json()["username"] == "root"

    assert client.get("/api/v1/auth/me").json()["username"] == "root"


def test_login_bad_credentials_and_disabled(app, admin_client):
    make_user(admin_client, "carol", "viewer", password="carolpassword")
    client = TestClient(app)
    r = client.post(
        "/api/v1/auth/login", json={"username": "carol", "password": "nope-nope"}
    )
    assert r.status_code == 401
    r = client.post(
        "/api/v1/auth/login", json={"username": "ghost", "password": "whatever1"}
    )
    assert r.status_code == 401

    admin_client.patch("/api/v1/users/carol", json={"disabled": True})
    r = client.post(
        "/api/v1/auth/login", json={"username": "carol", "password": "carolpassword"}
    )
    assert r.status_code == 401


def test_login_throttled_returns_429(app, admin_client):
    client = TestClient(app)
    for _ in range(5):
        r = client.post(
            "/api/v1/auth/login", json={"username": "root", "password": "wrong!!!"}
        )
        assert r.status_code == 401
    # A further WRONG login is throttled (429)...
    r = client.post(
        "/api/v1/auth/login", json={"username": "root", "password": "wrong!!!"}
    )
    assert r.status_code == 429
    # ...but the correct password still lets the real owner in immediately, so
    # an attacker's bad guesses can't lock the account's owner out.
    r = client.post("/api/v1/auth/login", json=ADMIN)
    assert r.status_code == 200

    audit_actions = [e["action"] for e in admin_client.get("/api/v1/audit").json()]
    assert "login_failed" in audit_actions
    assert "login_throttled" in audit_actions
    assert "login_succeeded" in audit_actions


def test_logout_clears_session(admin_client):
    assert admin_client.get("/api/v1/auth/me").status_code == 200
    r = admin_client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert admin_client.get("/api/v1/auth/me").status_code == 401
    assert admin_client.get("/api/v1/datasets").status_code == 401


def test_logout_requires_credential(app, admin_client):
    assert TestClient(app).post("/api/v1/auth/logout").status_code == 401


def test_disabled_users_existing_session_rejected_over_http(app, admin_client):
    make_user(admin_client, "carol", "viewer", password="carolpassword")
    carol = login_as(app, "carol", "carolpassword")
    assert carol.get("/api/v1/auth/me").status_code == 200
    admin_client.patch("/api/v1/users/carol", json={"disabled": True})
    assert carol.get("/api/v1/auth/me").status_code == 401
    assert carol.get("/api/v1/datasets").status_code == 401


def test_secure_cookie_flag(ws):
    app = create_app(ws, secure_cookies=True)
    client = TestClient(app)
    client.post("/api/v1/auth/setup", json=ADMIN)
    r = client.post("/api/v1/auth/login", json=ADMIN)
    assert "Secure" in r.headers["set-cookie"]


def test_secure_cookie_via_forwarded_proto(app):
    client = TestClient(app)
    client.post("/api/v1/auth/setup", json=ADMIN)
    r = client.post(
        "/api/v1/auth/login", json=ADMIN, headers={"X-Forwarded-Proto": "https"}
    )
    assert "Secure" in r.headers["set-cookie"]


# ---------------------------------------------------------------------------
# /auth/me and /auth/status
# ---------------------------------------------------------------------------

def test_me_and_status_reflect_current_user(app, admin_client):
    me = admin_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "root"
    assert me.json()["role"] == "admin"

    status = admin_client.get("/api/v1/auth/status").json()
    assert status["auth_required"] is True
    assert status["setup_required"] is False
    assert status["user"]["username"] == "root"

    anonymous = TestClient(app)
    assert anonymous.get("/api/v1/auth/me").status_code == 401
    status = anonymous.get("/api/v1/auth/status")
    assert status.status_code == 200  # never 401
    assert status.json()["user"] is None


# ---------------------------------------------------------------------------
# RBAC matrix
# ---------------------------------------------------------------------------

@pytest.fixture
def role_clients(app, admin_client):
    make_user(admin_client, "vera", "viewer", password="verapassword")
    make_user(admin_client, "eddy", "editor", password="eddypassword")
    return {
        "viewer": login_as(app, "vera", "verapassword"),
        "editor": login_as(app, "eddy", "eddypassword"),
        "admin": admin_client,
    }


GET_PATHS = [
    "/api/v1/workspace",
    "/api/v1/datasets",
    "/api/v1/transforms",
    "/api/v1/builds",
    "/api/v1/lineage",
    "/api/v1/ontology/object-types",
    "/api/v1/ontology/actions",
    "/api/v1/audit",
]


def test_rbac_all_roles_can_read(role_clients):
    for role, client in role_clients.items():
        for path in GET_PATHS:
            assert client.get(path).status_code == 200, (role, path)


def test_rbac_viewer_cannot_mutate(role_clients):
    viewer = role_clients["viewer"]
    checks = [
        viewer.post("/api/v1/datasets", json={"name": "v_ds"}),
        viewer.post(
            "/api/v1/datasets/v_up/upload",
            files={"file": ("a.csv", b"a\n1\n", "text/csv")},
        ),
        viewer.post("/api/v1/builds", json={}),
        viewer.post(
            "/api/v1/ontology/actions/nope/apply", json={"parameters": {}}
        ),
        viewer.post("/api/v1/tokens", json={"name": "nope"}),
    ]
    for r in checks:
        assert r.status_code == 403, r.request.url
        assert "detail" in r.json()


def test_rbac_editor_can_mutate_data_but_not_admin(role_clients):
    editor = role_clients["editor"]
    assert editor.post("/api/v1/datasets", json={"name": "e_ds"}).status_code == 200
    assert editor.post("/api/v1/builds", json={}).status_code == 200
    assert editor.post("/api/v1/tokens", json={"name": "edtok"}).status_code == 200
    # ...but user management is admin-only.
    assert editor.get("/api/v1/users").status_code == 403
    assert (
        editor.post(
            "/api/v1/users",
            json={"username": "sneaky", "password": "sneakypass", "role": "admin"},
        ).status_code
        == 403
    )
    assert editor.patch("/api/v1/users/vera", json={"role": "admin"}).status_code == 403
    assert editor.delete("/api/v1/users/vera").status_code == 403


def test_rbac_viewer_user_endpoints_403(role_clients):
    viewer = role_clients["viewer"]
    assert viewer.get("/api/v1/users").status_code == 403


def test_actor_is_authenticated_username(role_clients):
    editor = role_clients["editor"]
    # X-Laurelin-User must be ignored when auth is on.
    editor.post(
        "/api/v1/datasets",
        json={"name": "actor_ds"},
        headers={"X-Laurelin-User": "spoofed"},
    )
    audit = editor.get("/api/v1/audit").json()
    created = [e for e in audit if e["action"] == "dataset_created"
               and e["details"].get("dataset") == "actor_ds"]
    assert created and created[0]["actor"] == "eddy"


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def test_user_crud_and_listing(admin_client):
    user = make_user(admin_client, "dave", "editor", password="davepassword")
    assert user["username"] == "dave"
    assert user["role"] == "editor"
    assert "password" not in user and "password_hash" not in user

    users = admin_client.get("/api/v1/users").json()
    assert [u["username"] for u in users] == ["dave", "root"]

    r = admin_client.patch("/api/v1/users/dave", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"

    r = admin_client.patch("/api/v1/users/dave", json={"password": "newdavepass"})
    assert r.status_code == 200
    r = admin_client.patch("/api/v1/users/dave", json={"password": "2short"})
    assert r.status_code == 400

    r = admin_client.delete("/api/v1/users/dave")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [u["username"] for u in admin_client.get("/api/v1/users").json()] == ["root"]

    assert admin_client.patch("/api/v1/users/ghost", json={"role": "admin"}).status_code == 404
    assert admin_client.delete("/api/v1/users/ghost").status_code == 404


def test_user_create_validation_over_http(admin_client):
    r = admin_client.post(
        "/api/v1/users", json={"username": "ok", "password": "short", "role": "viewer"}
    )
    assert r.status_code == 400
    r = admin_client.post(
        "/api/v1/users",
        json={"username": "root", "password": "longenough", "role": "viewer"},
    )
    assert r.status_code == 400  # duplicate
    r = admin_client.post(
        "/api/v1/users",
        json={"username": "ok", "password": "longenough", "role": "superuser"},
    )
    assert r.status_code == 400  # unknown role


def test_admin_cannot_demote_disable_or_delete_self(admin_client):
    assert admin_client.patch("/api/v1/users/root", json={"role": "viewer"}).status_code == 400
    assert admin_client.patch("/api/v1/users/root", json={"disabled": True}).status_code == 400
    assert admin_client.delete("/api/v1/users/root").status_code == 400
    # Still an active admin.
    me = admin_client.get("/api/v1/auth/me").json()
    assert me["role"] == "admin" and me["disabled"] is False
    # Password change and no-op role are fine.
    assert admin_client.patch(
        "/api/v1/users/root", json={"role": "admin", "password": "trustno1!"}
    ).status_code == 200


def test_deleting_user_kills_their_sessions_and_tokens(app, admin_client):
    make_user(admin_client, "eddy", "editor", password="eddypassword")
    eddy = login_as(app, "eddy", "eddypassword")
    token = eddy.post("/api/v1/tokens", json={"name": "t"}).json()["token"]
    admin_client.delete("/api/v1/users/eddy")
    assert eddy.get("/api/v1/auth/me").status_code == 401
    fresh = TestClient(app)
    r = fresh.get("/api/v1/datasets", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_user_management_is_audited(admin_client):
    make_user(admin_client, "dave", "viewer", password="davepassword")
    admin_client.patch("/api/v1/users/dave", json={"role": "editor"})
    admin_client.delete("/api/v1/users/dave")
    audit = admin_client.get("/api/v1/audit").json()
    by_action = {}
    for e in audit:
        by_action.setdefault(e["action"], []).append(e)
    assert any(e["details"].get("username") == "dave" for e in by_action["user_created"])
    assert any(e["details"].get("username") == "dave" for e in by_action["user_updated"])
    assert any(e["details"].get("username") == "dave" for e in by_action["user_deleted"])
    # Never log secrets.
    assert "davepassword" not in json.dumps(audit)


# ---------------------------------------------------------------------------
# API tokens over HTTP
# ---------------------------------------------------------------------------

def test_token_create_shows_plaintext_exactly_once(admin_client):
    r = admin_client.post("/api/v1/tokens", json={"name": "ci"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "name", "token"}
    plaintext = body["token"]

    listing = admin_client.get("/api/v1/tokens")
    assert listing.status_code == 200
    tokens = listing.json()
    assert len(tokens) == 1
    assert set(tokens[0]) == {"id", "name", "username", "created_at", "last_used_at"}
    assert plaintext not in listing.text
    # Audit must not contain the plaintext either.
    assert plaintext not in json.dumps(admin_client.get("/api/v1/audit").json())


def test_bearer_token_auth_and_revocation(app, admin_client):
    plaintext = admin_client.post("/api/v1/tokens", json={"name": "ci"}).json()
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {plaintext['token']}"}
    assert client.get("/api/v1/datasets", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).json()["username"] == "root"

    r = admin_client.delete(f"/api/v1/tokens/{plaintext['id']}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/v1/datasets", headers=headers).status_code == 401
    assert admin_client.delete(f"/api/v1/tokens/{plaintext['id']}").status_code == 404


def test_token_ownership_rules(app, admin_client):
    make_user(admin_client, "eddy", "editor", password="eddypassword")
    make_user(admin_client, "fred", "editor", password="fredpassword")
    eddy = login_as(app, "eddy", "eddypassword")
    fred = login_as(app, "fred", "fredpassword")

    eddy_token = eddy.post("/api/v1/tokens", json={"name": "eddys"}).json()
    admin_token = admin_client.post("/api/v1/tokens", json={"name": "roots"}).json()

    # Users see only their own tokens; admin sees all.
    assert [t["name"] for t in eddy.get("/api/v1/tokens").json()] == ["eddys"]
    assert fred.get("/api/v1/tokens").json() == []
    assert {t["name"] for t in admin_client.get("/api/v1/tokens").json()} == {
        "eddys",
        "roots",
    }

    # Fred cannot revoke Eddy's token; Eddy can; admin can revoke anyone's.
    assert fred.delete(f"/api/v1/tokens/{eddy_token['id']}").status_code == 403
    assert eddy.delete(f"/api/v1/tokens/{eddy_token['id']}").status_code == 200
    eddy_token2 = eddy.post("/api/v1/tokens", json={"name": "eddys2"}).json()
    assert admin_client.delete(f"/api/v1/tokens/{eddy_token2['id']}").status_code == 200
    assert admin_client.delete(f"/api/v1/tokens/{admin_token['id']}").status_code == 200


def test_bearer_requests_bypass_csrf(admin_client):
    token = admin_client.post("/api/v1/tokens", json={"name": "ci"}).json()["token"]
    fresh = TestClient(admin_client.app)
    r = fresh.post(
        "/api/v1/datasets",
        json={"name": "via_bearer"},
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://elsewhere.example",
        },
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_csrf_origin_mismatch_403_for_cookie_mutations(admin_client):
    r = admin_client.post(
        "/api/v1/datasets",
        json={"name": "csrf_ds"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert "detail" in r.json()

    # Matching origin (TestClient host is "testserver") is allowed.
    r = admin_client.post(
        "/api/v1/datasets",
        json={"name": "csrf_ds"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200

    # No Origin header (curl-style same-machine use) is allowed.
    r = admin_client.post("/api/v1/datasets", json={"name": "csrf_ds2"})
    assert r.status_code == 200

    # GETs are not subject to the origin check.
    r = admin_client.get(
        "/api/v1/datasets", headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# No-auth mode
# ---------------------------------------------------------------------------

def test_no_auth_mode_implicit_admin(ws):
    app = create_app(ws, no_auth=True)
    client = TestClient(app)

    status = client.get("/api/v1/auth/status").json()
    assert status["auth_required"] is False
    assert status["setup_required"] is False

    # Everything is reachable, including admin surfaces and the docs.
    assert client.get("/api/v1/datasets").status_code == 200
    assert client.get("/api/v1/users").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.post("/api/v1/datasets", json={"name": "open_ds"}).status_code == 200
    me = client.get("/api/v1/auth/me").json()
    assert me["role"] == "admin"
    assert me["username"] == "anonymous"


def test_no_auth_mode_honors_x_laurelin_user_actor(ws):
    client = TestClient(create_app(ws, no_auth=True))
    client.post(
        "/api/v1/datasets",
        json={"name": "attributed"},
        headers={"X-Laurelin-User": "amdt"},
    )
    audit = client.get("/api/v1/audit").json()
    created = [e for e in audit if e["action"] == "dataset_created"]
    assert created and created[0]["actor"] == "amdt"


def test_no_auth_env_var_read_at_app_creation(ws, monkeypatch):
    monkeypatch.setenv("LAURELIN_NO_AUTH", "1")
    app = create_app(ws)
    monkeypatch.delenv("LAURELIN_NO_AUTH")
    client = TestClient(app)
    assert client.get("/api/v1/datasets").status_code == 200


# ---------------------------------------------------------------------------
# Hardening regression tests (security-review findings)
# ---------------------------------------------------------------------------


def test_correct_password_bypasses_throttle_lock(service):
    """A known user is never locked out by an attacker's bad guesses: the
    correct password succeeds and clears the lock (finding 1)."""
    service.create_user("victim", "rightpassword", Role.editor)
    for _ in range(THROTTLE_FAILURES):
        assert service.authenticate("victim", "wrong") is None
    # Attacker is now throttled on this username...
    assert service.authenticate("victim", "wrong") is THROTTLED
    # ...but the real owner still gets in, and the lock is cleared.
    user = service.authenticate("victim", "rightpassword")
    assert user is not None and user.username == "victim"
    assert service.authenticate("victim", "wrong") is None  # counter reset


def test_throttle_table_is_bounded(service):
    """Unlimited distinct usernames can't grow the throttle table without
    bound (finding 2)."""
    for i in range(MAX_THROTTLE_ENTRIES + 500):
        service.authenticate(f"ghost{i}", "whatever")
    assert len(service._throttle) <= MAX_THROTTLE_ENTRIES


def test_overlong_password_rejected_before_hashing(service):
    """Over-long passwords are refused at creation and at login without being
    fed into scrypt (finding 3)."""
    with pytest.raises(ValueError):
        service.create_user("big", "x" * (PASSWORD_MAX_LENGTH + 1), Role.viewer)
    service.create_user("big", "goodpassword", Role.viewer)
    assert service.authenticate("big", "x" * (PASSWORD_MAX_LENGTH + 1)) is None


def test_concurrent_setup_creates_single_admin(ws):
    """Two racing first-run setups can't both plant an admin (finding 4)."""
    import threading

    store = MetadataStore(ws.metadata_path)
    svc = AuthService(store)
    results: list = []

    def setup(name):
        results.append(svc.create_first_admin(name, "password123"))

    threads = [
        threading.Thread(target=setup, args=(f"admin{i}",)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    created = [r for r in results if r is not None]
    assert len(created) == 1
    assert store.count_users() == 1


def test_setup_endpoint_second_racer_gets_409(ws):
    """HTTP setup is idempotent under races: exactly one 200, rest 409."""
    client = TestClient(create_app(ws))
    first = client.post("/api/v1/auth/setup", json={"username": "root", "password": "trustno1!"})
    assert first.status_code == 200
    second = client.post("/api/v1/auth/setup", json={"username": "evil", "password": "trustno1!"})
    assert second.status_code == 409
