"""Export real, locally signed fixtures for the read-only React demo.

Run before starting or building the UI. No private keys or undisclosed leaves are
exported. The JSON report is presentation data, not a signed verification claim;
the downloadable ZIP, public trust and sequence can be verified independently.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from demo.demo import prepare_fixture
from vatlog.bundle import Bundle
from vatlog.canon import sha256_hex
from vatlog.roles import TrustBundle, Keyring
from vatlog.transparency import TransparencyLog
from vatlog.verify import verify_bundle

FIXTURES = ("clean", "tampered", "doctored", "partial")
EXPECTED_CODES = dict(zip(FIXTURES, (0, 2, 0, 6)))
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024
MAX_LEAVES = 100
PREVIEW_KINDS = {"prompt", "transcript", "reasoning", "aa_sequence", "nt_sequence"}


def artifact_views(bundle: Bundle) -> dict[str, dict[str, bool | str | None]]:
    """Describe only disclosed references; availability is separate from text previewability."""
    artifacts: dict[str, dict[str, bool | str | None]] = {}
    for record in bundle.records:
        for ref in record.inputs + record.results + record.intermediates:
            data = bundle.blobs.get(ref.sha256)
            view = artifacts.setdefault(ref.sha256, {"available": data is not None, "text": None})
            if data is None or ref.kind not in PREVIEW_KINDS or len(data) > MAX_PREVIEW_BYTES:
                continue
            try:
                view["text"] = data.decode("utf-8")
            except UnicodeDecodeError:
                pass  # Bytes remain available in the ZIP, even without a text preview.
    return artifacts


def _prepared_fixtures():
    with tempfile.TemporaryDirectory(prefix="vat-ui-log-") as shared:
        keys = Keyring.generate()
        with TransparencyLog(Path(shared) / "transparency.sqlite", keys.trust_bundle(),
                             signer=keys.transparency_log, log_id="fixture-batch") as transparency:
            for fixture in FIXTURES:
                yield fixture, prepare_fixture(fixture, Path(shared) / fixture,
                                               keyring=keys, transparency=transparency)


def export_ui(destination: Path) -> dict[str, Any]:
    """Regenerate a stopped demo's public evidence; failure leaves no usable catalog.

    Only the generated catalog is invalidated. A full successful rerun recovers partially
    replaced downloads; concurrent generation and live serving are not supported.
    """
    destination.mkdir(parents=True, exist_ok=True)
    catalog_path = destination / "catalog.json"
    catalog_path.unlink(missing_ok=True)
    scenarios = []
    for fixture, (bundle, trust, sequence) in _prepared_fixtures():
        archive = destination / f"{fixture}.zip"
        if bundle.signed_tree_head.tree_size > MAX_LEAVES:
            raise ValueError("demo leaf limit exceeded")
        bundle.save(archive)
        trust.save(destination / f"{fixture}.trust.json")
        (destination / f"{fixture}.sequence.txt").write_text(sequence, encoding="utf-8")
        # Verify the exact download, not just its in-memory precursor.
        loaded = Bundle.load(archive)
        trust = TrustBundle.load(destination / f"{fixture}.trust.json")
        sequence = (destination / f"{fixture}.sequence.txt").read_text(encoding="utf-8")
        report = verify_bundle(loaded, trust, submitted_sequence=sequence, require_transparency=True)
        if report.exit_code != EXPECTED_CODES[fixture]:
            raise ValueError(f"unexpected {fixture} outcome: {report.outcome.value}")
        scenarios.append({
            "id": fixture,
            "bundle": loaded.model_dump(mode="json", exclude={"blobs"}),
            "trust": trust.model_dump(mode="json"),
            "sequence": sequence,
            "artifacts": artifact_views(loaded),
            "report": {**asdict(report), "exit_code": report.exit_code},
            "report_text": report.render(),
            "archive_sha256": sha256_hex(archive.read_bytes()),
        })
    catalog = {
        "format": "vat-ui-demo/1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": scenarios,
    }
    encoded = json.dumps(catalog, indent=2, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_CATALOG_BYTES:
        raise ValueError("demo catalog byte limit exceeded")
    with tempfile.NamedTemporaryFile(dir=destination, prefix=".catalog-", delete=False) as pending:
        pending_path = Path(pending.name)
        try:
            pending.write(encoded)
            pending.close()
            pending_path.replace(catalog_path)
        finally:
            pending_path.unlink(missing_ok=True)
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("web/public/demo"))
    args = parser.parse_args()
    export_ui(args.out)
    print(f"Exported {len(FIXTURES)} locally verified demo scenarios to {args.out}")


if __name__ == "__main__":
    main()
