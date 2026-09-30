"""Recalculate the committed BBF-14 panel result without remote execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


EXPECTED_PANEL_SHA256 = "880ebce29826a9b0b52740bd86b91d69383cc5af878ee016272ed0de68c829fd"
EXPECTED_REVISION = "9e1b81696da46835e9e9cde9a3da976e0abc92ab"


def repository(path: Path) -> Path:
    """Return a validated biodesign-vat-demo repository root and expose its source tree."""
    root = path.resolve()
    marker = root / "pyproject.toml"
    if not marker.is_file() or 'name = "biodesign-vat-demo"' not in marker.read_text(encoding="utf-8"):
        raise ValueError("--repo must identify the biodesign-vat-demo repository root")
    sys.path.insert(0, str(root / "src"))
    return root


def analyze(path: Path) -> dict:
    """Validate the frozen panel and calculate its predeclared published-score analysis."""
    from demo.benchbb_analysis import describe_groups, exact_auc_permutation
    from demo.benchbb_panel import BenchBBPanel

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_PANEL_SHA256:
        raise ValueError(f"panel digest mismatch: expected {EXPECTED_PANEL_SHA256}, received {digest}")
    panel = BenchBBPanel.model_validate_json(raw)
    if panel.dataset_revision != EXPECTED_REVISION or panel.target != "BBF-14" or len(panel.designs) != 12:
        raise ValueError("panel does not match the frozen BBF-14 validation contract")
    labels = [design.binder_final for design in panel.designs]
    values = [design.ipsae_min_ef2fast for design in panel.designs]
    if any(value is None for value in values):
        raise ValueError("published score is absent for one or more designs")
    numeric = [float(value) for value in values]
    return {
        "schema": panel.schema_version,
        "panel_sha256": digest,
        "dataset_repository": panel.dataset_repository,
        "dataset_revision": panel.dataset_revision,
        "target": panel.target,
        "designs": len(panel.designs),
        "label_enriched": panel.label_enriched,
        "analysis": {
            "metric": "ipsae_min_ef2fast",
            "source": "published",
            "higher_is_better": True,
            **describe_groups(labels, numeric),
            **exact_auc_permutation(labels, numeric, higher_is_better=True),
        },
        "limits": [
            "This is a 12-design, label-enriched panel.",
            "The calculation uses published scores and does not rerun a model.",
            "The result does not establish prospective biological validity.",
        ],
    }


def main() -> int:
    """Run the frozen analysis and emit structured JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--panel", type=Path, default=Path("fixtures/benchbb_bbf14_panel.json"))
    args = parser.parse_args()
    try:
        root = repository(args.repo)
        panel = (root / args.panel).resolve() if not args.panel.is_absolute() else args.panel.resolve()
        if not panel.is_relative_to(root):
            raise ValueError("panel must remain under the repository")
        result = analyze(panel)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
