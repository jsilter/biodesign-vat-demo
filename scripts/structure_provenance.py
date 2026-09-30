"""Record fresh local comparisons of saved structures; never rerun or relabel inference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

import gemmi

from demo.structure_comparison import compare_complex
from vatlog import VatRecorder, vat
from vatlog.provenance import bundle_provenance
from vatlog.roles import Keyring
from vatlog.session_export import publish_recorded
from vatlog.verify import verify_bundle


@vat
def import_structure(raw: bytes, filename: str) -> dict:
    """Record supplied file bytes; the caller's origin claim is not an inference receipt."""
    return {"bytes": raw, "source": filename, "boundary": "saved structure supplied by operator; prior inference provenance not established"}


@vat
def compare_structures(prediction: bytes, reference: bytes) -> dict:
    """Parse saved mmCIF and execute the established target-aligned comparison."""
    predicted = gemmi.make_structure_from_block(gemmi.cif.read_string(prediction.decode()).sole_block())
    reference_ensemble = gemmi.make_structure_from_block(gemmi.cif.read_string(reference.decode()).sole_block())
    return compare_complex(predicted, reference_ensemble)


@vat
def comparison_report(comparisons: list[dict]) -> dict:
    """Retain every supplied comparison, with metric and inference limitations explicit."""
    return {"comparisons": comparisons, "count": len(comparisons),
            "claim": "Local comparison of imported structures; not affinity, activity, or new inference"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--prediction', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    paths = [args.reference, *args.prediction]
    for path in paths:
        if not path.is_file() or path.stat().st_size > 8 * 1024**2:
            parser.error(f"Supply an existing mmCIF file up to 8 MiB: {path}")
    args.out.mkdir(parents=True, exist_ok=False)
    keys = Keyring.generate()
    with VatRecorder(args.out / 'journal', keys.recorder) as recorder:
        imports = [import_structure.with_recorder(recorder).call(path.read_bytes(), path.name) for path in paths]
        comparisons = [compare_structures.with_recorder(recorder).call(pred.ref('/bytes'), imports[0].ref('/bytes'))
                       for pred in imports[1:]]
        succeeded = all(result.status.value == 'succeeded' for result in comparisons)
        if succeeded:
            result = comparison_report.with_recorder(recorder).call([item.ref() for item in comparisons])
            print(json.dumps(result.value, indent=2))
        bundle = publish_recorded(recorder, args.out, keys, str(uuid4()),
                                  prompt='Local comparison of operator-supplied saved structures; prior inference not recaptured')
    bundle.save(args.out / 'bundle.zip')
    keys.trust_bundle().save(args.out / 'trust.json')
    print(verify_bundle(bundle, keys.trust_bundle()).render())
    print('Provenance:', bundle_provenance(bundle, keys.trust_bundle())[0].outcome)
    return 0 if succeeded else 1


if __name__ == '__main__':
    raise SystemExit(main())
