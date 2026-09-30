"""Run the simulated notary in a process that loads only its own private key."""

import sys

from vatlog.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["notarize", *sys.argv[1:]]))
