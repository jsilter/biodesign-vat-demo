"""Deterministic PROV-O projection; the original VAT bytes remain authoritative."""
from __future__ import annotations

from .canon import canonical_bytes, sha256_hex
from .provenance import OutputSelector


def entity_id(node_id: str, selector: OutputSelector) -> str:
    selected = sha256_hex(canonical_bytes(selector.model_dump(mode="json")))
    return f"urn:vat:output:{node_id}:{selected}"


def export_prov(nodes) -> dict:
    """Map disclosed statements to PROV activities/entities without inventing ancestry."""
    nodes = list(nodes)
    graph, selected = {}, {}
    for node in nodes:
        for binding in node.statement().input_bindings:
            selected.setdefault(binding.producer_node_id, []).append(binding.selector)
    for node in nodes:
        statement, identifier = node.statement(), node.node_id
        activity = "urn:vat:node:" + identifier
        signer = "urn:vat:signer:" + node.signatures[0].keyid
        graph[signer] = {"@id": signer, "@type": "prov:Agent"}
        graph[activity] = {"@id": activity, "@type": "prov:Activity",
                           "prov:label": statement.function.function_name,
                           "vat:recorder": {"@id": signer},
                           "vat:status": statement.status.value,
                           "prov:startedAtTime": {"@value": statement.timestamps.stage_start.isoformat(), "@type": "xsd:dateTime"},
                           "prov:endedAtTime": {"@value": statement.timestamps.stage_stop.isoformat(), "@type": "xsd:dateTime"},
                           "prov:used": [], "vat:requestedBy": []}
        for binding in statement.input_bindings:
            entity = entity_id(binding.producer_node_id, binding.selector)
            graph.setdefault(entity, {"@id": entity, "@type": "prov:Entity"})
            graph[activity]["prov:used"].append({"@id": entity})
        for control in statement.control_references:
            graph[activity]["vat:requestedBy"].append({"@id": "urn:vat:node:" + control.producer_node_id})
    # Generation claims are emitted only when the producer itself was supplied.
    for node in nodes:
        selectors = [OutputSelector()] if node.statement().status.value == "succeeded" else []
        selectors += selected.get(node.node_id, [])
        selectors += [OutputSelector(kind="artifact", name=a.name) for a in node.statement().results]
        for selector in selectors:
            entity = entity_id(node.node_id, selector)
            graph[entity] = {"@id": entity, "@type": "prov:Entity",
                             "prov:wasGeneratedBy": {"@id": "urn:vat:node:" + node.node_id}}
    return {"@context": {"prov": "http://www.w3.org/ns/prov#",
                         "xsd": "http://www.w3.org/2001/XMLSchema#",
                         "vat": "urn:vat:provenance:"},
            "@graph": [graph[key] for key in sorted(graph)]}
