# VAT protocol: spec extract from RAND PE-A4613-1

**Source**: Allison Berke, Kyle A. Kilian, Graham Griffin, Jay Atanda, Enrique Vazquez,
*Verifiable Audit Trails for AI-Enabled Biological Design Tools: A Proposal for Verifiable
Biodesign Logging*, RAND Corporation, PE-A4613-1, June 2026.
<https://www.rand.org/pubs/perspectives/PEA4613-1.html>

**Why this file exists**: RAND's terms forbid reposting the publication, so this repository links
to it rather than carrying a copy. The PDF this extract was made from has SHA-256
`a8fed8e1edad2050a3d8830ffd01ad647ee122f684cce48a66b6fc11d0c3c5a3`. The extract is written to
stand on its own.
Quotes are marked; everything else is paraphrase. Extracted 2026-09-08.

## The gap the paper names

Synthesis providers and regulators receive a sequence and cannot determine whether it was
"derived using safe design objectives or tailored for harmful properties." Traditional
documentation (PDF reports, notebooks) is mutable and tool-specific. The information is not
inherently unrecoverable; there is simply "no systematic mechanism to capture and preserve it
at the point of design."

The paper states the cryptographic components (hash chaining, hardware-bound attestation,
append-only transparency logging) are individually mature, and that its contribution is
"the proposed composition of these components into a unified protocol tailored to the specific
threat model of AI-driven biodesign."

## Parties

| Role | Definition |
| --- | --- |
| Designer | Individual or org using AI-bio tools to generate a sequence |
| Recorder | Software process under a hardware-enforced trust boundary that observes and logs each step |
| Notary | Independent service that validates the log against policy and issues a signed attestation |
| Verifier | Downstream party (synthesis provider, regulator) confirming provenance |

Roles may be filled by one organization; what matters is that each carries distinct trust
assumptions.

## Protocol phases

1. **Logging.** Recorder observes each computational event (tool invocation, input file,
   parameter set, output artifact) and appends a structured record to an append-only local log.
   Each record embeds the hash of the previous record, forming a hash chain, so retroactive
   insertion or deletion is detectable. Signing key ideally inaccessible to the designer and
   host OS. Software-rooted trust (key protected only by OS access control) is called
   acceptable in lower-risk contexts but explicitly weaker.
2. **Sealing.** At run end, compute a root hash over the full log; produce a signed attestation
   binding that root hash to the recorder process identity and hardware environment. The paper
   calls this attestation "the primary artifact of the protocol."
3. **Notarization.** Designer submits log plus attestation to a notary. The notary replays the
   hash chain, validates the attestation signature, applies domain policy (their example: check
   that no output sequence exceeds a similarity threshold to sequences of concern), then appends
   an inclusion record to a public append-only transparency log, obtaining a timestamp and
   inclusion proof. Returns a signed bundle (log + attestation + inclusion proof). The
   transparency log's specific job is making backdating detectable.
4. **Verification.** Verifier checks four things in order: (1) hash chain internally consistent;
   (2) attestation signature valid and from a recognized notary; (3) inclusion proof places the
   attestation in the transparency log at a plausible time; (4) the submitted sequence matches
   the output recorded in the log. Passing all four yields nonrepudiation, tamper evidence, and
   attribution.

## Record schema (Table, nine field groups)

| Group | Fields | Stated biosecurity value |
| --- | --- | --- |
| User and session context | `user_id` (public-key thumbprint); institutional OIDC (OpenID Connect); purpose tag (e.g. "therapeutic lead" vs "biochemistry class"); `session_uuid`; login IP; coarse GeoIP location | Attribution and insider-threat deterrence |
| Timestamps | Start/stop per pipeline stage; monotonic clock skew record | Event chronologies for forensic reconstruction |
| Input artifacts | Uploaded FASTA or GenBank files; template PDB IDs; conditioning prompts; reference MSAs (multiple sequence alignments); SHA-256 of each | Shows whether restricted sequences were supplied |
| Design parameters | Hyperparameters (`num_recycles`, `temperature`, `cfg_scale`); objective functions (Rosetta score weights, diffusion loss terms); random seeds | Detect covert optimization for toxicity or immune evasion |
| External data sources | Exact database snapshot digests (UniProt release, NCBI NR date); AI model checkpoints | Reveal use of disallowed or outdated data |
| Environment snapshots | Container image digest; Python/CUDA versions; GPU model; kernel hash; SBOM (software bill of materials) | Deterministic re-execution |
| Intermediate outputs | MSA depth metrics; validation scores; filtering thresholds; troubleshooting logs | Surface suspicious pruning of red-flag variants |
| Final results | Designed AA/nucleotide sequences; structural coordinates; in-silico QC reports | Core object for the downstream synthesis screen |
| Hash-linking metadata | `sha_prev` chain pointer; Merkle leaf index; Rekor entry ID | Cryptographic immutability across records |

Typing rules: sequences as text or as hashes with the full sequence stored separately;
parameters as typed key-value pairs (numeric thresholds/scores, enumerated strings for model
names and objective functions); timestamps ISO 8601. Purpose: fields must be machine-parsable
for automated screening without manual inspection.

## Security properties

Completeness, immutability, usability, scalability, extensibility, neutral trust anchor.
Immutability is enforced jointly by the phase-1 hash chain and the phase-3 transparency log.
Usability and scalability are called implementation constraints rather than cryptographic
properties, but security-relevant "because a schema that imposes prohibitive overhead will be
circumvented." The neutral trust anchor justifies phase-3 independent notarization; without it
"the schema reduces to a self-signed claim by the designer."

Tool independence comes from two choices: a domain-neutral provenance envelope where any
computational activity is an activity node with typed I/O entities (a Rosetta scoring run and
an AlphaFold prediction use the same schema), and recording that intercepts file I/O and
environment variables at the OS level rather than integrating with each tool, so legacy tools
with no API can be logged unmodified.

## Capture configurations, and the assurance bound (the load-bearing limitation)

The protocol "displaces rather than removes the trust requirement": if the recorder only sees
what the tool declares, the log can be internally consistent and cryptographically valid while
not reflecting what the tool actually computed.

| Capture tier | Mechanism | Stated limitation |
| --- | --- | --- |
| Strongest | Recorder and tool co-execute inside the same TEE (trusted execution environment); outputs captured before reaching the host OS | Requires the tool compiled and attested for the TEE; "not currently feasible for most existing biodesign software" |
| Middle | Kernel- or hypervisor-level monitor intercepting the tool's syscalls | Avoids modifying the tool but needs elevated privileges and its own integrity assumptions |
| Weakest / most accessible | Recorder wraps the tool as a subprocess, observing declared inputs and outputs via filesystem and environment-variable interfaces | A designer with root can substitute outputs after the run but before the recorder finalizes the log |

Key sentence for our purposes: the subprocess tier "provides meaningful evidence of
nonrepudiation against casual or opportunistic falsification but may not withstand a determined,
technically sophisticated designer with root access to their own machine."

Deployment configurations (a separate axis from capture): (1) hardware-bound local signing,
(2) remote notary signing for researchers lacking TEE-capable hardware, which also enables
centralized policy enforcement, (3) offline and air-gapped verification.

## Feasibility analogs the paper cites

Sigstore Rekor, ~5 million entries/day in 2025, storing in-toto attestations for Kubernetes,
distroless containers, Homebrew. Also Google Certificate Transparency, Go language-package
checksums (~2 million entries), Debian Reproducible Builds (~10k pilot). FoldMark and SynthID
appear as watermarking pilots, a different mechanism from logging.

## Gaps and future work the paper names

- **Primary barriers are not technical.** Three named: absence of governance structures to
  operate shared infrastructure (transparency logs, notary services); uneven distributional
  burden across the research community; and "the gap between the existence of an audit record
  and the existence of institutional capacity to review and act on it."
- **Highest practical uncertainty** is tool-level integration, specifically secure capture of
  tool outputs by the recorder.
- **Reference implementation is anticipated, not delivered.** The paper asks developers of
  AlphaFold, RoseTTAFold and ProteinMPNN to engage "before a reference implementation is
  finalized, not after."
- **Federated identity** (institutional IdPs issuing researcher credentials, mutually
  recognized by notaries) is called worth developing in future work, "particularly because it
  maps naturally onto existing research computing infrastructure."
- **Tiered assurance schema** should be developed, and communicating assurance levels should be
  a priority in any pilot.
- Cross-tool harmonization with extant biodesign software remains a challenge.
- The decentralized design is "a maximally decentralized baseline," not the permanent target;
  federated or centralized trust may satisfy requirements at lower complexity.

## Where our build sits relative to this

We implement local recording, signatures, verification, and a simulated phase-3 notary and
transparency log. The separate laptop-local SQLite log signs checkpoints over attestation
entries and returns inclusion/consistency proofs. A verifier can preserve an earlier checkpoint
and check subsequent growth. Four local keys distinguish roles; none establishes an independent
operator or trusted time source. Scientific workers can execute on Modal, while the local
recorder signs the observations it receives. Platform capture and source correspondence are not
independently attested. See `VISION.md` for current assurance limits.

We also target the review-capacity gap: an agent that reads a bundle and reports what the run
actually optimized, what data it touched, and where stated purpose and recorded objective
diverge.
