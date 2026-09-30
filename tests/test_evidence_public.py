"""General evidence tools over a bundle carrying public Sigstore evidence.

The anchored bundle is built offline with ``sigstore_fakes`` (a fake Fulcio, Rekor and TSA), so
no production identity or log entry is committed. Verification is offline: the fake trusted root
stands in for a TUF refresh, and the official verifier checks the certificate at the
countersigned time.
"""

import asyncio
import base64
import json
import shutil
from datetime import datetime, timedelta

import httpx
import pytest

from sigstore_fakes import IDENTITY, FakeSigstore, TestTsa, forged_identity_token, notarized_bundle

from runner import claude_science_mcp as connector
from runner.api import create_app
from runner.graph import PUBLIC_EVIDENCE_ACTIONS, VerifyPolicy, falsify, project
from vatlog.bundle import Bundle
from vatlog.roles import Keyring, TrustBundle
from vatlog.sigstore_transparency import anchor_receipt, parse_trusted_root, receipt_payload_digest

pytest.importorskip("sigstore", reason="public-transparency dependency group not installed")
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl binary needed to mint test RFC 3161 tokens")

REKOR_INDEX = "1"  # FakeRekorLog starts with one pre-existing leaf


@pytest.fixture(scope="module")
def anchored_dir(tmp_path_factory):
    """anchored.zip, trust.json and trusted_root.json written the way the CLI would hand them over."""
    work = tmp_path_factory.mktemp("anchored")
    ring = Keyring.generate()
    fake = FakeSigstore(TestTsa(work / "tsa"))
    source = notarized_bundle(work / "session", ring)
    anchored, _ = anchor_receipt(source, ring.trust_bundle(), fake, acknowledged=True, trusted_root=fake.trusted_root(),
                                 recovery_dir=work / "recovery", token_source=forged_identity_token)
    out = work / "evidence"
    out.mkdir()
    anchored.save(out / "anchored.zip")
    ring.trust_bundle().save(out / "trust.json")
    (out / "trusted_root.json").write_text(fake.trusted_root_json())
    return out


@pytest.fixture(scope="module")
def anchored(anchored_dir):
    bundle = Bundle.load(anchored_dir / "anchored.zip")
    trust = TrustBundle.load(anchored_dir / "trust.json")
    root = parse_trusted_root((anchored_dir / "trusted_root.json").read_bytes())
    return bundle, trust, root


def policy(root, identity=IDENTITY, **kwargs):
    return VerifyPolicy(sigstore_identity=identity, sigstore_trusted_root=root, **kwargs)


def test_anchored_fixture_carries_sigstore_evidence_and_no_private_material(anchored, anchored_dir):
    bundle, trust, _ = anchored
    assert bundle.records and bundle.notary_receipt is not None
    assert "private" not in (anchored_dir / "trust.json").read_text()
    assert bundle.transparency_evidence is None and bundle.sigstore_evidence is not None
    assert bundle.sigstore_evidence.identity == IDENTITY


def test_projection_verifies_public_evidence_only_under_verifier_policy(anchored):
    bundle, trust, root = anchored
    unverified = project(bundle, trust)["bundle_report"]
    assert unverified["outcome"] == "BAD_SIGNATURE" and unverified["public_transparency"] == "absent"
    graph = project(bundle, trust, policy=policy(root, require_public_transparency=True))
    report = graph["bundle_report"]
    assert report["outcome"] == "OK"
    assert report["public_transparency"].startswith("Sigstore Rekor inclusion verified")
    assert f"/ {REKOR_INDEX} " in report["rekor_log"] and report["public_signing_identity"].startswith(IDENTITY)
    assert datetime.fromisoformat(report["rfc3161_timestamp"].split(" ")[0]).utcoffset() == timedelta(0)
    assert "publication time" in report["rfc3161_timestamp"]
    assert report["notarization"] != "absent" and "simulated" in report["notarization"]
    assert graph["verify_policy"]["sigstore_trusted_root"] == "verifier-supplied"
    assert graph["public_evidence"]["claimed_log_index"] == REKOR_INDEX
    assert project(bundle, trust, policy=policy(root, identity="someone@example.com"))["bundle_report"]["outcome"] == "BAD_SIGNATURE"


@pytest.mark.parametrize("kind", sorted(PUBLIC_EVIDENCE_ACTIONS))
def test_public_evidence_falsifications_keep_records_and_fail_the_right_check(anchored, kind):
    bundle, trust, root = anchored
    original = bundle.model_dump()
    changed, description = falsify(bundle, {"kind": kind, "value": "someone@example.com"})
    assert bundle.model_dump() == original and description
    assert changed.records == bundle.records and changed.notary_receipt == bundle.notary_receipt
    report = project(changed, trust, policy=policy(root, require_public_transparency=True))["bundle_report"]
    failed = [check for check in report["checks"] if not check["passed"]]
    if kind == "strip_public_evidence":
        assert report["outcome"] == "NOT_INCLUDED" and failed[0]["detail"] == "Required public transparency evidence is missing"
    else:
        assert report["outcome"] == "BAD_SIGNATURE" and failed[0]["number"] == 2


def test_public_actions_need_public_evidence_and_valid_identity(anchored):
    bundle, _, _ = anchored
    with pytest.raises(ValueError):
        falsify(bundle, {"kind": "substitute_public_identity", "value": "not-an-email"})
    stripped, _ = falsify(bundle, {"kind": "strip_public_evidence"})
    with pytest.raises(ValueError):
        falsify(stripped, {"kind": "retarget_public_evidence"})


def test_evidence_api_applies_upload_policy_to_every_later_projection(anchored_dir):
    async def exercise():
        app = create_app()
        archive = base64.b64encode((anchored_dir / "anchored.zip").read_bytes()).decode()
        trust = json.loads((anchored_dir / "trust.json").read_text())
        root = (anchored_dir / "trusted_root.json").read_text()
        digest_prefix = receipt_payload_digest(Bundle.load(anchored_dir / "anchored.zip").notary_receipt)[:8]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            loaded = await client.post("/api/evidence", json={"archive": archive, "trust": trust, "public": {
                "sigstore_identity": IDENTITY, "sigstore_trusted_root": root, "require_public_transparency": True}})
            assert loaded.status_code == 200, loaded.text
            identifier = loaded.json()["id"]
            assert loaded.json()["graph"]["bundle_report"]["outcome"] == "OK"
            assert (await client.get(f"/api/evidence/{identifier}")).json() == loaded.json()
            stripped = await client.post(f"/api/evidence/{identifier}/experiment", json={"kind": "strip_public_evidence"})
            assert stripped.json()["graph"]["bundle_report"]["outcome"] == "NOT_INCLUDED"
            assert stripped.json()["graph"]["verify_policy"]["require_public_transparency"] is True
            archive_bytes = await client.get(f"/api/evidence/{identifier}/bundle")
            assert archive_bytes.content.startswith(b"PK") and archive_bytes.headers["content-type"] == "application/zip"
            trust_text = await client.get(f"/api/evidence/{identifier}/trust")
            assert "private" not in trust_text.text and TrustBundle.model_validate(trust_text.json())
            unpoliced = await client.post("/api/evidence", json={"archive": archive, "trust": trust})
            assert unpoliced.json()["graph"]["bundle_report"]["outcome"] == "BAD_SIGNATURE"
            for public in ({"sigstore_identity": IDENTITY, "sigstore_trusted_root": "{}"},
                           {"sigstore_identity": IDENTITY, "unknown": True},
                           {"require_public_transparency": "yes"}):
                rejected = await client.post("/api/evidence", json={"archive": archive, "trust": trust, "public": public})
                assert rejected.status_code == 422 and digest_prefix not in rejected.text
    asyncio.run(exercise())


def test_connector_reads_evidence_files_only_under_the_evidence_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": "MARKER"}')
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "trust.json").write_text('{"entries": []}')
    (inside / "link.json").symlink_to(outside)
    (inside / "big.zip").write_bytes(b"0" * (connector.MAX_TEXT_BYTES + 1))
    monkeypatch.setenv("VAT_EVIDENCE_DIR", str(inside))
    assert connector.read_local("trust.json", connector.MAX_TEXT_BYTES) == b'{"entries": []}'
    for path in ("../outside.json", str(outside), "link.json", "missing.json"):
        with pytest.raises(RuntimeError) as excinfo:
            connector.read_local(path, connector.MAX_TEXT_BYTES)
        assert "MARKER" not in str(excinfo.value) and str(outside) not in str(excinfo.value)
    with pytest.raises(RuntimeError, match="size limit"):
        connector.read_local("big.zip", connector.MAX_TEXT_BYTES)


def test_connector_load_evidence_forwards_files_and_policy_without_reading_them_itself(anchored_dir, monkeypatch):
    seen = {}
    monkeypatch.setenv("VAT_EVIDENCE_DIR", str(anchored_dir))
    monkeypatch.setattr(connector, "call", lambda method, path, payload=None: seen.update(method=method, path=path, payload=payload) or {"id": "x"})
    result = connector.load_evidence("anchored.zip", "trust.json", sigstore_identity=IDENTITY,
                                     sigstore_trust_root_path="trusted_root.json", require_public_transparency=True)
    assert result == {"id": "x"} and (seen["method"], seen["path"]) == ("POST", "/api/evidence")
    payload = seen["payload"]
    assert base64.b64decode(payload["archive"]) == (anchored_dir / "anchored.zip").read_bytes()
    assert payload["trust"] == json.loads((anchored_dir / "trust.json").read_text())
    assert payload["public"] == {"require_public_transparency": True, "sigstore_identity": IDENTITY, "sigstore_issuer": None,
                                 "sigstore_trusted_root": (anchored_dir / "trusted_root.json").read_text()}
    connector.load_evidence("anchored.zip", "trust.json")
    assert "public" not in seen["payload"]


def test_connector_serves_the_evidence_routes_in_process_without_any_backend(anchored_dir, monkeypatch):
    """Claude Science sandboxes the connector with no network and no AF_UNIX sockets."""
    monkeypatch.delenv("VAT_BACKEND_URL", raising=False)
    monkeypatch.delenv("VAT_BACKEND_MODE", raising=False)
    monkeypatch.setenv("VAT_EVIDENCE_DIR", str(anchored_dir))
    assert connector.in_process()
    monkeypatch.setattr(connector.httpx, "Client",
                        lambda *a, **k: pytest.fail("in-process mode must not open a transport"))
    loaded = connector.load_evidence("anchored.zip", "trust.json", sigstore_identity=IDENTITY,
                                     sigstore_trust_root_path="trusted_root.json",
                                     require_public_transparency=True)
    report = loaded["graph"]["bundle_report"]
    assert report["outcome"] == "OK" and f"/ {REKOR_INDEX} " in report["rekor_log"]
    stripped = connector.experiment_evidence(loaded["id"], {"kind": "strip_public_evidence"})
    assert stripped["graph"]["bundle_report"]["outcome"] == "NOT_INCLUDED"
    assert connector.remove_evidence(loaded["id"]) == {"removed": True}


def test_connector_uses_the_loopback_backend_when_one_is_named(monkeypatch):
    monkeypatch.setenv("VAT_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("VAT_BACKEND_MODE", raising=False)
    assert not connector.in_process()
    monkeypatch.setenv("VAT_BACKEND_MODE", "in-process")
    assert connector.in_process()


def test_in_process_connector_refuses_the_routes_that_own_the_durable_journal(monkeypatch):
    monkeypatch.delenv("VAT_BACKEND_URL", raising=False)
    monkeypatch.setenv("VAT_BACKEND_MODE", "in-process")
    with pytest.raises(RuntimeError) as excinfo:
        connector.workflow_status("00000000-0000-4000-8000-000000000001")
    assert "loopback backend" in str(excinfo.value)
    assert connector.call("GET", "/api/science/catalog")["version"] == "protein-tool-catalog/1"
