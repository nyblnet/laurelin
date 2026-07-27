"""Tests for SAML 2.0 SSO. A pysaml2 IdP (fake, with a self-signed cert) mints a
REAL signed SAML assertion, so the SP's signature/condition validation actually
runs (via xmlsec1). Skipped if xmlsec1 isn't available."""

import base64
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app
from laurelin.core.config import Workspace

pytest.importorskip("saml2")

from saml2.sigver import get_xmlsec_binary  # noqa: E402

if not get_xmlsec_binary():  # pragma: no cover
    pytest.skip("xmlsec1 binary not available", allow_module_level=True)

SP_ENTITY = "https://laurelin.test/saml/metadata"
SP_ACS = "https://laurelin.test/api/v1/auth/saml/acs"
IDP_ENTITY = "https://idp.test/idp"


def _selfsigned(tmp_path):
    """Write a self-signed cert + key for the fake IdP, return (cert_path, key_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "idp.crt"
    key_path = tmp_path / "idp.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def _idp_server(tmp_path, cert, key):
    from saml2 import BINDING_HTTP_REDIRECT
    from saml2.config import IdPConfig
    from saml2.server import Server

    cfg = {
        "entityid": IDP_ENTITY,
        "service": {
            "idp": {
                "endpoints": {
                    "single_sign_on_service": [
                        (f"{IDP_ENTITY}/sso", BINDING_HTTP_REDIRECT),
                    ],
                },
                "name_id_format": ["urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"],
            }
        },
        "key_file": key,
        "cert_file": cert,
        "metadata": {"inline": [_SP_METADATA]},
        "allow_unknown_attributes": True,
    }
    conf = IdPConfig().load(cfg)
    return Server(config=conf)


# SP metadata the fake IdP needs (entityid + ACS).
_SP_METADATA = f"""<?xml version="1.0"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{SP_ENTITY}">
  <SPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <AssertionConsumerService index="0"
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
      Location="{SP_ACS}"/>
  </SPSSODescriptor>
</EntityDescriptor>"""


def _make_signed_response(idp, username, groups):
    from saml2.xmldsig import DIGEST_SHA256, SIG_RSA_SHA256

    name_id = idp.ident.transient_nameid(SP_ENTITY, username)
    resp = idp.create_authn_response(
        identity={"uid": [username], "groups": groups},
        in_response_to="id-request",
        destination=SP_ACS,
        sp_entity_id=SP_ENTITY,
        name_id=name_id,
        authn={"class_ref": "urn:oasis:names:tc:SAML:2.0:ac:classes:Password"},
        sign_assertion=True,
        sign_alg=SIG_RSA_SHA256,      # OpenSSL 3 / xmlsec 1.2.41 reject SHA1
        digest_alg=DIGEST_SHA256,
    )
    return base64.b64encode(str(resp).encode()).decode()


@pytest.fixture()
def idp(tmp_path):
    cert, key = _selfsigned(tmp_path)
    return _idp_server(tmp_path, cert, key)


@pytest.fixture()
def app(tmp_path, monkeypatch, idp):
    # Point the SP at the IdP's metadata (inline).
    from saml2.metadata import entity_descriptor

    idp_md = str(entity_descriptor(idp.config))
    monkeypatch.setenv("LAURELIN_SAML_IDP_METADATA", idp_md)
    monkeypatch.setenv("LAURELIN_SAML_SP_ENTITY_ID", SP_ENTITY)
    monkeypatch.setenv("LAURELIN_SAML_ACS_URL", SP_ACS)
    monkeypatch.setenv("LAURELIN_SAML_USERNAME_ATTR", "uid")
    monkeypatch.setenv("LAURELIN_SAML_GROUPS_ATTR", "groups")
    monkeypatch.setenv("LAURELIN_SAML_ROLE_MAP", "admins:admin,editors:editor")
    monkeypatch.setenv("LAURELIN_SAML_PROVIDER_NAME", "TestSAML")
    ws = Workspace.init(tmp_path / "ws", name="saml")
    app = create_app(ws)
    app.state.auth.create_first_admin("root", "trustno1!")
    return app


def test_status_reports_saml(app):
    s = TestClient(app).get("/api/v1/auth/status").json()
    assert s["saml"] == {"enabled": True, "provider_name": "TestSAML"}


def test_disabled_when_unconfigured(tmp_path):
    ws = Workspace.init(tmp_path / "ws2", name="nosaml")
    c = TestClient(create_app(ws, no_auth=True))
    assert c.get("/api/v1/auth/status").json()["saml"] == {"enabled": False}
    assert c.get("/api/v1/auth/saml/login", follow_redirects=False).status_code == 404


def test_metadata_and_login_redirect(app):
    c = TestClient(app)
    md = c.get("/api/v1/auth/saml/metadata")
    assert md.status_code == 200 and SP_ENTITY in md.text
    r = c.get("/api/v1/auth/saml/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(f"{IDP_ENTITY}/sso")


def test_signed_response_logs_in_and_maps_role(app, idp):
    c = TestClient(app)
    saml_response = _make_signed_response(idp, "alice", ["editors"])
    r = c.post("/api/v1/auth/saml/acs", data={"SAMLResponse": saml_response},
               follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert "laurelin_session" in r.headers.get("set-cookie", "")
    me = c.get("/api/v1/auth/me").json()
    assert me["username"] == "alice" and me["role"] == "editor"


def test_tampered_response_is_rejected(app, idp):
    c = TestClient(app)
    good = _make_signed_response(idp, "mallory", [])
    raw = base64.b64decode(good).decode()
    # Flip the username in the signed assertion -> signature no longer matches.
    tampered = base64.b64encode(raw.replace("mallory", "attacker").encode()).decode()
    r = c.post("/api/v1/auth/saml/acs", data={"SAMLResponse": tampered},
               follow_redirects=False)
    assert r.status_code == 400
