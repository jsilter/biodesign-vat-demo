"""Read-only evidence projection. Relationships come from claims, never tool names."""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature

from vatlog.bundle import Bundle
from vatlog.canon import canonical_record_bytes, parse_json, sha256_hex, record_digest
from vatlog.merkle import check_inclusion
from vatlog.node import VatLimits, VatNode, verify_node, verify_node_signature
from vatlog.notary import verify_recorder_commitment
from vatlog.provenance import bundle_provenance
from vatlog.prov_export import entity_id
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle


@dataclass(frozen=True)
class VerifyPolicy:
    """Verifier-side expectations that never come from the bundle under inspection.

    ``sigstore_trusted_root`` is a parsed official ``TrustedRoot`` obtained independently of
    the evidence (a verifier-supplied file or the client's production TUF cache).
    """

    require_public_transparency: bool = False
    sigstore_identity: str | None = None
    sigstore_issuer: str | None = None
    sigstore_trusted_root: Any = None

    def describe(self) -> dict:
        return {"require_public_transparency": self.require_public_transparency,
                "sigstore_identity": self.sigstore_identity, "sigstore_issuer": self.sigstore_issuer,
                "sigstore_trusted_root": "verifier-supplied" if self.sigstore_trusted_root is not None else "absent"}


def project(bundle: Bundle, trust: TrustBundle, sequence: str | None = None,
            policy: VerifyPolicy | None = None) -> dict:
    """Return bounded disclosed claims and independently computed verification states."""
    if len(bundle.records) > 10_000:
        raise ValueError("Graph exceeds 10,000 records")
    policy = policy or VerifyPolicy()
    report = verify_bundle(bundle, trust, submitted_sequence=sequence,
                           require_public_transparency=policy.require_public_transparency,
                           sigstore_trusted_root=policy.sigstore_trusted_root,
                           sigstore_identity=policy.sigstore_identity, sigstore_issuer=policy.sigstore_issuer)
    provenance, _ = bundle_provenance(bundle, trust)
    head = bundle.signed_tree_head
    try:
        verify_recorder_commitment(bundle, trust)
        commitment = "passed"
    except (InvalidSignature, ValueError, TypeError):
        commitment = "failed"
    nodes, edges, execution_ids, statements = {}, [], {}, {}
    occupied = set()
    positions = Counter(r.link.leaf_index for r in bundle.records)
    proofs = {}
    for proof in bundle.inclusion_proofs:
        index = proof.get("leaf_index")
        if type(index) is int:
            proofs.setdefault(index, []).append(proof)
    memberships, records, record_edges = {}, [], []
    previous = None
    for record in sorted(bundle.records, key=lambda r: r.link.leaf_index):
        index = record.link.leaf_index
        matches = proofs.get(index, [])
        membership = "unchecked" if commitment != "passed" else "passed" if (
            positions[index] == 1 and len(matches) == 1 and check_inclusion(canonical_record_bytes(record), head.root_hash,
                                                 matches[0], tree_size=head.tree_size)) else "failed"
        memberships[index] = membership
        if previous is not None:
            record_edges.append({"id": f"order:{index}", "source": f"record:{previous.link.leaf_index}",
                                 "target": f"record:{index}", "kind": "order", "label": "disclosed order"})
            if index == previous.link.leaf_index + 1:
                record_edges.append({"id": f"chain:{index}", "source": f"record:{previous.link.leaf_index}",
                                     "target": f"record:{index}", "kind": "chain",
                                     "label": "hash link matches" if record.link.sha_prev == record_digest(previous) else "hash link FAILED"})
        previous = record
        records.append({"index": index, "type": record.record_type.value, "membership": membership,
                        "params": record.params.model_dump(mode="json"),
                        "artifacts": [a.model_dump(mode="json") for a in [*record.inputs, *record.intermediates, *record.results]]})
        for artifact in record.intermediates:
            if artifact.kind != "vat_node" or artifact.sha256 not in bundle.blobs:
                continue
            raw = bundle.blobs[artifact.sha256]
            if len(raw) > VatLimits().max_node_bytes:
                raise ValueError("Embedded execution exceeds node limit")
            try:
                node = VatNode.model_validate(parse_json(raw))
                statement = node.statement()
            except (ValueError, TypeError):
                continue  # The record and verifier failure remain visible.
            identifier = node.node_id
            occupied.add(index)
            if identifier in nodes:
                nodes[identifier]["leaves"].append(index)
                continue
            statements[identifier] = statement
            execution_ids.setdefault(str(statement.execution_id), []).append(identifier)
            nodes[identifier] = {
                "id": identifier, "kind": "execution", "label": statement.function.function_name,
                "status": statement.status.value, "transport": statement.transport,
                "signature": "passed" if verify_node_signature(node, trust).passed else "failed",
                "integrity": "passed" if verify_node(node, trust).passed else "failed",
                "embedding": "passed" if sha256_hex(raw) == artifact.sha256 and len(raw) == artifact.bytes_len else "failed",
                "leaves": [index], "statement": statement.model_dump(mode="json"),
                "signers": [s.keyid for s in node.signatures],
            }
    references = sum(len(s.input_bindings) + len(s.control_references) for s in statements.values())
    if len(statements) > 10_000 or references > 100_000:
        raise ValueError("Graph exceeds execution/reference limits")
    for identifier, statement in statements.items():
        for position, binding in enumerate(statement.input_bindings):
            producer = binding.producer_node_id
            if producer not in nodes:
                nodes[producer] = {"id": producer, "kind": "missing", "label": "Undisclosed producer", "leaves": []}
            entity = entity_id(producer, binding.selector)
            if entity not in nodes:
                nodes[entity] = {"id": entity, "kind": "output", "label": binding.selector.name or binding.selector.pointer or "Return value",
                                 "leaves": [], "selector": binding.selector.model_dump(mode="json"), "producer": producer}
                disclosed = producer in statements
                edges.append({"id": "generation:" + entity, "source": producer, "target": entity,
                              "kind": "generation" if disclosed else "reference",
                              "label": "produced" if disclosed else "claimed source"})
            edges.append({"id": f"data:{identifier}:{position}", "source": entity, "target": identifier,
                          "kind": "data", "label": binding.destination})
        for position, control in enumerate(statement.control_references):
            producer = control.producer_node_id
            nodes.setdefault(producer, {"id": producer, "kind": "missing", "label": "Undisclosed requester", "leaves": []})
            edges.append({"id": f"control:{identifier}:{position}", "source": producer, "target": identifier,
                          "kind": "control", "label": control.call_id or "requested"})
    for record in bundle.records:
        params = record.params.hyperparams
        execution = params.get("execution_id") if params.get("capture_kind") == "execution_lifecycle" else params.get("vat_execution_id")
        identifiers = execution_ids.get(execution, []) if isinstance(execution, str) else []
        identifier = identifiers[0] if len(identifiers) == 1 else None
        if params.get("capture_kind") == "execution_lifecycle":
            identifier = identifier or "pending:" + str(execution)
            nodes.setdefault(identifier, {"id": identifier, "kind": "pending", "label": "Unresolved execution", "leaves": []})
            if record.link.leaf_index not in nodes[identifier]["leaves"]:
                nodes[identifier]["leaves"].append(record.link.leaf_index)
        elif record.link.leaf_index not in occupied:
            identifier = "leaf:" + str(record.link.leaf_index)
            nodes[identifier] = {"id": identifier, "kind": "record", "label": record.record_type.value.replace("_", " "),
                                 "leaves": [record.link.leaf_index], "provenance": "not recorded"}
    if len(nodes) > 100_000 or len(edges) > 100_000:
        raise ValueError("Graph projection exceeds limits")
    for node in nodes.values():
        node["memberships"] = [{"leaf": i, "status": memberships[i]} for i in sorted(node["leaves"])]
    present = set(bundle.revealed_indices())
    missing_count = max(0, head.tree_size - len({i for i in present if 0 <= i < head.tree_size}))
    gaps = []
    # Walk only disclosed positions and the first 100 gaps, even for huge claimed trees.
    start = 0
    for index in sorted(i for i in present if 0 <= i < head.tree_size) + [head.tree_size]:
        gaps.extend(range(start, min(index, start + 100 - len(gaps))))
        start = index + 1
        if len(gaps) == 100:
            break
    return {"session": bundle.session_uuid, "root": head.root_hash, "tree_size": head.tree_size,
            "commitment": commitment, "trust": "Operator-supplied public keys",
            "bundle_report": asdict(report), "provenance_report": asdict(provenance),
            "verify_policy": policy.describe(),
            "public_evidence": bundle.sigstore_evidence.summary() if bundle.sigstore_evidence else None,
            "nodes": list(nodes.values()), "edges": edges, "records": records, "record_edges": record_edges,
            "gaps": gaps, "missing_count": missing_count,
            "head": head.model_dump(mode="json"), "attestation": bundle.attestation.model_dump(mode="json")}


def falsify(bundle: Bundle, action: dict) -> tuple[Bundle, str]:
    """Change one copied disclosure without access to signing or execution services."""
    from demo.disclosure import withhold_records
    changed = bundle.model_copy(deep=True)
    kind = action.get("kind")
    if kind == "narrative":
        text = action.get("value")
        if not isinstance(text, str) or len(text) > 10_000:
            raise ValueError("Narrative must be text up to 10,000 characters")
        return changed, text
    if kind in PUBLIC_EVIDENCE_ACTIONS:
        return _falsify_public_evidence(changed, kind, action)
    index = action.get("leaf")
    if type(index) is not int:
        raise ValueError("Choose a disclosed leaf")
    record = next((r for r in changed.records if r.link.leaf_index == index), None)
    if record is None:
        raise ValueError("Leaf is not disclosed")
    if kind == "withhold":
        return withhold_records(bundle, {index}), f"Withheld leaf {index} and its proof"
    if kind == "parameter":
        record.params.hyperparams["falsification"] = action.get("value", "changed")
    elif kind == "artifact":
        digest = action.get("digest")
        if digest not in {a.sha256 for a in [*record.inputs, *record.results, *record.intermediates]} or digest not in changed.blobs:
            raise ValueError("Choose available artifact bytes on this leaf")
        changed.blobs[digest] += b"\nChanged in falsification view"
    elif kind in {"remove_binding", "redirect_binding", "result"}:
        artifact = next((a for a in record.intermediates if a.kind == "vat_node"), None)
        if artifact is None or artifact.sha256 not in changed.blobs:
            raise ValueError("Leaf has no available execution node")
        raw = parse_json(changed.blobs[artifact.sha256])
        node = VatNode.model_validate(raw)
        node.statement()
        if kind == "result":
            output = next((a for a in node.statement().results if a.name == "return.json"), None)
            if output is None:
                raise ValueError("Node has no return value")
            raw["blobs"][output.sha256] = base64.b64encode(b'{"changed":true}').decode()
        else:
            payload = parse_json(base64.b64decode(raw["payload"], validate=True))
            bindings = payload.get("input_bindings", [])
            if not bindings:
                raise ValueError("Node has no data binding")
            if kind == "remove_binding":
                bindings.pop(0)
            else:
                from vatlog.provenance import InputBinding
                binding = dict(bindings[0], producer_node_id=action.get("producer"))
                bindings[0] = InputBinding.model_validate(binding).model_dump(mode="json")
            from vatlog.canon import canonical_bytes
            raw["payload"] = base64.b64encode(canonical_bytes(payload)).decode()
        from vatlog.canon import canonical_bytes
        changed.blobs[artifact.sha256] = canonical_bytes(raw)
    else:
        raise ValueError("Unknown falsification action")
    return changed, f"Applied {kind} to leaf {index}; original signatures and commitments retained"


PUBLIC_EVIDENCE_ACTIONS = frozenset({"strip_public_evidence", "retarget_public_evidence", "substitute_public_identity"})


def _falsify_public_evidence(changed: Bundle, kind: str, action: dict) -> tuple[Bundle, str]:
    """Alter only the public Sigstore evidence object; records, receipt and signatures stay intact."""
    evidence = changed.sigstore_evidence
    if evidence is None:
        raise ValueError("Disclosed evidence carries no public transparency evidence")
    if kind == "strip_public_evidence":
        changed.sigstore_evidence = None
        return changed, "Removed the public Sigstore evidence; records, receipt and local signatures retained"
    if kind == "retarget_public_evidence":
        other = sha256_hex(b"a different receipt")
        changed.sigstore_evidence = evidence.model_copy(update={"receipt_digest": other})
        return changed, "Public evidence now names a different receipt digest; the Rekor entry, receipt and signatures retained"
    identity = action.get("value")
    if not isinstance(identity, str) or not 3 <= len(identity) <= 320 or "@" not in identity:
        raise ValueError("Choose a replacement signing identity email")
    changed.sigstore_evidence = evidence.model_copy(update={"identity": identity})
    return changed, f"Public evidence now claims signing identity {identity}; certificate and Rekor entry retained"
