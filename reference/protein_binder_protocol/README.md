# Anthropic's protein-binder-design protocol prompts (notes)

**Source:** Anthropic, *claude-protein-binder-design* dataset, Hugging Face, 2026,
<https://huggingface.co/datasets/Anthropic/claude-protein-binder-design>, subpath `prompts/prompts/`.
The prompt discussed here is
[`multi_target_binder_design_prompt.md`](https://huggingface.co/datasets/Anthropic/claude-protein-binder-design/blob/main/prompts/prompts/multi_target_binder_design_prompt.md).
**Licence:** CC BY 4.0 (dataset README, line 181: "Data and documentation: CC BY 4.0"). Cite as given
in the upstream `CITATION.cff`, which ships inside each release archive.
**Retrieved:** 2026-09-12. At retrieval, all 19 markdown files of the prompt release matched the
upstream `MANIFEST.sha256`, 0 mismatches. The prompts are not copied into this repository; fetch
them from the dataset.

## Why it is here

These prompts are the primary source for what an agentic design campaign actually emits, which is
what a VAT recorder would have to capture: files frozen before outcomes exist and carrying their own
digests (`lib/submit_gate.py` with its `gate_sha256`, `/state/method_vocab.json`,
`/state/gates/{target}.json`), append-only ledgers (`/ledger/deviations.jsonl`), and a dispatch
surface where every container start is authorized one at a time (`submit_gate()`, Modal sandboxes).
Claims here about that workflow can be checked against the prompt text rather than against the announcement (cited in `../anthropic-protein-design-article.md`), which
describes the campaign as a single autonomous agent and omits the sub-agent structure the prompts
specify.

## What the release contains

The multi-target prompt (14 targets, 48 h / $50k, 16,281 words; the "16,000-word prompt" of the
secondary reading below) is the whole protocol. The upstream `README.md` in `prompts/` records what
was redacted for release. The rest of the release: 16 single-target prompts (24 h / $10k, one per
target from a shared template, and close variants of each other: `EGFR.md` and `PD-L1.md` differ on
42 of 265 lines, `Mature GDF-8.md` and `Latent GDF-8.md` on 6); the kickoff messages that open a
campaign at T0; `MANIFEST.sha256` and `REDISTRIBUTION_NOTES.csv`; the 1.16 GB `corpus/` zip (317
third-party sources, each keeping its own terms; `source_url` in the upstream index is
authoritative); and two referenced figures.

## Where the recording surface sits in the text

Term counts in `multi_target_binder_design_prompt.md`: `ledger` 38, `submit_gate` 26,
`root_backbone_id` 7, `sha256` 5, `provenance` 3, `frames table` 2, `method_vocab` 2,
`append-only` 1.

Four clauses that define what there would be to record, quoted verbatim from the source:

- Precommitment: "freeze and disclose the formula BEFORE scoring any production designs on that
  target."
- Enumerated identifiers rather than free text: "structure_method and seq_method are NOT free text:
  they are tokens from the campaign-frozen method_vocab.json".
- Provenance keys propagated across stages: "Every generation arm assigns root_backbone_id (one id
  per de novo backbone, shared by every derived sequence and partial-diffusion variant) and
  structure_method at backbone-generation time and propagates both unchanged through all downstream
  stages."
- Append-only deviation log: `/ledger/deviations.jsonl`, "an append-only file and never overwritten
  in place."
- One record per tool dispatch: `/ledger/job_metadata/{launching_frame_id}.jsonl`, "one append per
  dispatch, one file per launching frame, never a single shared file: {job_id, campaign, target,
  launching_frame_id, submitted_at_utc, gpu_type, est_hourly_usd}", written "immediately after
  submit_gate() returns True, on EVERY dispatch path".

One correction to the secondary reading, recorded so it is not propagated. Dylan Bourgeois's
"Anatomy of a protocol prompt" (https://dtsbourg.me/en/articles/anatomy-of-a-protocol-prompt)
describes the prompt as enumerating "degenerate solutions"; the word `degenerate` does not appear in
the prompt at all. The corresponding passage is headed **target-fold mimics** and reads: "A
structure generator conditioned on the full target complex, with a requested binder length close to
a target chain's, may reproduce that chain's fold (from the training data a protomer is an excellent
binder of its oligomeric neighbors); this is most acute on homo-oligomeric targets." The mechanism
is as the article describes it; the label is the article's, not Anthropic's.

## Caveat on transfer

These prompts assume the Claude Science agent harness (`host.delegate`, `host.compute`,
`submit_gate`, `wait_for_notification`), a Slack channel, a Google Drive deliverables folder and an
operator-funded Modal account. A recorder capturing a campaign like this one would have to sit on
those surfaces; anything built against a reimplementation records the reimplementation, and should
say so.
