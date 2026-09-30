# Anthropic technical reports, 2026-08-18: extract

Both reports accompany https://www.anthropic.com/research/Claude-accelerates-protein-design (see
`../anthropic-protein-design-article.md`). The report PDFs are not redistributed here; fetch them from the URLs in
`../anthropic-protein-design-article.md`. This file stands alone, the way
`reference/rand-vat-spec-extract.md` stands in for the RAND paper.
Quotation is kept short because neither report states a licence. Page counts and figures below are
from the PDFs as fetched on 2026-09-13.

---

## 1. Autonomous de novo protein binder design with Claude

29 pages. Claude Science and Amir Shanehsazzadeh, Anthropic. Dated 2026-08-18.

### How the campaigns were run

One protocol prompt of about 16,000 words, "loaded as the system prompt of every agent, together
with a short kickoff message". By word count (Figure M2) the prompt divides into science and tooling
34.2%, orchestration and verification 34.7%, operations 31.1%, so two thirds of it is not science.
The report describes that two thirds as "how to pace a fixed GPU budget against the wall clock, how
to delegate work to a two-layer team of sub-agents and supervise it, how to verify results before
reporting them, and when to report".

Those sections "were iterated over test campaigns until Claude reliably sustained 24- to 48-hour
campaigns and used the full budget as intended, and were then frozen before the campaigns reported
here."

Formats: 48 hours and USD $50,000 for a 14-target campaign, 24 hours and USD $10,000 for a
single-target campaign, 30 designs per target either way, GPUs on Modal. Models were Claude Opus 4.8
at maximum reasoning effort and Claude Mythos Preview at high.

Operator setup: a funded Modal account with a per-campaign container limit, connectors to Slack,
Google Drive, Gmail and bioRxiv, a network-domain allowlist, credentials, and an offline reference
corpus. No design tool was pre-installed; the prompt has Claude build each from its public
repository and validate it in the first hour. During a campaign the operator watched dashboards and
approved access prompts "that carried no scientific content". Beyond that: "The only human messages
that entered any campaign were short, non-technical instructions to resume after a session had died
for infrastructure reasons."

### Results

1,320 designs on 15 targets (the mature GDF-8 dimer aggregated and its 120 designs are excluded).
354 bound, a 27% hit rate; among designs ranked first for each target in each campaign, 49% bound.
On RBX1, 28 of Claude's 90 designs bound, against 9 of 245 in a prior open competition, and the
tightest had K_D 3.9 nM against the competition winner's 45 nM re-measured on the same plate. Of 233
binders tested against the mouse ortholog, 130 also bound it.

### The parts that bear on audit trails

- **Design-time scores are not comparable.** "Confidence values recorded during a campaign came from
  whichever predictors, settings, and target constructs Claude chose at the time, so they may not be
  comparable across campaigns or targets." After the campaigns closed they re-scored every ordered
  design under one protocol with ten co-folding predictors, five runs each. The campaign log records
  what was run; it does not by itself yield a comparable measurement.
- **Per-design provenance is the released decision record.** Tool counts "are taken from the
  released per-design provenance, which records the tools that produced each of the 1,315 tested
  designs", and the release "offers a record of Claude's design decisions for those who wish to
  study how an AI agent conducts such work."
- **That capture is incomplete, and the report says so.** The data release gives, per design, the
  sequence and the design model, the latter "retained by the pipeline for 1,309 of the 1,440
  designs". 131 designs shipped without the model that produced them.
- **Redaction happened at release, not at capture.** The prompt release carries "credentials and
  internal identifiers redacted".
- **Blinding of the external check.** "Orders were placed with sequences shuffled and with no
  indication of model, campaign, or rank", and neither CRO saw the other's data.

### Stated limitations

Evidence is binding, not structure or function; no design was solved structurally, so "every pose in
this report is a prediction". Designs are counted per sequence though variants of one backbone are
not independent (counting one sequence per backbone: 200 of 809 bound, 24.7%). Each combination of
model, format and target ran once, so "model, format, and run-to-run variation are therefore
confounded, and we have described campaigns rather than models". Most targets are well characterized
in the literature.

### Availability

Three archives at https://huggingface.co/datasets/Anthropic/claude-protein-binder-design, "under CC
BY 4.0 for data and the MIT license for scripts". The prompt archive is cited in
`../protein_binder_protocol/README.md`.

---

## 2. Automated processing of raw NMR and LC-MS data with Claude Opus 5 on Claude Science

9 pages. David Kamber and Claude Science, Anthropic. Dated 2026-08-18.

One routine CRO quality-control sample, two independent concurrent sessions, raw proprietary
instrument files plus prompts "of one to three short sentences". The comparison standard is the
CRO's own work: "Claude Science's output is judged against the CRO's own processing of the same
acquisition, preserved in its audit trails and analysis report." That audit trail records an
operator opening the dataset 11 minutes after acquisition and spending 1 min 49 s on phasing,
baseline correction, peak picking and twenty integral regions.

**NMR.** 91 tool calls for the first answer, returning an 18-signal peak table summing to 19.8 H,
phased to ph0 = 61.6 degrees, plus an unprompted recommendation to run a D2O shake. Given the
exchange spectrum, its first comparison claimed four exchangeable signals removed. Then: "Its
built-in auditor showed that the headline contradicted Claude Science's own comparison table and
that the table rested on a superseded row set", and it corrected itself to "Two regions clear, not
four". Where peak table and operator integrals can be matched they agree within 0.08 H.

**LC-MS.** 60 tool calls, starting from an undocumented vendor binary with no parser available. It
identified an OLE2 container, found the MS and PDA data among 281 streams, and "accepted the decode
only after its internal checks passed": summed ion intensities reproduced the vendor-stored
total-ion chromatogram "exactly, to the integer, for all 2,664 scans", and all 3,751 PDA blocks
parsed with exact byte consumption.

**Two purity numbers from one file.** Claude reported 96.4% over three peaks from 0 min; the CRO
reported 96.33% over seven peaks from 3.46 min; computed on the CRO's basis, Claude's own areas give
98.8%. The number is not interpretable without the peak set it was computed over.

Claude stated three limitations itself: a single quadrupole gives nominal mass only, the
intensity-to-mAU scaling was inferred rather than documented, and the vendor's "Max Plot" stream did
not decode, so the max-absorbance chromatogram was recomputed from the validated absorbance matrix.

A figure annotation became a prompt, answered "29 s and one tool call later" from the underlying
data rather than the pixels.

---

## 3. Why these are kept here

Four things carry into this repo's design, and none of them require accepting the reports' claims:

1. **The leaf unit is the tool call.** The chemistry report counts 91, 60, and 1 tool calls per
   answer. Those are the events a recorder would hash, and the counts show the scale is tractable
   for one session and not for a 48-hour campaign.
2. **Recording what was run is not the same as recording a comparable result.** The protein report
   had to re-score every design post hoc because design-time scores came from instruments the agent
   chose mid-campaign. A VAT bundle faces the same limit and should not imply otherwise.
3. **Capture gaps are normal and should be visible.** 131 of 1,440 designs reached the release
   without the model that produced them. A verifier that reports missing capture explicitly is
   reporting something that occurs in practice, not a hypothetical.
4. **An internal auditor caught a false summary.** The NMR headline contradicted the table it was
   drawn from, over a superseded row set. That is the failure a signed chain addresses directly: not
   a malicious edit, but an assertion that no longer matches the data it came from.
