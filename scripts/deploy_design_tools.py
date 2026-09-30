"""Deploy scientific images using root .env without putting credentials in arguments."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-deployment", action="store_true")
    args = parser.parse_args()
    if not args.confirm_deployment:
        parser.error("Deployment builds remote images; obtain release/spend approval and pass --confirm-deployment.")
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False, interpolate=False)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    return subprocess.run([sys.executable, "-m", "modal", "deploy", "-m", "runner.modal_app"],
                          cwd=root, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
