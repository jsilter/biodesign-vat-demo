"""Run the verifier with public trust configuration and no private key."""

import sys

from vatlog.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["verify", *sys.argv[1:]]))
