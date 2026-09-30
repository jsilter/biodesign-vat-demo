# biodesign-vat: presentation guide

`docs/` contains the interactive HTML slides and this presenter guide, plus screenshot
assets, the machine-readable schema used by provenance tests, and a literature-screening
prompt. Operational instructions live in `README.md` and research in `reference/`.

## Files and versions

- [Interactive HTML slides](project-demo-slides.html): 12-slide overview; use **Switch to deep dive** for the 30-slide learning version. Press **N** for narration. Arrow keys or Space advance; Escape closes notes. Keep `assets/presentation/` alongside the HTML.

The overview contains 736 spoken words: roughly 4:55 at 150 words/minute before pauses.
Allow five to six minutes; a strict five-minute slot needs further distillation and rehearsal.
The timing table below is a target, not a measured delivery time. Narration is maintained
inside the HTML next to each slide. Notes overlay the audience slide; keep them closed during
projection. PDF / Print exports the selected version; the detailed version needs 25–30 minutes with explanation, longer for exercises. These are presentations, not video files.

## The product story

Write for an external audience unfamiliar with the repository. Slides and spoken notes must
stand alone: explain roles and concepts, cite public sources, and state meaningful limitations.
Keep source paths, internal feature names, revision history and reproduction commands in this
presenter guide, not on audience slides. Technical examples must teach a concept, not require
knowledge of this implementation.

Apply the author's prose standards: formal, specific statements; sentence-case headings;
no em dashes or slogan sequences. Name what each participant submits, sees or verifies.
Retain technical limitations while avoiding repeated rhetorical contrasts and concluding aphorisms.

The presentation proposes using design records in synthesis review:

> The organization submits a sequence with signed records for authorized review. A public
> ledger records commitments and metadata without publishing the underlying design records.

One organization produces work; an authorized reviewer inspects evidence; a globally readable,
independently operated ledger anchors compact commitments. Those roles and deployment controls
are the destination. The current implementation demonstrates the proof flow with a local log.

Screenshots show the verifier's results on synthetic submissions. The clean
and tampered fixture check panels were captured from the built frontend, with no live backend,
model call or GPU execution. Assets are unaltered element screenshots under
`docs/assets/presentation/`. The remaining diagrams and result cards are explanatory visuals,
not fabricated application screenshots.

## Precisely who receives what

| Recipient | Current data boundary | Qualification |
| --- | --- | --- |
| Organization / local recorder | Recorded requests, outcomes, artifact bytes and local signing keys. | External services still see inputs sent to them. |
| External scientific, model or literature service | Submitted prompts, queries, sequences, structures or other applicable inputs. | VAT does not guarantee provider confidentiality, logging behavior or retention. |
| Current notary and transparency-log admission | Submitted bundle: full record chain, signed objects and any supplied artifact bytes. | Both validation paths replay the full chain. Public export is narrower than admission. |
| Authorized reviewer | The disclosed records, supplied artifact bytes, signatures, proofs and public trust. | Passing session completeness requires all committed records. Omitted artifact bytes remain unavailable for inspection. |
| Public ledger reader / monitor | Entries, checkpoints and proof hashes/positions; exact fields below. | No underlying record chain or artifact bytes in the public export. |

**Public entry fields:** `entry_format`, `log_id`, `leaf_index`, `accepted_at`,
`notary_receipt_digest`, `attestation_digest`, `session_uuid`, `session_root`,
`session_tree_size`.

**Public checkpoint fields:** `checkpoint_format`, `log_id`, `tree_size`, `root_hash`,
`timestamp`, `keyid`, `simulated`, `independent_timestamp`, `signature`.

The receipt digest covers the canonical signed receipt; the attestation digest covers its
decoded payload. The entry does not contain either underlying document. Public entries have
no sequence, prompt, objective, transcript, structure, raw result or private-key bytes.

Session identifiers, counts, times and
digests can permit correlation. Identifiers are not guaranteed anonymous; avoid sensitive
names in free-form identifiers. Hashing is not encryption or a cryptographically hiding
commitment, and known candidate preimages can be checked against deterministic digests.

Privacy from the public is not privacy from the validating service or reviewer. Hosting the
current append interface unchanged would expose submitted records and supplied blobs to its
operator. Confidential validation with commitment-only public submission is a deployment
architecture that remains to be designed. Transport protection, organizational
access controls, operator trust and retention arrangements also need explicit deployment work.

Complete private review does not mean public disclosure of the notebook. Conversely, selecting
records does not preserve a claim of complete disclosure: withholding committed records yields
INCOMPLETE. Signed record fields cannot be silently redacted. A digest-only artifact does not
prove its availability or allow content review. No zero-knowledge review is implemented.

## Overview storyboard

| Slide | Target time | Purpose |
| --- | --- | --- |
| 1 | 0:00–0:25 | Introduce verifiable records for biological design. |
| 2 | 0:25–0:50 | Organization → authorized reviewer → review decision. |
| 3 | 0:50–1:20 | Identify recipients of private records and public metadata. |
| 4 | 1:20–1:45 | Actual clean fixture screenshot: consistent evidence. |
| 5 | 1:45–2:10 | Actual tampered fixture screenshot: altered evidence rejected. |
| 6 | 2:10–2:35 | Authenticated objectives can still warrant contextual scrutiny. |
| 7 | 2:35–3:00 | Membership versus complete disclosure of a committed session. |
| 8 | 3:00–3:25 | Private review: records, optional artifact bytes and visible gaps. |
| 9 | 3:25–3:55 | Exactly what the public ledger does and does not expose. |
| 10 | 3:55–4:20 | Real BenchBB work supports the product, including its null result. |
| 11 | 4:20–4:45 | Distinguish demonstrated mechanics from independent deployment. |
| 12 | 4:45–5:00 | Propose evaluating VATs in synthesis review. |

All frames are discrete 16:9 slides, never a continuous scroll. Green marks an integrity-check
pass, not a safety determination. A public-ledger vision is explicit; local proof mechanics
are not represented as an already deployed global service.

## Learning version

The detailed version has its own order.

| Slides | Story and teaching objective |
| --- | --- |
| 1–4 | Product promise, RAND's synthesis-provider problem, four protocol phases, proposal versus prototype. |
| 5–6 | Privacy audiences and RAND's four checks plus our additional disclosure-completeness check. |
| 7–11 | Clean, tampered, contextual-objective and partial-disclosure walkthrough; explain objective names without inferring malicious intent. |
| 12–14 | Backdating and independent observation, public disclosure, real recorded scientific work. |
| 15–18 | Record schema, canonical bytes, membership/completeness and a four-leaf proof diagram. |
| 19–24 | Trusted roles, portable verification, explicit lineage and graph, recovery, two distinct trees. |
| 25–27 | Our exact public schema, five-recipient disclosure map, confidential validation versus public export. |
| 28–30 | Scientific interpretation, audience discussion questions, proposed voluntary pilot and governance questions. |

RAND's proposal adds process evidence to existing sequence screening; it does not replace
screening. Its completeness requirement is broader than our extra check that every committed
record is disclosed. Our local notary applies no sequence screen. Caller-side capture and
local keys do not establish hardware-attested execution or independently authenticated identity.
Public observation can constrain backdating claims; publication is not exact execution time,
and our laptop supplies no independent timestamp. Exact public schemas and signed-lineage APIs
are implementation choices, not RAND-mandated formats.

The closing pilot is a proposed product direction, not a launched service or partner commitment.
Agree on confidential handling, trusted operators, review and escalation rules, retention and
appeals; measure evidence usefulness, review time, false flags and compliance burden.

The detailed notes preserve the current implementation distinctions: research events enter
session records, while scientific execution nodes can be embedded as separately signed
objects. Completeness, provenance validity, artifact availability, and scientific meaning
are separate judgments.

## Reproduction and sources

Proposal: Berke, Kilian, Griffin, Atanda and Vazquez,
[Verifiable Audit Trails for AI-Enabled Biological Design Tools](https://www.rand.org/pubs/perspectives/PEA4613-1.html),
RAND PE-A4613-1, June 2026. Chapters 3–4 ground the protocol and trust boundaries;
chapter 5 grounds governance and voluntary-pilot discussion. The deck paraphrases and links
the proposal; it does not reproduce the RAND PDF or imply RAND endorsement.

From the repository root, run separately; fixture nonzero exits are intentional:

```bash
PYTHONPATH=src .venv/bin/python -m demo.demo --fixture clean
PYTHONPATH=src .venv/bin/python -m demo.demo --fixture tampered
PYTHONPATH=src .venv/bin/python -m demo.demo --fixture doctored
PYTHONPATH=src .venv/bin/python -m demo.demo --fixture partial
PYTHONPATH=src .venv/bin/python scripts/transparency_demo.py
```

Fixture exits are 0, 2, 0, 6. The observer demonstrates 1 → 2 at log size 3 and rejection of
stripped required evidence with exit 4. The slides do not execute verification in the browser.

Optional operator exercises (not audience prerequisites): run the four fixtures, the observer
demo, and `scripts/provenance_demo.py --out .local/my-new-graph-demo` with `PYTHONPATH=src`
and the repository Python environment. Use a fresh output directory. Full provenance setup
and graph-viewer instructions are in `README.md#recording-computation-provenance`.

Privacy fields and admission behavior: `src/vatlog/transparency.py`,
`src/vatlog/notary.py`, `src/vatlog/bundle.py`, and `src/vatlog/verify.py`.
Product direction/readiness: `VISION.md`. Scientific numbers:
the BenchBB study paragraph in `README.md`. Reproducing the slides needs no paid execution.

Never distribute private workflow stores, keys, bundles or the separate RAND publication as
presentation assets.
