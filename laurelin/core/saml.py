"""SAML 2.0 SSO (Service Provider) — the legacy 10% of enterprise identity.

Thin wrapper over pysaml2. Enabled when ``LAURELIN_SAML_IDP_METADATA`` (a URL,
file path, or inline XML) and ``LAURELIN_SAML_SP_ENTITY_ID`` are set. Supports
SP-initiated (redirect to the IdP) and IdP-initiated (unsolicited POST) flows;
IdP assertions must be signed (verified via xmlsec1). On a valid response we
JIT-provision a local identity and map group attributes to a role, then issue
the normal Laurelin session — identical to the OIDC path downstream.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from laurelin.core.models import Role
from laurelin.core.oidc import _normalize_username, _parse_role_map


@dataclass
class SAMLConfig:
    idp_metadata: str = ""       # URL, file path, or inline XML
    sp_entity_id: str = ""
    acs_url: str = ""            # Assertion Consumer Service (our callback)
    username_attr: str = ""      # attribute holding the username; else the NameID
    groups_attr: str = "groups"
    default_role: Role = Role.viewer
    role_map: dict[str, Role] = field(default_factory=dict)
    superadmin_group: str = ""
    provider_name: str = "SAML"

    @property
    def enabled(self) -> bool:
        return bool(self.idp_metadata and self.sp_entity_id)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "SAMLConfig":
        e = env if env is not None else os.environ
        default_role = Role.viewer
        try:
            default_role = Role(e.get("LAURELIN_SAML_DEFAULT_ROLE", "viewer"))
        except ValueError:
            pass
        return cls(
            idp_metadata=e.get("LAURELIN_SAML_IDP_METADATA", ""),
            sp_entity_id=e.get("LAURELIN_SAML_SP_ENTITY_ID", ""),
            acs_url=e.get("LAURELIN_SAML_ACS_URL", ""),
            username_attr=e.get("LAURELIN_SAML_USERNAME_ATTR", ""),
            groups_attr=e.get("LAURELIN_SAML_GROUPS_ATTR", "groups"),
            default_role=default_role,
            role_map=_parse_role_map(e.get("LAURELIN_SAML_ROLE_MAP", "")),
            superadmin_group=e.get("LAURELIN_SAML_SUPERADMIN_GROUP", ""),
            provider_name=e.get("LAURELIN_SAML_PROVIDER_NAME", "SAML"),
        )

    def role_for(self, groups: list[str]) -> Role:
        best = self.default_role
        for g in groups:
            mapped = self.role_map.get(g)
            if mapped is not None and mapped.rank > best.rank:
                best = mapped
        return best

    def is_superadmin(self, groups: list[str]) -> bool:
        return bool(self.superadmin_group) and self.superadmin_group in groups


def _metadata_section(idp_metadata: str) -> dict:
    """Return the pysaml2 metadata config for a URL / file / inline-XML source."""
    s = idp_metadata.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return {"remote": [{"url": s}]}
    if s.startswith("<"):
        return {"inline": [s]}
    return {"local": [s]}


class SAMLProvider:
    def __init__(self, config: SAMLConfig):
        self.config = config

    def _client(self):
        from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
        from saml2.client import Saml2Client
        from saml2.config import Config

        cfg = {
            "entityid": self.config.sp_entity_id,
            "service": {
                "sp": {
                    "endpoints": {
                        "assertion_consumer_service": [
                            (self.config.acs_url, BINDING_HTTP_POST),
                        ],
                    },
                    "allow_unsolicited": True,   # accept IdP-initiated responses
                    "authn_requests_signed": False,
                    "want_assertions_signed": True,   # IdP must sign assertions
                    "want_response_signed": False,
                }
            },
            "metadata": _metadata_section(self.config.idp_metadata),
            "allow_unknown_attributes": True,
        }
        conf = Config().load(cfg)
        return Saml2Client(config=conf)

    def metadata_xml(self) -> str:
        from saml2.metadata import create_metadata_string

        client = self._client()
        return create_metadata_string(None, config=client.config).decode()

    def login_redirect(self) -> str:
        """Return the IdP SSO URL to redirect the browser to (HTTP-Redirect)."""
        from saml2 import BINDING_HTTP_REDIRECT

        client = self._client()
        _reqid, info = client.prepare_for_authenticate(binding=BINDING_HTTP_REDIRECT)
        for key, value in info["headers"]:
            if key == "Location":
                return value
        raise RuntimeError("SAML: could not build the IdP redirect URL")

    def parse_response(self, saml_response_b64: str) -> tuple[str, list[str]]:
        """Validate a SAMLResponse and return (username, groups). Raises on any
        signature / condition failure."""
        from saml2 import BINDING_HTTP_POST

        client = self._client()
        authn = client.parse_authn_request_response(
            saml_response_b64, BINDING_HTTP_POST
        )
        if authn is None:
            raise ValueError("SAML: could not parse the response")
        identity = authn.get_identity() or {}  # {attr: [values]}
        # username: configured attribute, else the subject NameID
        username = None
        if self.config.username_attr and self.config.username_attr in identity:
            vals = identity[self.config.username_attr]
            if vals:
                username = str(vals[0])
        if not username:
            username = authn.get_subject().text
        if not username:
            raise ValueError("SAML: response has no usable username")
        groups = [str(g) for g in identity.get(self.config.groups_attr, [])]
        return _normalize_username(username), groups
