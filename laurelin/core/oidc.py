"""OpenID Connect (OIDC) SSO — authorization-code flow with PKCE.

Covers the 90% of enterprise SSO (Okta, Entra ID, Google, Keycloak, Auth0, …):
the operator points Laurelin at an OIDC issuer and users sign in with their IdP.
On callback Laurelin JIT-provisions a local identity, maps IdP group claims to a
role (and optionally superadmin), and issues its normal session cookie — so the
rest of the app (RBAC, workspaces, ACLs) is unchanged.

Configuration is via environment (``OIDCConfig.from_env``); SSO is *enabled*
only when issuer + client id + client secret are all set. Network calls
(discovery, JWKS, token exchange) go through small methods so they can be mocked
in tests.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt

from laurelin.core.models import Role

log = logging.getLogger("laurelin.oidc")


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(sha256(verifier.encode()).digest())
    return verifier, challenge


def _parse_role_map(raw: str) -> dict[str, Role]:
    """Parse 'group1:admin,group2:editor' into {group: Role}."""
    out: dict[str, Role] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        group, role = part.split(":", 1)
        try:
            out[group.strip()] = Role(role.strip())
        except ValueError:
            continue
    return out


@dataclass
class OIDCConfig:
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_url: str = ""  # optional; else derived from the request
    scopes: str = "openid email profile"
    username_claim: str = "preferred_username"
    email_claim: str = "email"
    groups_claim: str = "groups"
    default_role: Role = Role.viewer
    role_map: dict[str, Role] = field(default_factory=dict)
    superadmin_group: str = ""
    provider_name: str = "SSO"

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "OIDCConfig":
        e = env if env is not None else os.environ
        default_role = Role.viewer
        try:
            default_role = Role(e.get("LAURELIN_OIDC_DEFAULT_ROLE", "viewer"))
        except ValueError:
            pass
        return cls(
            issuer=e.get("LAURELIN_OIDC_ISSUER", "").rstrip("/"),
            client_id=e.get("LAURELIN_OIDC_CLIENT_ID", ""),
            client_secret=e.get("LAURELIN_OIDC_CLIENT_SECRET", ""),
            redirect_url=e.get("LAURELIN_OIDC_REDIRECT_URL", ""),
            scopes=e.get("LAURELIN_OIDC_SCOPES", "openid email profile"),
            username_claim=e.get("LAURELIN_OIDC_USERNAME_CLAIM", "preferred_username"),
            email_claim=e.get("LAURELIN_OIDC_EMAIL_CLAIM", "email"),
            groups_claim=e.get("LAURELIN_OIDC_GROUPS_CLAIM", "groups"),
            default_role=default_role,
            role_map=_parse_role_map(e.get("LAURELIN_OIDC_ROLE_MAP", "")),
            superadmin_group=e.get("LAURELIN_OIDC_SUPERADMIN_GROUP", ""),
            provider_name=e.get("LAURELIN_OIDC_PROVIDER_NAME", "SSO"),
        )

    # -- claim mapping --------------------------------------------------------

    def username_for(self, claims: dict) -> Optional[str]:
        for key in (self.username_claim, self.email_claim, "sub"):
            val = claims.get(key)
            if isinstance(val, str) and val.strip():
                # Normalize to Laurelin's username charset (^[a-z0-9_.-]{2,32}$).
                return _normalize_username(val)
        return None

    def groups_of(self, claims: dict) -> list[str]:
        val = claims.get(self.groups_claim)
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [str(v) for v in val]
        return []

    def role_for(self, groups: list[str]) -> Role:
        """Highest role granted by the user's groups, else the default."""
        best = self.default_role
        for g in groups:
            mapped = self.role_map.get(g)
            if mapped is not None and mapped.rank > best.rank:
                best = mapped
        return best

    def is_superadmin(self, groups: list[str]) -> bool:
        return bool(self.superadmin_group) and self.superadmin_group in groups


def _normalize_username(raw: str) -> str:
    import re

    u = raw.strip().lower()
    u = re.sub(r"[^a-z0-9_.-]", ".", u)  # e.g. alice@corp.com -> alice.corp.com
    u = u[:32]
    if len(u) < 2:
        u = (u + "..")[:2]
    return u


class OIDCError(Exception):
    pass


class OIDCProvider:
    """Thin OIDC client. Network calls are isolated in overridable methods."""

    def __init__(self, config: OIDCConfig):
        self.config = config
        self._meta: Optional[dict] = None
        self._jwks: Any = None

    # -- network (mockable) ---------------------------------------------------

    def discover(self) -> dict:
        if self._meta is None:
            url = f"{self.config.issuer}/.well-known/openid-configuration"
            resp = httpx.get(url, timeout=10)
            resp.raise_for_status()
            self._meta = resp.json()
        return self._meta

    def jwks(self):
        if self._jwks is None:
            resp = httpx.get(self.discover()["jwks_uri"], timeout=10)
            resp.raise_for_status()
            self._jwks = JsonWebKey.import_key_set(resp.json())
        return self._jwks

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> dict:
        resp = httpx.post(
            self.discover()["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code_verifier": code_verifier,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            # R1: `resp.text[:200]` used to come back. The POST directly above
            # carries `client_secret`, and IdPs routinely echo request
            # parameters in `error_description` — so the body of a *failed*
            # token exchange is one of the likelier places for our own client
            # secret to be sitting. The status code is the diagnostic; the body
            # goes to the log.
            #
            # UNVERIFIED at runtime: no IdP available on this tree.
            log.warning(
                "OIDC token exchange failed with %s: %s",
                resp.status_code, resp.text[:2000],
            )
            raise OIDCError(f"Token exchange failed with HTTP {resp.status_code}")
        return resp.json()

    # -- flow -----------------------------------------------------------------

    def authorization_url(
        self, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.discover()['authorization_endpoint']}?{urlencode(params)}"

    def validate_id_token(self, id_token: str, nonce: str) -> dict:
        try:
            claims = jwt.decode(
                id_token,
                self.jwks(),
                claims_options={
                    "iss": {"essential": True, "value": self.config.issuer},
                    "aud": {"essential": True, "value": self.config.client_id},
                },
            )
            claims.validate()  # exp / iat / iss / aud
        except Exception as exc:  # noqa: BLE001 - any JWT failure is an auth failure
            raise OIDCError(f"Invalid id_token: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise OIDCError("id_token nonce mismatch")
        return dict(claims)
