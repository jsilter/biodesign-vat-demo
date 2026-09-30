# biodesign-vat-demo Vision

## Mission

Build a demonstrable reference implementation of RAND's verifiable audit trail (VAT)
proposal for AI-enabled biological design. The project should let a synthesis provider,
reviewer, or regulator answer two different questions:

1. Does a submitted biological artifact match an authenticated record of how it was made?
2. Does the submitted bundle account for every record in the committed design session?

The first question is RAND's core provenance problem. The second is this project's extension:
honest individual records do not prevent a designer from disclosing only the favorable member
of a larger search.

Built as a one-day demo for the Boston Computational Biology Hackathon (2026-09-22); not a
production service or a substitute for biological sequence screening.

## Product Story

A judge receives a submitted sequence, its VAT audit bundle, and public signing-role trust.
The verifier checks the exact sequence against the recorded outputs and distinguishes
four materially different cases:

- a complete, internally consistent bundle;
- an altered record or artifact;
- an honestly recorded objective that conflicts with the stated purpose; and
- a valid record disclosed from an incomplete committed session.

The demo should make the limits equally visible. A signed local log cannot establish that the
recording process was independent, and no record-layer construction can prove that a designer
performed no additional off-ledger work.

## Architecture

### VAT log (`src/vatlog/`)

Typed Pydantic records cover RAND's nine field groups. Records are RFC 8785-canonicalized,
hash-chained, placed in an RFC 6962-style Merkle tree, and sealed with an Ed25519-signed head
and DSSE-shaped attestation. The verifier checks record integrity, signatures, Merkle
membership, exact output binding, and completeness relative to the signed record count.
Portable `vat-bundle/1` ZIP archives preserve original binary artifact bytes; digest-only
bundles remain possible when artifact availability is out of scope.

Four local signing roles separate responsibilities: a designer statement at leaf 0,
recorder-signed heads and attestations, simulated `local-null-screen/1` notary receipts, and a
transparency log that signs checkpoints over notarized attestation entries. Bundles carry
inclusion and consistency proofs, and verifier-preserved checkpoints let a reviewer compare
later views against earlier ones. The local log is a laptop-local simulation with no
independent time source or public operator. An opt-in `anchor-sigstore` path adds standard
Sigstore evidence for the notary receipt digest (keyless signing under a Google
service-account identity, Rekor inclusion and an RFC 3161 timestamp), checked by the official
Sigstore verifier under an independently obtained trusted root.

### Capture (`from vatlog import vat`)

A caller-side capture boundary sits above Modal's own function decorator. Sync and async calls
return immutable `VatResult` evidence, and a durable local recorder journals preparation,
acceptance and terminal observations before signing a placement-independent `vat-node/1`.
Private recorder keys never enter remote workers. Explicit `VatResult` references record
signed argument bindings, so a separate provenance verifier can check which outputs fed which
steps. The backend executes owner-approved `vat-workflow/1` DAGs and publishes the original
signed nodes without rebuilding them; recovery after failure never reruns science.

### Applications (`src/runner/`, `web/`)

- A Designer web workflow runs live literature research (PubMed MCP, Europe PMC, bioRxiv) and
  a fixed protein-design pipeline (BoltzGen generation, ESMFold2-Fast prediction, ESMC-300M
  scoring) on Modal, with ProteinMPNN callable separately, recording every model response and tool result as
  its own leaf. The backend, not the model, schedules tools.
- A catalog of 39 protein tools derived from Anthropic's protein-design report exposes 30
  behind one `run(tool_id, request) -> VatResult` boundary; see
  `reference/anthropic-protein-tool-inventory.md`.
- An Evidence graph view renders recorded relationships from public bundles and trust,
  separating node signatures, session membership and missing evidence, with single-action
  falsification experiments.
- A Claude Science integration (MCP connector plus skills) proposes, approves, executes and
  interprets workflows. Registered workflows require per-node approval, an exact proposal
  digest and separate spend authorization; approved `vat-dynamic-job/1` source runs on a
  fixed CPU-only Modal boundary with no network, secrets or volumes.
- Demo fixtures (clean, tampered, doctored, partial) exercise the verifier's distinct
  outcomes, including agent transcript capture attached to the records they describe.

Computational confidence and masked pseudo-perplexity are separate named metrics, not
experimental affinity, safety certification or independent model votes.

## Assurance Model and Limits

The backend uses four distinct local software-rooted keys. It demonstrates integrity and membership
relative to a supplied key and session commitment; it does not demonstrate an independently
recognized notary, independently observed timestamp, or platform-enforced capture boundary.
Modal's configuration flags are designer-selected unless Modal itself attests their
enforcement.

The Basic Modal node is intentionally no stronger: it proves that the local recorder signed the
declared bytes it observed. Runtime identifiers, source correspondence and configuration flags
are observed or self-asserted, not platform attestations. Undeclared filesystem, subprocess,
logging, network and external side effects are outside the node.

Completeness is also deliberately narrow. A tree head commits to N record leaves, so a bundle
containing fewer than N unique authenticated positions is detectably incomplete. It does not
count design runs when one run emits multiple events, prove the hidden links of a partially
revealed hash chain, show that the population was committed before outcomes were known, or
detect a separate unpublished session. Closing the last gap requires an external chokepoint,
such as an execution platform, synthesis provider, journal, or registry that refuses unlogged
work.

Transcript hashes commit only to the bytes the producer supplied. They cannot establish that
the transcript is complete, truthful, or the only account of the session. Calls aggregated
inside one transcript are invisible to leaf counting; redaction must happen before hashing.

The strongest defensible claim is therefore:

> The verifier detects changes to authenticated records, establishes exact binding to a
> recorded output, and reports missing records relative to an accepted session commitment. It
> does not establish independent execution capture, independent timing, or the absence of
> off-ledger work.

## Known gaps

- The notary is simulated (`local-null-screen/1`) and applies no sequence screen; all time
  evidence is local apart from the opt-in Sigstore anchor.
- The typed schema is not cross-checked field by field against the RAND proposal.
- Several scientific adapters are validated only offline; offline tests do not establish GPU
  execution. MMseqs2 and Foldseek need staged database snapshots, and nine cataloged tools have
  no endpoint.
- No dedicated bundle-review agent or mixed literature/design agent is built, and there is no
  independent platform capture. Model summaries never replace deterministic verification.

## Repository and Publication Constraints

The RAND PDF is cited and linked, never committed; RAND's terms forbid reposting it. Keep RAND
requirements distinct from the project's session-completeness extension. Detailed research belongs in
`reference/`. `docs/` holds the presentation and its supporting assets.
