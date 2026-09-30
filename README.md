# biodesign-vat-demo

> **Hackathon project.** This was built during a hackathon and is very rough. It is not
> maintained or updated, and issues and pull requests will not be answered. Released under the
> [MIT License](LICENSE).

Presentation: [slides](https://claude.ai/artifact/WE4AfaHSrr834PVJ3Q1LPK) · [interactive slides and narration](docs/project-demo-slides.html) · [presenter guide](docs/project-demo-storyboard.md).

A reference implementation of the VAT (verifiable audit trail) protocol for AI-enabled
biological design, with a recorder intended to run on Modal instead of on the designer's
machine.

Built for the Boston Computational Biology Hackathon (2026-09-22; Anthropic / Modal / Flagship
Pioneering). The Basic Modal VAT decorator is offline-tested; of the scientific adapters, only
ESMFold2-Fast and ESMC have run on Modal GPUs. See [`VISION.md`](VISION.md) for assurance
boundaries and current state.

## The gap

A synthesis provider or regulator receiving a designed sequence cannot tell what objective it
was optimized for. Sequence screening (SecureDNA, the IBBIS Common Mechanism) answers "is this
sequence hazardous." It does not answer "how was this produced, and does the designer's stated
purpose match the objective function they actually ran." [RAND PE-A4613-1](https://www.rand.org/pubs/perspectives/PEA4613-1.html) (June 2026) specifies
a protocol for the second question and calls for a reference implementation. This is that
implementation, for one deployment substrate.

The spec extract is in [`reference/rand-vat-spec-extract.md`](reference/rand-vat-spec-extract.md). Read it
first. RAND's terms forbid reposting the publication, so the source is linked, not copied; see
[References](#references).

## The claim being tested

RAND grades assurance by how the recorder captures tool output, and its weakest tier (recorder
wraps the tool as a subprocess) is weak for a specific reason: a designer with root on their own
machine can substitute outputs after the run but before the log is finalized. Its strongest tier
(recorder and tool co-executing in a trusted execution environment) is, in the paper's words,
not currently feasible for most biodesign software.

Serverless execution is not on that ladder. On Modal the designer submits code but does not hold
root on the host; the container image digest and hardware are attested by the platform rather
than asserted by the user; and the recorder can run outside the designer's reach. That should
place it above subprocess-wrapper and below TEE co-execution. Whether it does, and what residual
risks remain (the designer still authors the code that runs), is what the build is for.

## Scope

1. Recorder as a decorator around a Modal function, emitting the nine RAND schema field groups.
2. Hash chain over records, Merkle root, signed attestation (phases 1 and 2).
3. Local transparency log, plus opt-in public anchoring to Sigstore Rekor.
4. `verify` command implementing the paper's four ordered checks (phase 4).
5. One real design tool run end to end. ESMFold2-Fast and ESMC have run on Modal; ProteinMPNN
   and BoltzGen are GPU-unvalidated.
6. Not built: a Claude reviewer that reads a bundle and reports what the run optimized, what data it
   touched, and where stated purpose and recorded objective diverge. This targets the barrier
   the paper names as the gap between a record existing and anyone being able to act on it.

Demo order: tamper detection first (alter one record, verification names the broken link), then
the doctored-notebook case (benign stated purpose, recorded objective says otherwise).

## Non-goals

- Not a sequence screener. The audit bundle is what you hand a screener; the paper lists final
  results as the object for the downstream synthesis screen.
- No trusted execution environment. Assurance tier must be stated explicitly in any writeup,
  which the paper requires of any deployment.
- Not a governance proposal. RAND names governance, distributional burden, and institutional
  review capacity as the binding non-technical barriers. We address only the third, partially.

## Backend test coverage

Install development dependencies with `python -m pip install -e '.[dev]'` in the
repository virtual environment. Run from the repository root:

```bash
python -m coverage erase
python -m coverage run -m pytest -q
python -m coverage combine
python -m coverage report
python -m coverage html
python -m coverage json
```

Open `htmlcov/index.html` for annotated source. Coverage includes branches, Python
subprocesses, and all three backend packages (`vatlog`, `runner`, `demo`),
including modules never imported by tests. The report's `Cover` column combines
statement and branch coverage; `coverage.json` contains separate counts.
No minimum percentage is enforced. These are offline tests: mocked cloud/model
boundaries do not establish live Modal or scientific-model reliability.

## Portable audit bundles

### Basic VAT for an ordinary Modal function

Install the Modal integration with `pip install -e '.[modal]'`. Put `@vat` **above**
`@app.function(...)`. Modal owns deployment and configuration; VAT records caller-observed
inputs and outcomes. The private recorder key stays in the caller process.

```python
from pathlib import Path

import modal

from vatlog.keys import load_private
from vatlog import VatRecorder, VatResult, vat
from vatlog.roles import LocalSigner, Role

app = modal.App("example")
image = modal.Image.debian_slim().pip_install(
    "pydantic", "cryptography", "rfc8785", "pymerkle"
).add_local_python_source("vatlog")

@vat
@app.function(image=image, cpu=1.0, timeout=300, retries=0)
def square(value: int) -> int:
    return value * value

@app.local_entrypoint()
def main():
    signer = LocalSigner(Role.RECORDER, load_private(Path("recorder.key")))
    with VatRecorder(Path(".vat"), signer) as recorder:
        call = square.with_recorder(recorder).spawn(12)
        result = call.get()
        assert result.value == 144
        result.save(Path("square.vat-node.json"))
        restored = VatResult.load(Path("square.vat-node.json"), trust=recorder.trust)
        assert restored.node_id == result.node_id
```

`VatResult` is immutable evidence, not a transparent replacement for the worker's value.
It exposes `.value`, `.status`, `.error`, `.domain_outcome`, `.execution_id`, `.node_id`,
`.node`, `.artifacts`, `.verify(trust)`, `.save(path)` and `.append_to(...)`.
Value and node access return defensive copies; artifacts have verified `.read_bytes()`.
An error dictionary is still a successful Python return. A raised exception produces a safe
failure node; accessing its `.value` raises `VatExecutionError` retaining `.result`.

Use `.remote.aio(...)`, `.spawn.aio(...)`, `.get.aio(...)` and `.cancel.aio()` in async code.
Persist a call before associating it with workflow state using `.prepare(...)`, then
`.submit()` / `.submit.aio()`. `.prepare_call(args, kwargs, execution_id=...)` keeps an explicit
execution UUID separate from worker arguments. `recorder.executions()` lists durable identities.
Use `square.with_recorder(recorder).resume(execution_id)` after a process restart. Poll and
connection timeouts never resubmit work; an unconfirmed submission is deliberately not retried.
Modal uses the same built-in `TimeoutError` for finite polling deadlines and worker-raised
timeouts. A finite poll therefore leaves that outcome unresolved; use `.get()` without a
poll deadline (or independently validated recovery) to collect the worker exception.
`VatResult.append_to(...)` embeds the exact standalone node in an open `SessionLog` without
rerunning the function. `verify_node(node, trust)` verifies the recorder signature and every
declared input/result byte independently of a session.

Submission acceptance is shielded from cancellation of an async waiter. Await `recorder.drain()`
before closing when submission tasks remain. A cancellation request is not proof of cancellation.
Adapter/signing failures retain the terminal observation for repair without re-execution.

All four scientific tools use the same recorder. Their error returns have separate domain
outcomes, partial bytes remain intermediates, and scientific acceptance is recorded separately
from execution. Sessions embed each exact node once. Proposed sequences become accepted output
artifacts only after scientific validation. Synthesized registered workflows pass explicit
`VatResult` references between nodes. Consumers record signed `input_bindings` or separate
control references; equal copied values do not create lineage.

The API targets Modal 1.5.5, with no older-API shims.
Its public callable/name accessors are deprecated for 1.6, so the dependency excludes that
untested future release. Singular module-level sync and async workers support bounded canonical
JSON values plus bytes (tuples normalize to arrays). Generators/methods and facade map/batch
methods are rejected. Scheduled, serialized and semantic-decorator execution is unsupported;
VAT cannot inspect all inner Modal configuration through public APIs. Our scientific workers
use retries=0; generic evidence describes logical invocations, not hidden physical retries.
Nested remote-to-remote VAT capture is not supported. This is `basic-local-recorder/1`: it proves what a trusted local recorder
observed, not Modal attestation, correct execution, or capture of filesystem, subprocess, log,
network and other undeclared side effects. Offline tests do not constitute a live Modal canary.
Current regression coverage includes `tests/test_vat_infrastructure.py` and
`tests/test_vat_modal_protocol.py` for independent computation and offline SDK behavior.

`build_bundle(log).save(Path("audit.zip"))` exports a ZIP containing:

```text
manifest.json       records, signed head, attestation, optional receipt, proofs, blob digests
blobs/<sha256>      original artifact bytes, including binary files
```

The manifest uses `bundle_format: "vat-bundle/1"`. File contents are stored directly,
without text conversion or base64. Verification checks included files against the
recorded SHA-256 hashes and byte lengths. Signing roles add a designer statement at leaf 0
and a signed key id on the head.

```bash
PYTHONPATH=src .venv/bin/python -m vatlog.cli show audit.zip
PYTHONPATH=src .venv/bin/python -m vatlog.cli verify audit.zip --trust trust.json --sequence MKV
```

`Bundle.load(Path("audit.zip"))` reads the archive without extracting files;
`bundle.blobs` maps digests to Python `bytes`. Missing, duplicate, or unexpected archive
members are rejected. Changed file contents produce `TAMPERED` during verification.
Use `include_blobs=False` to export only metadata and digests; otherwise missing source
files fail export. A digest-only bundle does not establish availability of those files.

## Role-based web demo

### Live Designer research workflow

The Designer window can submit a prompt to a current OpenAI or Anthropic model via
OpenRouter. The backend uses the official Python MCP SDK for public PubMed tools and direct
HTTP APIs for Europe PMC preprint keyword search and bioRxiv DOI metadata; no new research-service account is
required. Scientific design is a separate Designer mode, documented below. Successful research
workflows offer per-model-turn and per-tool-call request/response evidence,
three distinct local signatures, public trust, a ZIP bundle and a Python verification report.
The response is untrusted text, not a safety verdict. No sequence is submitted for binding.

Try: “Research GFP-binding nanobodies. Search PubMed, retrieve abstracts, and summarize
the evidence and limitations.” Ask Europe PMC for preprint keyword discovery, then look up
known bioRxiv DOIs directly. Europe PMC indexes multiple preprint servers, not just bioRxiv.
Tool/network failures are explicit results the model can explain or
work around using another source. A server unavailable during connection is visibly disabled;
direct preprint tools remain available if PubMed MCP discovery fails. bioRxiv returns unreviewed preprint metadata
and abstracts; returned PDF/XML URLs do not mean full text was fetched. The app does not fetch
arbitrary returned URLs. Direct tool events retain exact HTTP response bytes as base64,
alongside parsed metadata, source URL and HTTP status. Use `scripts/check_research_mcp.py` for a fresh operator check.

Public MCP/API health can be checked without a model call or API key:

```bash
PYTHONPATH=src .venv/bin/python scripts/check_research_mcp.py
```

### Claude Science connector and skill

The loopback backend also owns approved `vat-workflow/1` DAGs and bounded
`vat-dynamic-job/1` CPU jobs. The stdio MCP process is only a client: it does not open the
SQLite store, load signing keys or call Modal directly. Every registered or generated terminal
execution remains an immutable `VatResult`; summaries returned by MCP are not evidence.

Install the repository environment and start the backend with `scripts/dev.py`. In Claude
Science, open **Settings > Connectors > Add connector > Local command**. Use a lowercase name
such as `vat-protein-workflows`, set the command to the absolute path of the repository Python,
and add these arguments:

```text
-m
runner.claude_science_mcp
```

Set `PYTHONPATH` to the repository's absolute `src` directory. These are non-secret settings.
Run `npm --prefix web run build:static` once if the rendered evidence pages
(`render_evidence_page`, `render_demo_evidence_page`; see "Graph viewer") are wanted.

Claude Science runs the connector inside a sandbox that has no network route to the loopback
backend and a seccomp filter blocking AF_UNIX sockets, so no transport reaches port 8000 from
there. The connector therefore serves the evidence and demo routes in this process by default,
from the same routers and the same verifier, which returns the same verdicts. The workflow and
dynamic routes own the durable journal, the recorder keys and Modal dispatch, so in-process
they refuse with `TOOL_UNAVAILABLE` rather than become a second owner.

Two consequences follow for setup. The interpreter must live inside the repository directory,
because the sandbox binds that directory and nothing else that holds a Python: a `.venv` whose
`bin/python` symlinks outside the repository fails to execute at all. That interpreter also
needs the `workflow` and `public-transparency` extras, since it performs the verification
itself. Setting `VAT_BACKEND_URL` opts back into the loopback backend, which is what an
unsandboxed run wants when the paid Modal workflow tools are in use; `VAT_BACKEND_MODE` forces
either `in-process` or `backend`.

Do not place Modal, OpenRouter or signing credentials in the connector environment. Claude
Science stores local connector environment variables unencrypted. Its
current documentation supports local-command MCP connectors on macOS, Linux and Windows; on
Windows the command must be `python` or a full executable path.

Package the project skills reproducibly, then upload the relevant ZIPs through
**Settings > Skills > Add skill > Upload a skill**:

```bash
PYTHONPATH=src .venv/bin/python scripts/package_claude_science_skill.py
```

The command writes `build/vat-protein-workflows.zip`, `build/vat-evidence-review.zip`, and
`build/vat-benchbb-validation.zip`, which are ignored. The focused skills run directly through
repository code without an MCP connection. The workflow ZIP also contains the VAT, runner, and
demonstration runtime needed to hash, sign, seal, publish, recover, and verify evidence. It can
dispatch bounded Modal work after explicit owner approval and an operator-supplied call and
compute limit. Its guidance
also keeps dry-runs, offline adapter tests, signed local observations and scientific validation
distinct. Claude Science's reviewer checks claims against recorded work; it does not rerun the
analysis or independently verify VAT.

Unpaid rehearsal commands:

```bash
for fixture in clean tampered partial; do
  PYTHONPATH=src .venv/bin/python -m demo.demo --fixture "$fixture" || test "$fixture" != clean
done
PYTHONPATH=src .venv/bin/python scripts/claude_science_demo.py registered --target-cif /path/to/source-checked-gfp.cif
PYTHONPATH=src .venv/bin/python scripts/claude_science_demo.py dynamic
```

The connector also loads any bundle by path: `load_evidence(archive_path, trust_path, ...)`
reads files under `VAT_EVIDENCE_DIR` (default: the connector's working directory), submits them
to the `/api/evidence` route, and returns an id for `inspect_evidence`, `experiment_evidence`,
`read_evidence_artifact` and `remove_evidence`; `vat://evidence/{id}/bundle` and `/trust`
serve canonical bytes. Optional `sigstore_identity`, `sigstore_issuer`,
`sigstore_trust_root_path` and `require_public_transparency` set the verifier policy for
bundles carrying public Sigstore evidence; the policy is fixed at load time and applies to
every later projection and experiment. The registered command only validates and prints a
BoltzGen → Boltz-2 plus ESMC proposal. The dynamic command only prints exact source, settings and digest. Neither command deploys an image
or submits Modal work. ipSAE is deliberately absent from the sample DAG because the
Boltz-2 return contract does not expose a compatible named PAE value at proposal time.

Install dependencies once from the repository root:

```bash
.venv/bin/python -m pip install -e '.[dev,workflow]'
cd web
npm ci
```

Then, from the repository root, launch both servers with one command:

```bash
.venv/bin/python scripts/dev.py
```

Open `http://127.0.0.1:5173/`. Ctrl+C stops both servers and their child processes. If either
server exits or fails to start, the launcher stops the other. It regenerates fixtures on
launch, but does not automatically install dependencies. Copy `.env.example` to `.env` in
the repository root and set `OPENROUTER_API_KEY=your-key` there for live calls. The launcher
reads it at startup regardless of your working directory. Existing exported variables take
precedence; values are literal (no variable interpolation or shell execution). `.env` is
git-ignored. Restart the launcher after edits. Without a key the fixture walkthrough still works.
Vite proxies `/api` to the loopback backend on port 8000. Port 8000 is the API, not a
second portal; visiting its root redirects to the UI on 5173. To use other ports, pass
`--api-port` and `--ui-port` to `scripts/dev.py` or set `VAT_API_PORT` and `VAT_UI_PORT`;
the launcher forwards both to the API, the Vite proxy and the redirect. The live model selector defaults
to Claude Opus 4.8 when available. Text is enlarged for presenting.
The key stays on the backend; never use a `VITE_` variable for it. Go incurs provider usage
charges. Network timeouts retry up to three attempts; previous attempts may still be billed.
Cancellation requires confirmation and interrupts local pending I/O, but cannot reverse
provider work or charges. Jobs and downloads are temporary and disappear on backend restart;
download what you want to keep. Only Designer displays live results; other roles remain fixtures.
This is a local single-user demo, not an authenticated public service. Production preview
below serves the fixture walkthrough only, without the development API proxy.

### Fixture walkthrough

The read-only walkthrough in `web/` presents the four generated fixtures through Designer,
Recorder, Notary, and Verifier tabs. It uses the real Python verifier during fixture generation;
the browser only displays the generated report. This is a synthetic local simulation, not a
live signing service or biological safety screen.

From the repository root, with Python 3.11+ and Node 24 installed:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd web
npm ci
npm run dev
```

Open the loopback URL printed by Vite (normally `http://127.0.0.1:5173/`). `npm run dev`,
`npm test`, and `npm run build` regenerate the ignored `web/public/demo/` evidence first.
For a production-like local server, run `npm run build` followed by `npm run preview`.
Stop the server before regenerating fixtures.

To verify a downloaded scenario from the repository root, pass the sequence as literal text:

```bash
PYTHONPATH=src .venv/bin/python -m vatlog.cli verify web/public/demo/clean.zip \
  --trust web/public/demo/clean.trust.json \
  --sequence "$(< web/public/demo/clean.sequence.txt)"
```

`clean`, `tampered`, `doctored`, and `partial` demonstrate exit codes 0, 2, 0, and 6.
The displayed catalog is unsigned presentation data; the supplied public keys establish only
the local trust model described above.

Included files are loaded into memory, so this implementation targets small
artifacts rather than streaming large model checkpoints.

The reader applies operational limits before expanding member contents: 64 MiB archive,
8 MiB manifest, 256 MiB total uncompressed contents, and 10,000 members. Stored and DEFLATE
members are accepted; encrypted and other compression methods are rejected. Python callers
can supply `Bundle.load(path, limits=BundleLimits(...))` from `vatlog.bundle` to choose a
different positive budget. The CLI uses the defaults. These are reader-policy limits, not
changes to artifact hashes, signatures, or the `vat-bundle/1` format.

Duplicate JSON keys, non-JSON constants, and record values that cannot be canonicalized are
rejected. Large incomplete sessions report at most 100 missing indices plus the exact missing
count; the verifier and local notary do not allocate a list proportional to the claimed size.

## Local signing roles

Four distinct Ed25519 keys have checkable roles: the designer signs a statement of
session and purpose in `session_open` at leaf 0; the recorder signs the tree head and DSSE
attestation; the notary signs a simulated receipt; the local transparency log signs checkpoints
over notarized attestation entries. Verification uses a public trust
bundle supplied by the verifier, resolving each key id against its registered role.
`--pubkey PUBLIC_KEY_BASE64` remains a single pinned-key mode and establishes no designer identity.
Revealing leaf 0 authenticates the designer's key id; withholding it leaves identity unverified.

The `local-null-screen/1` notary policy requires the full chain, replays it against the root,
verifies recorder signatures and checks the head/attestation session, size and root. It applies
no sequence screen. Its receipt remains verifiable with a selectively disclosed bundle.
Separate processes and keys imitate role boundaries; they do not establish independent
notarization, independent timing, or platform-enforced capture. All time evidence is local.

For a local process demonstration, keep private keys outside the log directory:

```bash
mkdir -p /tmp/vat-role-demo/keys
export PYTHONPATH=src
for role in designer recorder notary transparency_log; do
  .venv/bin/python -m vatlog.cli keygen --role "$role" \
    --private "/tmp/vat-role-demo/keys/$role.key" --trust /tmp/vat-role-demo/trust.json
done
.venv/bin/python -m vatlog.cli sign-session --designer-key /tmp/vat-role-demo/keys/designer.key \
  --session role-demo --purpose 'therapeutic lead' --out /tmp/vat-role-demo/statement.json
.venv/bin/python scripts/role_recorder.py --recorder-key /tmp/vat-role-demo/keys/recorder.key \
  --statement /tmp/vat-role-demo/statement.json --root /tmp/vat-role-demo/logs --out /tmp/vat-role-demo/recorded.zip
.venv/bin/python scripts/role_notary.py /tmp/vat-role-demo/recorded.zip \
  --notary-key /tmp/vat-role-demo/keys/notary.key --trust /tmp/vat-role-demo/trust.json --out /tmp/vat-role-demo/notarized.zip
.venv/bin/python -m vatlog.cli log-init --store /tmp/vat-role-demo/transparency.sqlite \
  --log-id role-demo --log-key /tmp/vat-role-demo/keys/transparency_log.key --trust /tmp/vat-role-demo/trust.json
.venv/bin/python scripts/role_transparency_log.py /tmp/vat-role-demo/notarized.zip \
  --store /tmp/vat-role-demo/transparency.sqlite --log-key /tmp/vat-role-demo/keys/transparency_log.key \
  --trust /tmp/vat-role-demo/trust.json --out /tmp/vat-role-demo/anchored.zip
.venv/bin/python scripts/role_verifier.py /tmp/vat-role-demo/anchored.zip \
  --trust /tmp/vat-role-demo/trust.json --require-transparency
```

Key generation refuses existing private paths or duplicate roles; files are created at mode
0600. Trust JSON contains public keys only.

## Local transparency observer

The separate SQLite log commits canonical attestation entries and signed checkpoints atomically.
Notarized bundles include entry inclusion and immediate consistency proofs. A verifier must use
`--require-transparency` when anchoring is required; removing both optional objects then fails
with `NOT_INCLUDED`. Receipt-only bundles are intermediate submissions and also return exit 4.
Recorder-only verification remains available without that requirement.

Run `PYTHONPATH=src .venv/bin/python scripts/transparency_demo.py --out .local/observer-demo`
in a fresh directory. This records three sessions, saves the first and second checkpoints,
and verifies growth to the second checkpoint even after the third append. With those outputs:

```bash
PYTHONPATH=src .venv/bin/python -m vatlog.cli log-consistency \
  --store .local/observer-demo/log.sqlite --trust .local/observer-demo/trust.json \
  --from-checkpoint .local/observer-demo/first.json --to-checkpoint .local/observer-demo/target.json \
  --out .local/observer-demo/observer-proof.json
PYTHONPATH=src .venv/bin/python -m vatlog.cli verify .local/observer-demo/bundle.zip \
  --trust .local/observer-demo/trust.json --require-transparency \
  --witness-checkpoint .local/observer-demo/first.json \
  --witness-consistency-proof .local/observer-demo/observer-proof.json
```

`log-export --store PATH --trust PATH --out PATH` exposes canonical entries and checkpoints
without private keys. Observer input files are capped at 64 KiB. Retain public trust and earlier
checkpoints separately: the laptop operator controls the local clock, database, and keys.
Consistency exposes changes relative to those retained views; it establishes no independent
timestamp or globally public availability.

Backend publication saves the exact notarized submission before append and recovers it on
restart, without rerunning model or scientific work. Private preparation state and the stable
log live under the workflow root; only final bundle/trust/report/response files are downloadable.


## Public Sigstore anchoring

`vat anchor-sigstore` signs the SHA-256 digest of a notarized bundle's receipt payload with
Sigstore's keyless flow, submits it to Sigstore's public Rekor log and requests an RFC 3161
timestamp from Sigstore's public timestamp authority. The signing identity is a Google Cloud
service account; Fulcio puts its email in the certificate, so the public record says "this
service account signed the digest of this receipt". Records, sequences, prompts, transcripts,
artifact blobs, the notary key and the notary signature never leave the machine. No browser or
interactive login runs at demo time; the only credential is the service-account JSON key.

One-time setup: in a Google Cloud project you own, create a service account with no IAM roles,
create a JSON key for it, and store the key under `.local/` (gitignored) with mode 0600. Its
email, `NAME@PROJECT_ID.iam.gserviceaccount.com`, becomes permanently visible in every entry.
No Google API needs enabling.

```bash
.venv/bin/python -m pip install -e '.[public-transparency]'
PYTHONPATH=src .venv/bin/python -m vatlog.cli anchor-sigstore notarized.zip \
  --output anchored.zip --trust trust.json \
  --google-service-account-key .local/KEY.json          # prints the disclosure preview and stops
PYTHONPATH=src .venv/bin/python -m vatlog.cli anchor-sigstore notarized.zip \
  --output anchored.zip --trust trust.json \
  --google-service-account-key .local/KEY.json --acknowledge-publication
PYTHONPATH=src .venv/bin/python -m vatlog.cli verify anchored.zip --trust trust.json \
  --require-public-transparency --sigstore-identity NAME@PROJECT_ID.iam.gserviceaccount.com \
  [--sigstore-trust-root cached-root.json]
```

Three claims stay separate in the report. `notarization: simulated` means the local
`local-null-screen/1` receipt; `public transparency` means an independently operated log
included a signing event, by the named identity, over that receipt's digest; `RFC 3161
timestamp` is the countersigned publication time, never execution time.
`--require-transparency` accepts local or public evidence; `--require-public-transparency`
demands the public form. A bundle carrying both must pass both. Verification runs the official
Sigstore verifier under a trusted root obtained independently of the bundle (refreshed over
TUF, or a cached `--sigstore-trust-root` file) with an identity policy equal to the
verifier-supplied `--sigstore-identity` and `--sigstore-issuer` (default
`https://accounts.google.com`). Rekor's `integratedTime` is never used as trusted time.

Public entries cannot be removed. A network failure after submission exits with code 7
(`PUBLICATION_OUTCOME_UNKNOWN`), keeps the input, saves any complete response under
`OUTPUT.recovery/` with owner-only permissions, and never resubmits on its own;
`--recover-from FILE` rebuilds the output from a saved response without a network call, and
`--acknowledge-duplicate` is required to create a second public entry for the same receipt.

Status: tested against an in-memory Fulcio, CT log and Rekor stand-in plus a throwaway
`openssl ts` timestamp authority (`tests/sigstore_fakes.py`), with the official verifier running
unmodified in those tests, and demonstrated once against the production services on 2026-09-20. That canary's outputs are not included in
this repository, because its Fulcio certificate embeds a real service-account identity. To
produce your own anchored bundle, run `scripts/sigstore_demo.py` with your key as above. The
resulting bundle, its public trust file and a cached trusted root verify
offline with the command above or through the Claude Science connector's `load_evidence`
tool, or in the web evidence page by expanding "Public Sigstore evidence" and entering the
expected identity and trusted-root file. `scripts/sigstore_demo.py` stops before
publication unless given `--acknowledge-publication` and the key; each run with them creates
another permanent entry. Signing is keyless because Rekor v2
rejects pure Ed25519 `hashedrekord` entries.

## Agent transcript artifacts

Session instructions attach as `prompt` inputs on `session_open`. Each agent call can carry
a `prompt` input and `transcript` and optional provider-returned `reasoning` results. All use
the existing content-addressed blob store, ZIP export and check 4 digest/byte-length validation.
Identical bytes share one blob. The verifier never parses these artifacts and imposes no
transcript format or size cap; the demo uses synthetic timestamped JSON arrays.

Choose the record unit explicitly when supplying call bytes:

```python
from pathlib import Path

from demo.sessions import CallArtifacts, build_clean
from vatlog.roles import Keyring

root = Path("/tmp/vat-transcript-demo")
keyring = Keyring.generate()
log, _, _, _ = build_clean(
    root,
    keyring=keyring,
    instructions=b"Record the supplied demonstration outputs.",
    calls=[CallArtifacts(prompt=b"request", transcript=b"returned tool results")] * 3,
    granularity="agent_call",  # or "design_run" for a producer-aggregated transcript per run
)
```

The demo builder takes one call entry per design run; `agent_call_record` also lets custom
producers append any number of independently disclosable call leaves before sealing.
`build_doctored` demonstrates separate call leaves (1 opening + 3 calls + 3 runs), while
`build_partial` attaches transcripts to runs (1 opening + 9 runs). `build_clean` has no
transcripts unless supplied. `vat show` lists artifact kind, name, digest prefix, byte length
and availability without reading its content into the display.

Verifier status counts record types among revealed leaves and labels missing types unknown.
It reports instructions as present, withheld or absent; transcript counts distinguish records
with present bytes, withheld bytes, and no transcript. A record with multiple transcripts can
appear in both availability columns. Reasoning uses the same counts internally, but its absence
is not flagged or printed as a shortfall. These statuses add no check or exit code.

Redact before hashing. Redaction after hashing produces `TAMPERED` (2). Digest-only export
reports committed transcript bytes as withheld, and still verifies. A transcript commitment
establishes integrity of the supplied bytes relative to the signed record; it does not establish
a complete or truthful account of agent activity. Calls inside a single blob cannot be counted
or withheld independently, and calls omitted before hashing remain undetectable.

Sequence matching requires an `aa_sequence` or `nt_sequence` result; matching transcript or
reasoning bytes cannot establish sequence provenance. Inclusion proofs must match the signed
tree size and claimed leaf position under the fixed hash profile. Session identifiers must be
single path components, and the local API rejects appending or resealing once a sealing
artifact exists. Record-returning APIs and bundle export provide independent snapshots, and
loading a stored log checks its canonical encoding, session and chain before it can resume.

## Verification outcomes

`vat verify` prints each check and exits with a code, so a caller can branch without parsing
text. The codes are defined by `EXIT_CODES` in `src/vatlog/verify.py`.

| Outcome | Exit | Meaning |
| --- | --- | --- |
| `OK` | 0 | all five checks pass |
| `TAMPERED` | 2 | check 1, a broken hash-chain link; or check 4, a blob whose bytes do not match its recorded digest or length |
| `BAD_SIGNATURE` | 3 | check 2, an untrusted role or invalid head, envelope, designer statement or receipt; or inconsistent session commitments |
| `NOT_INCLUDED` | 4 | check 3, a revealed record has no inclusion proof under the signed root, or the head's timestamp is implausible |
| `SEQUENCE_MISMATCH` | 5 | check 4, the submitted sequence matches no revealed record's output |
| `INCOMPLETE` | 6 | check 5, fewer unique leaves are revealed than the signed `tree_size` |

Exit 2 also covers an archive that will not load at all (missing, duplicate, or unexpected
members), since a bundle that cannot be read and one that has been altered are both refused.

Checks 1 to 4 are RAND's four ordered checks. Check 5 is this project's session-completeness
extension, and it has a separate outcome because a withheld record and an altered one mean
different things to whoever receives the bundle:

```text
  [PASS] 1. hash chain - 1 revealed record, links intact
  [PASS] 2. attestation signature - assurance tier serverless_hosted
  [PASS] 3. inclusion proof - 1 leaf under signed root
  [PASS] 4. sequence match - matches output of record 5
  [FAIL] 5. completeness - bundle covers 1 of 10; missing leaves [0, 1, 2, 3, 4, 6, 7, 8, 9]
  signer roles: role-bound
  designer identity: unverified
  notarization: simulated (local-null-screen/1)
  independent timestamp: unavailable (local clock)
  attested capture tier: serverless_hosted
  head key: explicit key id
  revealed leaf types: design_run=1; unrevealed=9 (types unknown); signed tree_size=10
  session instructions: absent
  transcripts (revealed records): present=1, withheld=0, absent=0
  => INCOMPLETE
```

## Protein-design tools

The Designer's **Open protein design** button opens the generic structure-upload workflow.
It runs one user-authorized BoltzGen generation, then ESMFold2-Fast binder/complex predictions
and ESMC-300M masked sequence scoring for retained candidates. Those three adapters and the
separate ProteinMPNN v_48_020 backbone-to-sequence adapter can each be invoked on their own.
ProteinMPNN is listed in the Designer tool catalog but is deliberately not inserted into the
Go pipeline. GFP is example input, not an application restriction. Existing research and fixture
walkthroughs remain separate.

The [Anthropic report inventory](reference/anthropic-protein-tool-inventory.md) isolates 39 operational
tools. The repository has 30 implementations: the four above plus 26 others. Twenty-four
of those are offline-ready, and MMseqs2/Foldseek have implemented workers that wait for an explicit
database snapshot. Nine entries remain inventory-only and have no placeholder Modal endpoint because
their reviewed upstream revisions lack an immutable runtime/asset route or require owner-held gated
distributions that were not supplied.

Every added tool uses one request envelope and the same immutable result boundary:
`ModalTools().run(tool_id, request) -> VatResult`, with `await ModalTools().run.aio(...)` inside an
event loop. The catalog-driven operator command writes
the signed node and verified artifacts:

```bash
PYTHONPATH=src .venv/bin/python scripts/tool_run.py dssp \
  --request path/to/request.json --output .local/dssp-result --confirm-paid-work
```

The CPU/ESM3/soluble-ProteinMPNN deployment and Anthropic-kit deployment are separate so CPU tools
remain importable without the 722 MiB kit build context. These commands document the deployment
process:

```bash
# Pin and hash the exact kit build context.
PYTHONPATH=src .venv/bin/python scripts/stage_anthropic_kit_source.py

# After running a kit's documented install --weights route in its pinned image,
# bind every resulting private byte to that kit and optionally upload it.
PYTHONPATH=src .venv/bin/python scripts/stage_anthropic_kit_assets.py boltz2 path/to/boltz2-assets

# Prepare an explicit database snapshot for MMseqs2 or Foldseek.
PYTHONPATH=src .venv/bin/python scripts/stage_search_database.py \
  mmseqs2 uniref90-2026-03 'UniRef90 2026_03' database path/to/database

modal deploy src/runner/modal_tool_suite.py
VAT_ANTHROPIC_KIT_ROOT=.local/vendor/uplifting-biomolecular-modeling \
  modal deploy src/runner/modal_anthropic_kits.py
```

Source staging was exercised against the exact pinned checkout. Deployment, `--review-and-upload`,
image builds and paid calls were not run. Offline-ready means the pinned definition, staged-asset contract, bounded invocation,
parser and recovery tests pass; it does not mean an image was built or a model ran.

Install the local SDK/parser dependencies:

```bash
.venv/bin/python -m pip install -e '.[dev,workflow,serve]'
.venv/bin/python scripts/dev.py
```

Open http://127.0.0.1:5173/. Design is disabled unless `VAT_MODAL_ENABLED=1` is supplied in
the startup environment or root `.env`. **Do not enable it merely because offline tests pass.**
ESMFold2-Fast has run on Modal A100 hardware and ESMC-300M on L4 hardware. BoltzGen and
ProteinMPNN remain GPU-unvalidated. Separate scientific dependency locks are checked in;
review actual image identity and downloaded weight hashes for every new deployment.

Design state/artifacts persist in ignored `.local/workflows`, or `VAT_WORKFLOW_DIR` when set.
Only one backend may own a workflow directory. Ctrl+C requests remote cancellation;
unconfirmed cancellation is retained for recovery rather than declared successful.
Research-only jobs retain their existing temporary-storage behavior.

The implementation includes these explicit operator steps. Run only the tool-specific staging
and deployment requested for a reviewed, authorized experiment:

```bash
# Downloads tens of GB; reads HF_TOKEN from the startup environment/root .env.
PYTHONPATH=src .venv/bin/python scripts/stage_design_weights.py .local/tool-weights --download

# Or stage only the small official ProteinMPNN checkpoint and MIT notice; no HF account is needed.
PYTHONPATH=src .venv/bin/python scripts/stage_design_weights.py .local/tool-weights --tool proteinmpnn --download

# After reviewing source/weight licences and the generated hashes: writes to your Modal volume.
PYTHONPATH=src .venv/bin/python scripts/stage_design_weights.py .local/tool-weights --review-and-upload

# Builds/deploys SDK-only functions; review actual image/dependency identities before use.
.venv/bin/python scripts/deploy_design_tools.py --confirm-deployment
```

Staging, deployment and tool-smoke scripts read root `.env`, with exported variables taking precedence.
Never use a command that prints tokens or dumps the environment. No HF
token or OpenRouter key is supplied to scientific inference workers; they use staged weights.

For a reviewed target and explicitly authorized paid test, supply a JSON configuration with
`prompt`, `model`, `structure_path` (relative to the JSON), `format` (`pdb`/`cif`), `chain` and
optional `settings` (`count`, `min_length`, `max_length`, `seed`, `binding_residues`). Then:

```bash
PYTHONPATH=src .venv/bin/python scripts/design_demo.py path/to/demo.json \
  --output .local/demo-results --confirm-paid-work
```

Prepare the first example without billed inference:

```bash
PYTHONPATH=src .venv/bin/python scripts/prepare_demo_target.py P42212 \
  fixtures/gfp-wildtype.fasta --label 'avGFP wild-type (UniProt P42212)' --output .local/gfp-demo
```

This checks the public AlphaFold prediction against the supplied sequence and writes the
structure, source/representation metadata and `demo.json`. It is an ordinary-amino-acid
prediction, not experimental mature GFP chromophore chemistry. `fixtures/gfp.fasta`
contains F64L; it is not wild-type P42212.
The helper accepts other UniProt accessions and sequences; GFP is never hardcoded in the app.

After separate deployment/spend authorization, validate each adapter with a labelled control
sequence in `path/to/binder.txt` (canonical amino-acid bytes, not FASTA):

```bash
PYTHONPATH=src .venv/bin/python scripts/tool_smoke.py boltzgen --target .local/gfp-demo/target.cif --count 1 --output .local/smoke-generation --confirm-paid-work
PYTHONPATH=src .venv/bin/python scripts/tool_smoke.py proteinmpnn --backbone .local/gfp-demo/target.cif --design-chain A --count 2 --seed 1 --temperature 0.1 --output .local/smoke-proteinmpnn --confirm-paid-work
PYTHONPATH=src .venv/bin/python scripts/tool_smoke.py esmfold2 --sequence path/to/binder.txt --output .local/smoke-binder --confirm-paid-work
PYTHONPATH=src .venv/bin/python scripts/tool_smoke.py esmfold2 --sequence path/to/binder.txt --target .local/gfp-demo/target.cif --mode complex --output .local/smoke-complex --confirm-paid-work
PYTHONPATH=src .venv/bin/python scripts/tool_smoke.py esmc --sequence path/to/binder.txt --output .local/smoke-esmc --confirm-paid-work
```

Use a fresh output directory each time. Set `VAT_MODAL_ENABLED=1` only after release review;
restart the launcher before the end-to-end `design_demo.py .local/gfp-demo/demo.json` run.
Canary reports retain actual execution metadata, elapsed time and PASS/FAIL; billing cost and
cold/warm classification must be supplied from the operator's observation, not guessed.
The end-to-end helper saves downloads and checks both exact binding (exit 0) and a one-residue
alteration (exit 5). A zero-candidate result remains honest scientific evidence, not a passing
candidate demonstration. Fresh scientific Retry and mixed literature/design agent orchestration
are not implemented; uncertain jobs are recovered, never automatically resubmitted.

The retrospective BenchBB study is separate from the Designer pipeline. It froze
twelve released BBF-14 designs before inference, then ran one real ESMFold2-Fast complex fold
and one real ESMC score per design. Its complete 371-leaf evidence verifies; withholding and
alteration yield distinct verifier outcomes. The reusable operator is dry-run by default and
has no default cost cap. Its session evidence is not included in this repository.

A second bounded job uses public genomics data with no model at all. Cummins et al. (2024,
MBE, msae146) release their human ultraconserved-element sets as GRCh38 coordinates;
`scripts/uce_demo.py fetch` pins the publisher's supplement ZIP by digest, selects an evenly
spaced subset of the 279-element 100% set, fetches each sequence from NCBI one region at a
time, and writes `fixtures/uce_100set_subset.json` (ten elements, committed). `proposal` turns
that fixture into a `vat-dynamic-job/1` whose standard-library source recomputes the subset's
per-base composition against the paper's reported background (A/T 0.312, C/G 0.188); `run`
executes the same source locally without Modal. `dispatch` is the paid step: given the exact
proposal digest the owner approved and `--authorized`, it runs the same job service the
backend runs, against its own store under a `results/` directory, so it works from Claude
Science where the backend is unreachable; it seals the run, verifies it, and writes a tampered
copy beside it. A rerun resumes the job by identity. It needs `biodesign-vat-dynamic`
deployed (`PYTHONPATH=src .venv/bin/modal deploy -m runner.dynamic_modal_app`). The sealed job
never fetches: the sequences are its declared public input, and a reviewer checks them by
fetching again.

ProteinMPNN accepts a validated ordinary-amino-acid PDB/mmCIF backbone, remaps source chains to
stable A–Z IDs, and requires at least one original chain name through `--design-chain`. Its
sample scores and sequence recovery are model outputs, not experimental validation. A nonzero seed
is mandatory because upstream treats zero as randomized sampling. Distinct samples remain distinct
even when their generated sequences are identical.

Scores are computational assessments, not measured affinity, safety or independent votes.
ESMFold2 uses an ESMC component internally. Local capture/signatures are not platform
attestation, independent notarization or proof that no off-ledger work happened.

## Recording computation provenance

`@vat` records ordinary synchronous/asynchronous Python functions and registered Modal
functions. Use `.with_recorder(recorder).call(...)` for local work and `.remote(...)`
for Modal. Async callers use `.call.aio(...)` or `.remote.aio(...)`.

```python
from vatlog import vat, VatRecorder
from vatlog.roles import Keyring

@vat
def double(value):
    return value * 2

keys = Keyring.generate()
with VatRecorder(".local/example-journal", keys.recorder) as recorder:
    first = double.with_recorder(recorder).call(3)
    second = double.with_recorder(recorder).call(first.ref())
    assert second.value == 12
```

`.ref('/field/0')` selects from the immutable returned value using RFC 6901 JSON Pointer.
`.artifact_ref('file.cif')` selects named output bytes. Intermediate artifacts require
`allow_intermediate=True`. References may appear inside nested input lists/dictionaries.
The recorder verifies their evidence under its configured trust before dispatch and
passes ordinary values to the function. Reading `.value` and passing it loses lineage;
no dependency is guessed from matching data.

The signed `vat-prov/1` profile records the exact producer node, selector, destination
argument and canonical value commitment. Blob hashes identify content; producer-plus-
selector identifies an output occurrence. Repeated calls remain distinct even if their
results match. Local capture does not import Modal. Credentials and clients belong in
the wrapper's closure, not recorded arguments. Closures themselves are not captured.

The [schema](docs/schemas/vat-prov-1.schema.json) describes execution statements including
this profile. Existing statements without the profile have unrecorded lineage.
The verifier distinguishes valid, invalid, unresolved and unrecorded provenance from
the five bundle checks. Valid means recorded bindings are consistent, not that all
origins are known or every supplied input influenced execution.

```bash
PYTHONPATH=src .venv/bin/python scripts/provenance_demo.py --out .local/my-graph-demo
PYTHONPATH=src .venv/bin/python -m vatlog.cli provenance .local/my-graph-demo/bundle.zip --trust .local/my-graph-demo/trust.json --prov
```

The demo runs real local arithmetic including an intentional failure. No model, network
or GPU call is made. Output directories must be new to avoid overwriting history.
Provenance CLI exit codes: `VALID=0`, `INVALID=2`, `UNRESOLVED=3`, `NOT_RECORDED=4`.
They are separate from the existing `vat verify` exit codes.

### PROV mapping

Executions map to PROV-O activities, selected output occurrences to entities, generation
to `prov:wasGeneratedBy`, and input consumption to `prov:used`. Recorder identity uses a
separate VAT relation: it must not imply that the recorder executed the work. Explicit
request/control links use `vat:requestedBy`; they are not data bindings. JSON-LD has an
inline context and requires no remote schema lookup. The export is a projection of
claims, not a substitute for verifying the signed VAT evidence.

References: [W3C PROV-DM](https://www.w3.org/TR/prov-dm/),
[PROV-O](https://www.w3.org/TR/prov-o/), [JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901.html).

### Graph viewer

The graph can be viewed three ways. They share one React + ELK viewer and one Python
projection (`runner.graph.project`); they differ in what stays live.

1. **Web workspace.** Start both servers with `.venv/bin/python scripts/dev.py`, open
   `http://127.0.0.1:5173`, and choose **Evidence graph** at the top. Upload a bundle ZIP
   and its public `trust.json`. The generated arithmetic example above supplies both.
   The clean/tampered/partial fixtures can also be opened directly; they carry no recorded
   dependency edges. Everything below applies to this view.
2. **Self-contained page.** `npm --prefix web run build:static` once, then
   `PYTHONPATH=src .venv/bin/python -m runner.evidence_html bundle.zip trust.json --out page.html`
   (with `--sequence`, `--sigstore-identity`, `--sigstore-trusted-root` and
   `--require-public-transparency` as for the connector) verifies offline and writes one
   HTML file, about 2 MB, with the projection embedded as JSON. It renders the verdicts, the
   graph, the overlays and the object inspector, fetches nothing, and opens anywhere an
   `.html` file opens, including Claude Science's artifact preview, whose sandboxed iframe
   allows scripts but no network. Falsification, downloads and artifact previews are absent;
   they need the backend. The verdicts are those computed when the page was written.
3. **From the connector.** `render_evidence_page` (a loaded evidence id) and
   `render_demo_evidence_page` (`clean`, `tampered` or `partial`) write the same page as an
   `.html` file under `VAT_EVIDENCE_DIR`. Copy it into the Claude Science workspace to open it.

Workflow groups each execution's lifecycle leaves. Expand output values to see distinct
output occurrences; identical bytes from separate producers stay separate. Signatures
shows node verification and session membership separately. Order/hash-chain overlays
are not computation dependencies. Select a box or searchable list entry to inspect its
statement, signer identifiers and disclosed artifact bytes.

Falsification changes one copy at a time and retains the original signatures, head and
proofs. Apply and verify, compare the reports with the original, reset, or download the
changed bundle and action description. Narrative text is unsigned and does not change
the evidence. Referenced but undisclosed producers have no invented names or leaf positions.

The browser reads Python-computed reports; it does not trust uploaded report files.
Public trust is operator-supplied, not independently established. Uploads are kept in
memory under opaque identifiers until removed or the server stops: eight uploads,
256 MiB total artifact bytes, and the standard archive limits. Artifact previews are
plain text capped at 64 KiB. Views above 500 graph objects use the searchable list rather
than attempting an unreadable layout. No evidence endpoint executes tools or signs.

The viewer uses [React Flow](https://reactflow.dev/learn) 12.11.6 (MIT; React >=17) and
[ELK](https://github.com/kieler/elkjs) 0.12.0 (EPL-2.0 option). ELK sets positions only.

### Compare saved structures locally

```bash
PYTHONPATH=src .venv/bin/python scripts/structure_provenance.py \
  --reference /path/to/reference.cif \
  --prediction /path/to/prediction-1.cif \
  --prediction /path/to/prediction-2.cif \
  --out .local/structure-provenance
```

This records actual import, target-aligned comparison and report calls, retaining every
supplied comparison. It makes no network or inference call. The imported bytes are an
explicit starting point, not retroactive provenance for their creation. The existing
comparison requires matching protein sequences on chains A and B and reports every
reference conformer. No target identity is hardcoded.

### Integration status

Local and Modal capture share input references and finalization; the viewer accepts their
published bundles. Production research/provider-attempt capture, automatic reference
propagation through the scientific pipeline, and explicit repeat identities are not
implemented. Existing production records carry no dependency edges and are displayed as
supplied.

## References

- Allison Berke, Kyle A. Kilian, Graham Griffin, Jay Atanda, Enrique Vazquez, *Verifiable Audit Trails for AI-Enabled Biological Design Tools: A Proposal for Verifiable Biodesign Logging*, RAND Corporation, PE-A4613-1, June 2026. <https://www.rand.org/pubs/perspectives/PEA4613-1.html>
  Paraphrased with short quotations in [`reference/rand-vat-spec-extract.md`](reference/rand-vat-spec-extract.md).
- Anthropic, "Claude accelerates protein design", 2026-08-18.
  <https://www.anthropic.com/research/Claude-accelerates-protein-design>
- Anthropic, *claude-protein-binder-design* dataset (prompts, data and structures), Hugging Face,
  2026, CC BY 4.0. <https://huggingface.co/datasets/Anthropic/claude-protein-binder-design>. The multi-target campaign prompt this repository discusses is
  [`prompts/prompts/multi_target_binder_design_prompt.md`](https://huggingface.co/datasets/Anthropic/claude-protein-binder-design/blob/main/prompts/prompts/multi_target_binder_design_prompt.md); notes are in
  [`reference/protein_binder_protocol/README.md`](reference/protein_binder_protocol/README.md).
