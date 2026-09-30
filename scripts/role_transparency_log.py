"""Append a notarized bundle using only the local log operator's private key."""

import sys
from vatlog.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["log-append", *sys.argv[1:]]))
