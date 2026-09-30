"""Record a demo session using only the recorder private key and a signed statement."""

import argparse
from pathlib import Path

from demo.sessions import build_session, THERAPEUTIC_OBJECTIVES
from vatlog.bundle import build_bundle
from vatlog.keys import load_private
from vatlog.schema import DesignerStatement


def main() -> None:
    """Consume the designer's public statement and export a recorder-signed bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recorder-key", type=Path, required=True)
    parser.add_argument("--statement", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    statement = DesignerStatement.model_validate_json(args.statement.read_bytes())
    log, *_ = build_session(
        args.root, session_uuid=statement.session_uuid, n_runs=3,
        purpose_tag=statement.purpose_tag, objectives=THERAPEUTIC_OBJECTIVES,
        key=load_private(args.recorder_key), designer_statement=statement,
    )
    build_bundle(log).save(args.out)


if __name__ == "__main__":
    main()
