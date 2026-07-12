"""Authentication primitives + AuthService.

Everything is stdlib crypto: scrypt for passwords, ``secrets`` for token
generation, sha256 for token storage, ``hmac.compare_digest`` for comparison.
Plaintext passwords and tokens are never stored or logged; only hashes land in
metadata.db.

Password hash format: ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User, utcnow_iso

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_LEN = 32

PASSWORD_MIN_LENGTH = 8
# Cap password length so an attacker can't feed a huge body into scrypt (each
# call already allocates ~16 MB); bcrypt-style KDFs use similar bounds.
PASSWORD_MAX_LENGTH = 1024
# Bound the in-memory throttle table so unlimited distinct usernames can't grow
# it without limit; least-recently-touched idle entries are evicted past this.
MAX_THROTTLE_ENTRIES = 8192
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,32}$")

SESSION_TTL = timedelta(days=7)
THROTTLE_FAILURES = 5
THROTTLE_LOCK_SECONDS = 30.0

# Sentinel returned by authenticate() when the username is currently locked out.
THROTTLED = object()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_LEN
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = _scrypt(password, bytes.fromhex(salt_hex), int(n), int(r), int(p))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def new_token() -> str:
    """A fresh credential string. Store only ``hash_token(token)``."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_username(username: str) -> str:
    if not USERNAME_RE.match(username or ""):
        raise ValueError(
            "Invalid username: must match ^[a-z0-9_.-]{2,32}$ "
            "(lowercase letters, digits, '_', '.', '-'; 2-32 chars)"
        )
    return username


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"Password must be at most {PASSWORD_MAX_LENGTH} characters"
        )
    return password


# A real scrypt hash of an unguessable password, used to equalize timing for
# unknown usernames (authenticate always runs scrypt exactly once).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class AuthService:
    """User, session, and API-token management over a MetadataStore.

    ``clock`` is a monotonic-seconds callable used for login throttling and
    ``now`` a tz-aware datetime callable used for session expiry — both are
    injectable so tests can drive time.
    """

    def __init__(
        self,
        store: MetadataStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store = store
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        # username (lowercase) -> {"failures": int, "locked_until": float | None}
        self._throttle: dict[str, dict[str, Any]] = {}

    # -- users ----------------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        role: Role | str = Role.viewer,
        *,
        superadmin: bool = False,
        actor: str = "system",
    ) -> User:
        validate_username(username)
        validate_password(password)
        role = Role(role)
        if self.store.get_user(username) is not None:
            raise ValueError(f"Username already exists: {username!r}")
        user = User(
            id=secrets.token_hex(8), username=username, role=role, superadmin=superadmin
        )
        self.store.create_user(user, hash_password(password))
        self.store.log_audit(
            "user_created",
            {"username": username, "role": role.value, "superadmin": superadmin},
            actor=actor,
        )
        return user

    def create_first_admin(
        self, username: str, password: str, *, superadmin: bool = False, actor: str = "setup"
    ) -> Optional[User]:
        """First-run admin creation. Returns None (creating nothing) if any user
        already exists, so two racing setups can't both plant an admin."""
        validate_username(username)
        validate_password(password)
        user = User(
            id=secrets.token_hex(8),
            username=username,
            role=Role.admin,
            superadmin=superadmin,
        )
        if not self.store.create_user_if_none_exist(user, hash_password(password)):
            return None
        self.store.log_audit(
            "user_created",
            {"username": username, "role": Role.admin.value, "superadmin": superadmin},
            actor=actor,
        )
        return user

    def provision_oidc_user(
        self, username: str, role: Role, superadmin: bool = False, *, actor: str = "oidc"
    ) -> User:
        """Find-or-create an SSO identity. A new user gets the IdP-mapped role and
        an unusable random password (they sign in only via SSO). Existing users
        keep their local role (a local admin can override the IdP mapping); their
        ``disabled`` flag still blocks login (checked by the caller)."""
        user = self.store.get_user(username)
        if user is not None:
            return user
        validate_username(username)
        user = User(
            id=secrets.token_hex(8), username=username, role=role, superadmin=superadmin
        )
        self.store.create_user(user, hash_password(secrets.token_urlsafe(32)))
        self.store.log_audit(
            "oidc_user_provisioned",
            {"username": username, "role": role.value, "superadmin": superadmin},
            actor=actor,
        )
        return user

    def get_user(self, username: str) -> Optional[User]:
        return self.store.get_user(username)

    def list_users(self) -> list[User]:
        return self.store.list_users()

    def update_user(
        self,
        username: str,
        *,
        role: Role | str | None = None,
        password: str | None = None,
        disabled: bool | None = None,
        actor: str = "system",
    ) -> User:
        user = self.store.get_user(username)
        if user is None:
            raise KeyError(f"User not found: {username!r}")
        changed: dict[str, Any] = {}
        if role is not None:
            changed["role"] = Role(role).value
        password_hash = None
        if password is not None:
            validate_password(password)
            password_hash = hash_password(password)
            changed["password"] = "(changed)"
        if disabled is not None:
            changed["disabled"] = bool(disabled)
        self.store.update_user(
            user.username,
            role=Role(role).value if role is not None else None,
            password_hash=password_hash,
            disabled=disabled,
        )
        self.store.log_audit(
            "user_updated", {"username": user.username, "changes": changed}, actor=actor
        )
        updated = self.store.get_user(username)
        assert updated is not None
        return updated

    def delete_user(self, username: str, *, actor: str = "system") -> None:
        user = self.store.get_user(username)
        if user is None:
            raise KeyError(f"User not found: {username!r}")
        self.store.delete_user(user.username)
        self.store.log_audit("user_deleted", {"username": user.username}, actor=actor)

    # -- login throttling -------------------------------------------------------

    def _throttle_state(self, username: str) -> dict[str, Any]:
        key = username.lower()
        if key not in self._throttle and len(self._throttle) >= MAX_THROTTLE_ENTRIES:
            self._prune_throttle()
        return self._throttle.setdefault(key, {"failures": 0, "locked_until": None})

    def _prune_throttle(self) -> None:
        """Drop entries whose lock has expired (or never locked). Called under
        _lock when the table hits its cap, so it can't grow without bound."""
        now = self._clock()
        for key in [
            k
            for k, s in self._throttle.items()
            if s["locked_until"] is None or now >= s["locked_until"]
        ]:
            del self._throttle[key]

    def _is_throttled(self, username: str) -> bool:
        with self._lock:
            state = self._throttle_state(username)
            locked_until = state["locked_until"]
            if locked_until is None:
                return False
            if self._clock() >= locked_until:
                # Lock expired: give the user a fresh set of attempts.
                state["locked_until"] = None
                state["failures"] = 0
                return False
            return True

    def _record_failure(self, username: str) -> None:
        with self._lock:
            state = self._throttle_state(username)
            state["failures"] += 1
            # Arm the lock once when the threshold is first crossed; don't slide
            # it on every subsequent guess, so the lockout is a fixed window that
            # _is_throttled clears on expiry rather than an ever-extending one.
            if state["failures"] >= THROTTLE_FAILURES and state["locked_until"] is None:
                state["locked_until"] = self._clock() + THROTTLE_LOCK_SECONDS

    def _record_success(self, username: str) -> None:
        with self._lock:
            self._throttle.pop(username.lower(), None)

    # -- authentication -----------------------------------------------------------

    def authenticate(self, username: str, password: str):
        """Returns the User on success, None on bad credentials or a disabled
        account, or the THROTTLED sentinel when the username is locked out AND
        the supplied password is wrong.

        The correct password always succeeds and clears the lock, so an attacker
        spamming bad logins for a known username cannot lock the legitimate owner
        out — the lock only ever rejects further *wrong* guesses (returned as
        THROTTLED / 429 to slow brute force)."""
        # Reject over-long passwords before touching scrypt (amplification guard).
        if not isinstance(password, str) or len(password) > PASSWORD_MAX_LENGTH:
            self._record_failure(username)
            return THROTTLED if self._is_throttled(username) else None
        throttled = self._is_throttled(username)
        user = self.store.get_user(username)
        stored_hash = self.store.get_password_hash(username) if user else None
        # Always run scrypt exactly once so unknown usernames take as long as
        # wrong passwords.
        ok = verify_password(password, stored_hash or _DUMMY_HASH)
        if user is None or stored_hash is None or not ok or user.disabled:
            self._record_failure(username)
            return THROTTLED if throttled else None
        self._record_success(username)
        return user

    def login(self, user: User) -> tuple[str, User]:
        """Create a session for an already-authenticated user."""
        token = new_token()
        now = self._now()
        self.store.create_session(
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=now.isoformat(),
            expires_at=(now + SESSION_TTL).isoformat(),
        )
        return token, user

    def logout(self, token: str) -> None:
        self.store.delete_session(hash_token(token))

    def resolve_session(self, token: str) -> Optional[User]:
        now = self._now()
        self.store.purge_expired_sessions(now.isoformat())
        row = self.store.get_session(hash_token(token))
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if now >= expires:
            self.store.delete_session(row["token_hash"])
            return None
        user = self.store.get_user_by_id(row["user_id"])
        if user is None or user.disabled:
            return None
        return user

    # -- API tokens ------------------------------------------------------------------

    def create_api_token(self, user: User, name: str, *, actor: str = "system") -> tuple[str, dict]:
        """Returns ``(plaintext_token, record)``. The plaintext is shown once
        and never stored."""
        if not name or not name.strip():
            raise ValueError("Token name must not be empty")
        token = new_token()
        record = self.store.create_api_token(
            token_id=secrets.token_hex(8),
            name=name.strip(),
            token_hash=hash_token(token),
            user_id=user.id,
            created_at=utcnow_iso(),
        )
        self.store.log_audit(
            "token_created",
            {"token_id": record["id"], "name": record["name"], "username": user.username},
            actor=actor,
        )
        return token, record

    def resolve_api_token(self, token: str) -> Optional[User]:
        row = self.store.get_api_token_by_hash(hash_token(token))
        if row is None:
            return None
        user = self.store.get_user_by_id(row["user_id"])
        if user is None or user.disabled:
            return None
        self.store.touch_api_token(row["id"], utcnow_iso())
        return user

    def list_api_tokens(self, user: User | None = None) -> list[dict]:
        return self.store.list_api_tokens(user_id=user.id if user else None)

    def get_api_token(self, token_id: str) -> Optional[dict]:
        return self.store.get_api_token(token_id)

    def revoke_api_token(self, token_id: str, *, actor: str = "system") -> None:
        row = self.store.get_api_token(token_id)
        if row is None:
            raise KeyError(f"Token not found: {token_id!r}")
        self.store.delete_api_token(token_id)
        self.store.log_audit(
            "token_revoked",
            {"token_id": token_id, "name": row["name"]},
            actor=actor,
        )
