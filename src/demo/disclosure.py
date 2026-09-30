"""Derive demonstrably partial evidence without changing the original commitment."""
from vatlog.bundle import Bundle


def withhold_records(bundle: Bundle, indices: set[int]) -> Bundle:
    """Remove selected records, matching proofs and blobs used only by those records."""
    if not indices or 0 in indices or not indices <= set(bundle.revealed_indices()):
        raise ValueError("Choose existing non-opening record positions to withhold")
    if len(bundle.records) != len(bundle.inclusion_proofs):
        raise ValueError("Records and proofs must correspond")
    partial = bundle.model_copy(deep=True)
    pairs = [(record, proof) for record, proof in zip(partial.records, partial.inclusion_proofs)
             if record.link.leaf_index not in indices]
    partial.records = [record for record, _ in pairs]
    partial.inclusion_proofs = [proof for _, proof in pairs]
    referenced = {a.sha256 for r in partial.records for a in [*r.inputs, *r.results, *r.intermediates]}
    partial.blobs = {digest: raw for digest, raw in partial.blobs.items() if digest in referenced}
    return partial
