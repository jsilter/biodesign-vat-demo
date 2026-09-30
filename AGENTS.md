# Repository Guidelines

`VISION.md` governs direction, status, and assurance limits. `CLAUDE.md` links here; edit
`AGENTS.md` directly. This is a local security demo, not a safety screen.

## Project Structure & Module Organization

- `src/vatlog/`: canonical records, signatures, Merkle proofs, provenance, durable capture,
  bundles, verification, and the local transparency log (`transparency.py`).
- `src/runner/`: research integrations, scientific adapters, workflow recovery, and evidence
  APIs. Remote dependencies are locked in `src/runner/locks/`.
- `src/demo/`: verification fixtures and BenchBB.
- `web/`: React/TypeScript UI; frontend tests live beside components.
- `tests/`: Python tests mirroring source. `scripts/`, `fixtures/`, `results/`, and `docs/`:
  utilities, inputs, measurements, and presentation materials. Research lives in `reference/`.
- `intent/YYYY-MM-DD-slug/`: active plans, pending validation gates, contracts, and results.

Presentations address external audiences unfamiliar with the repository. Keep file paths,
internal feature names and project history in the presenter guide, not audience slides.

Consult `VISION.md` for adapter validation; offline tests
do not establish successful GPU execution.

## Build, Test, and Development Commands

Use Python 3.11+ and Node 24:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev,workflow,serve]'
npm --prefix web ci
.venv/bin/python scripts/dev.py       # API :8000, UI :5173
.venv/bin/python -m pytest -q         # Python suite
npm --prefix web test                 # Vitest suite
npm --prefix web run build            # type-check and production build
npm --prefix web run build:static     # one-file evidence page template for runner.evidence_html
git diff --check
```

Run all checks before handoff; frontend tests/builds sequentially regenerate fixtures.
CLI commands need `PYTHONPATH=src`; pytest configures it. Browser-check UI changes without paid execution.

`run-claude-science.sh` starts the backend and evidence UI (`scripts/dev.py`) and the Claude
Science daemon together. That daemon sandboxes the MCP connector: it binds the repository and
nothing else holding a Python, gives it no network route to port 8000, and applies a seccomp
filter that blocks AF_UNIX sockets. So
the connector runs from `.claude-science-venv` inside the repository, needs the `workflow` and
`public-transparency` extras there, and serves the evidence and demo routes in its own process.
A change to those routes reaches the connector with no backend restart. Verify connector work
by driving it over stdio under that filter, not from an unconfined shell, which can reach
transports the connector cannot. Claude Science shows an `.html` artifact in an iframe with
scripts but no network, so `runner/evidence_html.py` embeds the projection in the built
`web/dist-static/static.html` template; that page shows the verifier's verdicts and verifies nothing.

## Coding Style & Testing

Use four-space Python indentation, typed public APIs, `snake_case` functions/modules,
`PascalCase` classes, and uppercase constants. Match neighboring React style. Keep I/O at
boundaries, validate early, and never mask errors. No formatter/linter is configured.

Name tests `test_<behavior>`. Cover integrity rejection and cancellation/recovery races.
Assert parsed bytes, real bundles, and independently calculated scores. No coverage threshold
is configured; report skips honestly and never weaken tests.

## Commits, Reviews, and Security

Follow approved `intent/` stages; preserve user changes and use separate worktrees for concurrent work.
Use imperative commit subjects, one concern each. Pull requests link `intent/`, state the changed
guarantee and exact validation results, and include UI screenshots. Review security/correctness first.

## Transparency and Assurance

The loopback backend stays the only owner of the durable journal, the recorder keys and Modal
dispatch; the in-process connector refuses those routes rather than becoming a second owner.

`runner/publication.py` persists signed bytes before append; recovery never reruns science.
Preserve journal, log database, and four-role keys; never silently replace missing state.
Notarized verification requires transparency evidence; use
`--require-transparency` to reject stripping both receipt and evidence. Retain observer checkpoints
to detect inconsistent later views. Exercise the offline observer flow with
`PYTHONPATH=src .venv/bin/python scripts/transparency_demo.py`.

Never commit secrets, generated logs, bundles, model weights, or `.env`. No anchored
Sigstore bundle is committed: a Fulcio certificate embeds the signing identity, so produce your own
with `scripts/sigstore_demo.py` (each run creates a permanent Rekor entry). Paid Modal runs need
explicit owner authorization. Preserve sealed history and canonical bytes. Local signatures
prove integrity relative to local keys—not independent execution, timing, safety, or absence of
off-ledger work.
