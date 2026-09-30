"""Render one verified evidence graph into a self-contained HTML page.

The page is the web viewer's read-only half (verdicts, graph, object inspection) with the
Python projection embedded as data. It fetches nothing, so it opens anywhere an ``.html`` file
opens, including Claude Science's sandboxed artifact preview, which can reach no backend. The
page verifies nothing itself: its verdicts are the verifier's at render time, and a reader who
wants fresh verification loads the bundle again.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from .graph import VerifyPolicy, project

TEMPLATE = Path(__file__).resolve().parents[2] / "web" / "dist-static" / "static.html"
BUILD_COMMAND = "npm --prefix web run build:static"
PLACEHOLDER = '<script id="vat-evidence" type="application/json">null</script>'
TITLE_TAG = "<title>VAT evidence</title>"
MAX_TITLE = 120


def load_template(path: Path = TEMPLATE) -> str:
    """Read the built viewer; it is a build product, so say how to make it when it is absent."""
    if not path.is_file():
        raise FileNotFoundError(f"The static evidence viewer is not built at {path}. Run: {BUILD_COMMAND}")
    text = path.read_text(encoding="utf-8")
    if text.count(PLACEHOLDER) != 1 or text.count(TITLE_TAG) != 1:
        raise ValueError(f"{path} is not the static evidence viewer template; rebuild it with: {BUILD_COMMAND}")
    return text


def embed_json(value: object) -> str:
    """Serialize for a JSON script block: nothing the HTML parser could read as a tag end."""
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render(graph: dict, title: str = "VAT evidence", template: str | None = None) -> str:
    """Embed one projection in the viewer template."""
    if not isinstance(graph, dict) or "bundle_report" not in graph or "nodes" not in graph:
        raise ValueError("Expected a verification graph from runner.graph.project")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= MAX_TITLE:
        raise ValueError(f"Title must be 1 to {MAX_TITLE} characters")
    page = template if template is not None else load_template()
    return (page.replace(TITLE_TAG, f"<title>{html.escape(title.strip())}</title>", 1)
            .replace(PLACEHOLDER, f'<script id="vat-evidence" type="application/json">{embed_json(graph)}</script>', 1))


def render_bundle(archive: Path, trust: Path, sequence: str | None = None,
                  policy: VerifyPolicy | None = None, title: str = "VAT evidence") -> str:
    """Verify a bundle under supplied public trust and render the result."""
    return render(project(Bundle.load(archive), TrustBundle.load(trust), sequence, policy or VerifyPolicy()), title)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m runner.evidence_html",
                                     description="Verify a VAT bundle offline and write a self-contained HTML evidence page.")
    parser.add_argument("archive", type=Path, help="bundle ZIP")
    parser.add_argument("trust", type=Path, help="public trust JSON supplied by the verifier")
    parser.add_argument("--out", type=Path, required=True, help="HTML file to write")
    parser.add_argument("--title", default="VAT evidence")
    parser.add_argument("--sequence", help="designed sequence to check against the disclosed records")
    parser.add_argument("--sigstore-identity", help="expected signing identity for public Sigstore evidence")
    parser.add_argument("--sigstore-issuer")
    parser.add_argument("--sigstore-trusted-root", type=Path, help="official trusted root JSON obtained independently")
    parser.add_argument("--require-public-transparency", action="store_true")
    args = parser.parse_args(argv)
    root = None
    if args.sigstore_identity or args.require_public_transparency or args.sigstore_trusted_root:
        from vatlog.sigstore_transparency import load_trusted_root, parse_trusted_root
        root = (parse_trusted_root(args.sigstore_trusted_root.read_bytes()) if args.sigstore_trusted_root
                else load_trusted_root(None, offline=True))
    policy = VerifyPolicy(require_public_transparency=args.require_public_transparency, sigstore_identity=args.sigstore_identity,
                          sigstore_issuer=args.sigstore_issuer, sigstore_trusted_root=root)
    page = render_bundle(args.archive, args.trust, args.sequence, policy, args.title)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({len(page.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
