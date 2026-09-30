"""An in-memory Sigstore stand-in: test Fulcio CA, CT log, Rekor v2 log and a local TSA.

Nothing here touches a network. The fake signs real X.509 certificates with an embedded SCT,
real RFC 6962 checkpoints and inclusion proofs, and real RFC 3161 tokens (minted with the
``openssl ts`` binary), so the official ``sigstore.verify.Verifier`` runs unmodified against a
trusted root built from these materials. Only the identity token is forged: the official
client never verifies token signatures itself, and no Fulcio is involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from demo.sessions import build_clean
from vatlog.bundle import Bundle, build_bundle
from vatlog.notary import issue_receipt
from vatlog.roles import Keyring
from vatlog.sigstore_transparency import DEFAULT_ISSUER, PublicationError
from vatlog.transparency import TransparencyLog

TRUSTED_ROOT_MEDIA_TYPE = "application/vnd.dev.sigstore.trustedroot+json;version=0.1"
OIDC_ISSUER_OID = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.1")
SCT_LIST_OID = x509.ObjectIdentifier("1.3.6.1.4.1.11129.2.4.2")
IDENTITY = "vat-rekor-demo@example-project.iam.gserviceaccount.com"

TSA_CONFIG = """
[ req ]
distinguished_name = dn
prompt = no
[ dn ]
CN = VAT offline test TSA
[ ca_ext ]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[ tsa_ext ]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,timeStamping
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
[ tsa_config ]
serial = serial.txt
crypto_device = builtin
signer_cert = tsa.crt
signer_key = tsa.key
signer_digest = sha256
default_policy = 1.3.6.1.4.1.57264.2
other_policies = 1.3.6.1.4.1.57264.2
digests = sha256
accuracy = secs:1
ordering = no
tsa_name = yes
ess_cert_id_chain = no
ess_cert_id_alg = sha256
"""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _spki(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def forged_identity_token(identity: str = IDENTITY, issuer: str = DEFAULT_ISSUER, *, lifetime: int = 600) -> object:
    """A token with the claims Sigstore reads; no real issuer is involved."""
    from sigstore.oidc import IdentityToken
    now = int(time.time())
    claims = {"iss": issuer, "aud": "sigstore", "sub": "1234567890", "email": identity, "email_verified": True,
              "iat": now, "exp": now + lifetime}
    return IdentityToken(jwt.encode(claims, "not-a-secret-just-a-test-fixture-key!", algorithm="HS256"))


# --- VAT side ----------------------------------------------------------------------

def notarized_bundle(root: Path, keyring: Keyring, *, observed_at: datetime | None = None,
                     local_log: bool = False) -> Bundle:
    session, *_ = build_clean(root, keyring=keyring)
    bundle = build_bundle(session)
    bundle.notary_receipt = issue_receipt(bundle, keyring.notary, keyring.trust_bundle(), observed_at=observed_at)
    if local_log:
        with TransparencyLog(root / "log.sqlite", keyring.trust_bundle(), signer=keyring.transparency_log,
                             log_id="fake-local") as log:
            bundle.transparency_evidence = log.append(bundle)
    return bundle


# --- Rekor -------------------------------------------------------------------------

def _leaf(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(count: int) -> int:
    k = 1
    while k * 2 < count:
        k *= 2
    return k


def _root(leaves: list[bytes]) -> bytes:
    if len(leaves) == 1:
        return _leaf(leaves[0])
    k = _split(len(leaves))
    return _node(_root(leaves[:k]), _root(leaves[k:]))


def _proof(leaves: list[bytes], index: int) -> list[bytes]:
    if len(leaves) == 1:
        return []
    k = _split(len(leaves))
    if index < k:
        return _proof(leaves[:k], index) + [_root(leaves[k:])]
    return _proof(leaves[k:], index - k) + [_root(leaves[:k])]


class FakeRekorLog:
    """An RFC 6962 log with Ed25519-signed checkpoints in Rekor's signed-note format."""

    def __init__(self, origin: str = "vat-fake-rekor - 20260920") -> None:
        self.origin = origin
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.spki = _spki(self.key)
        self.log_id = hashlib.sha256(self.spki).digest()
        self.leaves: list[bytes] = [b"pre-existing leaf"]

    def checkpoint(self) -> str:
        note = f"{self.origin}\n{len(self.leaves)}\n{_b64(_root(self.leaves))}\n"
        signature = self.key.sign(note.encode())
        return note + "\n" + f"— {self.origin.split(' - ')[0]} {_b64(self.log_id[:4] + signature)}\n"

    def append(self, body: bytes) -> dict:
        self.leaves.append(body)
        index = len(self.leaves) - 1
        return {
            "logIndex": str(index), "logId": {"keyId": _b64(self.log_id)},
            "kindVersion": {"kind": "hashedrekord", "version": "0.0.2"},
            "inclusionProof": {"logIndex": str(index), "rootHash": _b64(_root(self.leaves)),
                               "treeSize": str(len(self.leaves)),
                               "hashes": [_b64(h) for h in _proof(self.leaves, index)],
                               "checkpoint": {"envelope": self.checkpoint()}},
            "canonicalizedBody": _b64(body),
        }


# --- Fulcio and CT ------------------------------------------------------------------

class FakeFulcio:
    """A self-signed test CA issuing Sigstore-profile leaf certificates with a precert SCT."""

    def __init__(self) -> None:
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        self.ct_key = ec.generate_private_key(ec.SECP256R1())
        self.ct_log_id = hashlib.sha256(_spki(self.ct_key)).digest()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VAT fake Fulcio root")])
        now = datetime.now(timezone.utc)
        self.root = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                     .public_key(self.ca_key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                                  key_encipherment=False, data_encipherment=False, key_agreement=False,
                                                  key_cert_sign=True, crl_sign=True, encipher_only=False,
                                                  decipher_only=False), critical=True)
                     .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()), critical=False)
                     .sign(self.ca_key, hashes.SHA256()))

    def _builder(self, public_key, identity: str, issuer: str, not_before: datetime, serial: int) -> x509.CertificateBuilder:
        return (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(self.root.subject)
                .public_key(public_key).serial_number(serial)
                .not_valid_before(not_before).not_valid_after(not_before + timedelta(minutes=10))
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(identity)]), critical=True)
                .add_extension(x509.UnrecognizedExtension(OIDC_ISSUER_OID, issuer.encode()), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False))

    def issue(self, public_key, identity: str, issuer: str = DEFAULT_ISSUER, *,
              not_before: datetime | None = None) -> x509.Certificate:
        not_before = not_before or datetime.now(timezone.utc) - timedelta(minutes=1)
        # The precertificate TBS is what the CT log signs; the serial must match the final cert.
        serial = x509.random_serial_number()
        pre = self._builder(public_key, identity, issuer, not_before, serial).sign(self.ca_key, hashes.SHA256())
        tbs = pre.tbs_certificate_bytes
        timestamp_ms = int(time.time() * 1000)
        issuer_key_id = hashlib.sha256(_spki(self.ca_key)).digest()
        signed = struct.pack(f"!BBQH32sBBB{len(tbs)}sH", 0, 0, timestamp_ms, 1, issuer_key_id,
                             *struct.pack("!I", len(tbs))[1:], tbs, 0)
        signature = self.ct_key.sign(signed, ec.ECDSA(hashes.SHA256()))
        sct = struct.pack("!B32sQH", 0, self.ct_log_id, timestamp_ms, 0) + struct.pack("!BBH", 4, 3, len(signature)) + signature
        sct_list = struct.pack("!H", len(sct) + 2) + struct.pack("!H", len(sct)) + sct
        der = bytes([0x04, len(sct_list)]) + sct_list if len(sct_list) < 128 else \
            bytes([0x04, 0x82]) + struct.pack("!H", len(sct_list)) + sct_list
        final = (self._builder(public_key, identity, issuer, not_before, serial)
                 .add_extension(x509.UnrecognizedExtension(SCT_LIST_OID, der), critical=False)
                 .sign(self.ca_key, hashes.SHA256()))
        assert final.tbs_precertificate_bytes == tbs
        return final


# --- TSA ---------------------------------------------------------------------------

class TestTsa:
    """A throwaway timestamp authority; keys live only in ``work`` for the test session."""

    __test__ = False

    def __init__(self, work: Path) -> None:
        self.work = work
        work.mkdir(parents=True, exist_ok=True)
        (work / "tsa.cnf").write_text(TSA_CONFIG)
        (work / "serial.txt").write_text("01\n")
        self._openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key")
        self._openssl("req", "-new", "-x509", "-key", "ca.key", "-subj", "/CN=VAT offline test TSA root", "-days", "30",
                      "-config", "tsa.cnf", "-extensions", "ca_ext", "-out", "ca.crt")
        self._openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "tsa.key")
        self._openssl("req", "-new", "-key", "tsa.key", "-subj", "/CN=VAT offline test TSA", "-config", "tsa.cnf",
                      "-out", "tsa.csr")
        self._openssl("x509", "-req", "-in", "tsa.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
                      "-days", "30", "-extfile", "tsa.cnf", "-extensions", "tsa_ext", "-out", "tsa.crt")
        self.chain = [x509.load_pem_x509_certificate((work / name).read_bytes()).public_bytes(serialization.Encoding.DER)
                      for name in ("tsa.crt", "ca.crt")]
        self.counter = 0

    def _openssl(self, *args: str) -> None:
        subprocess.run(["openssl", *args], cwd=self.work, check=True, capture_output=True)

    def timestamp(self, message: bytes) -> bytes:
        self.counter += 1
        name = f"req{self.counter}"
        (self.work / f"{name}.bin").write_bytes(message)
        self._openssl("ts", "-query", "-data", f"{name}.bin", "-sha256", "-cert", "-out", f"{name}.tsq")
        self._openssl("ts", "-reply", "-config", "tsa.cnf", "-section", "tsa_config", "-queryfile", f"{name}.tsq",
                      "-out", f"{name}.tsr")
        return (self.work / f"{name}.tsr").read_bytes()


# --- the fake stack ---------------------------------------------------------------------

class FakeSigstore:
    """Implements ``SigstoreServices.sign_digest`` the way the official ``Signer`` does."""

    def __init__(self, tsa: TestTsa, *, rekor_failure: BaseException | None = None,
                 fulcio_failure: BaseException | None = None, omit_timestamp: bool = False) -> None:
        self.tsa, self.rekor, self.fulcio = tsa, FakeRekorLog(), FakeFulcio()
        self.rekor_failure, self.fulcio_failure, self.omit_timestamp = rekor_failure, fulcio_failure, omit_timestamp
        self.requests: list[dict] = []
        self.timestamp_requests: list[bytes] = []
        self.tokens: list[object] = []

    def trusted_root_json(self, *, tsa_chain: list[bytes] | None = None, fulcio_root: bytes | None = None,
                          ct_spki: bytes | None = None, rekor_spki: bytes | None = None) -> str:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        rekor_key = rekor_spki or self.rekor.spki
        ct_key = ct_spki or _spki(self.fulcio.ct_key)
        return json.dumps({
            "mediaType": TRUSTED_ROOT_MEDIA_TYPE,
            "tlogs": [{"baseUrl": "https://rekor.fake.invalid", "hashAlgorithm": "SHA2_256",
                       "publicKey": {"rawBytes": _b64(rekor_key), "keyDetails": "PKIX_ED25519", "validFor": {"start": start}},
                       "logId": {"keyId": _b64(hashlib.sha256(rekor_key).digest())}}],
            "certificateAuthorities": [{"subject": {"organization": "VAT tests", "commonName": "VAT fake Fulcio root"},
                                        "uri": "https://fulcio.fake.invalid",
                                        "certChain": {"certificates": [{"rawBytes": _b64(fulcio_root or self.fulcio.root.public_bytes(serialization.Encoding.DER))}]},
                                        "validFor": {"start": start}}],
            "ctlogs": [{"baseUrl": "https://ctfe.fake.invalid", "hashAlgorithm": "SHA2_256",
                        "publicKey": {"rawBytes": _b64(ct_key), "keyDetails": "PKIX_ECDSA_P256_SHA_256", "validFor": {"start": start}},
                        "logId": {"keyId": _b64(hashlib.sha256(ct_key).digest())}}],
            "timestampAuthorities": [{"subject": {"organization": "VAT tests", "commonName": "VAT offline test TSA"},
                                      "uri": "https://tsa.fake.invalid",
                                      "certChain": {"certificates": [{"rawBytes": _b64(der)} for der in (tsa_chain or self.tsa.chain)]},
                                      "validFor": {"start": start}}],
        })

    def trusted_root(self, **overrides):
        from sigstore.models import TrustedRoot
        from sigstore_models.trustroot import v1 as trustroot_v1
        return TrustedRoot(trustroot_v1.TrustedRoot.from_json(self.trusted_root_json(**overrides)))

    def sign_digest(self, hashed, identity_token) -> dict:
        from sigstore._internal.rekor.client_v2 import RekorV2Client
        from sigstore.models import Bundle as SigstoreBundle, TransparencyLogEntry
        from sigstore_models.common.v1 import HashAlgorithm, HashOutput, MessageSignature
        from sigstore_models.rekor.v1 import TransparencyLogEntry as _Entry
        from sigstore_models.rekor.v2 import entry as v2_entry, hashedrekord as v2_hashedrekord
        from rfc3161_client import decode_timestamp_response
        self.tokens.append(identity_token)
        if self.fulcio_failure is not None:
            raise self.fulcio_failure
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = self.fulcio.issue(key.public_key(), identity_token.identity, identity_token.issuer)
        signature = key.sign(hashed.digest, ec.ECDSA(hashed._as_prehashed()))
        request = RekorV2Client._build_hashed_rekord_request(hashed, signature, certificate)
        self.requests.append(json.loads(json.dumps(request)))
        self.timestamp_requests.append(signature)
        timestamps = [] if self.omit_timestamp else [decode_timestamp_response(self.tsa.timestamp(signature))]
        if self.rekor_failure is not None:
            raise self.rekor_failure
        proposed = v2_entry.CreateEntryRequest.from_json(json.dumps(request)).hashed_rekord_request_v002
        body = v2_entry.Entry(kind="hashedrekord", api_version="0.0.2", spec=v2_entry.Spec(
            hashed_rekord_v002=v2_hashedrekord.HashedRekordLogEntryV002(
                data=HashOutput(algorithm=HashAlgorithm.SHA2_256, digest=_b64(hashed.digest)),
                signature=proposed.signature))).to_json().encode()
        entry = TransparencyLogEntry(_Entry.from_json(json.dumps(self.rekor.append(body))))
        content = MessageSignature(message_digest=HashOutput(algorithm=HashAlgorithm.SHA2_256, digest=_b64(hashed.digest)),
                                   signature=_b64(signature))
        return json.loads(SigstoreBundle._from_parts(certificate, content, entry, timestamps).to_json())


def other_tsa_chain() -> list[bytes]:
    """A fresh, unrelated CA and signer for TSA-chain rejection tests."""
    now = datetime.now(timezone.utc)
    ca_key, tsa_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other root")])
    tsa_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other tsa")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    tsa = (x509.CertificateBuilder().subject_name(tsa_name).issuer_name(ca_name).public_key(tsa_key.public_key())
           .serial_number(2).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
           .sign(ca_key, hashes.SHA256()))
    return [tsa.public_bytes(serialization.Encoding.DER), ca.public_bytes(serialization.Encoding.DER)]
