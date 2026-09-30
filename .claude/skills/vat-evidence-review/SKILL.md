---
name: vat-evidence-review
description: Inspect and verify local VAT demonstration fixtures or supplied VAT bundles without MCP, network access, signing, publication, or execution.
---

# VAT evidence review

Use this skill when the user wants to inspect VAT evidence, explain the five verification
checks, compare clean and falsified disclosures, or verify a local bundle. This is a read-only
review skill. It does not start scientific work, call Modal, write to a transparency log, load
private keys, or notarize evidence.

Run the deterministic helper with the repository-contained Python. From the repository root:

```bash
.claude-science-venv/bin/python \
  .claude/skills/vat-evidence-review/scripts/review_evidence.py list
.claude-science-venv/bin/python \
  .claude/skills/vat-evidence-review/scripts/review_evidence.py inspect clean
```

For owner-supplied evidence, keep both files under the repository and run:

```bash
.claude-science-venv/bin/python \
  .claude/skills/vat-evidence-review/scripts/review_evidence.py verify \
  path/to/bundle.zip --trust path/to/trust.json
```

Add `--sequence SEQUENCE` only when the owner supplies the sequence independently. Add
`--require-transparency` when the review must reject a notarized bundle whose local
transparency evidence was stripped.

Report the ordered five checks by their returned names and details. Distinguish `OK`,
`TAMPERED`, and `INCOMPLETE`; an incomplete disclosure is not necessarily altered. Treat
provenance as a separate report. Local signatures establish integrity relative to the supplied
public keys. They do not prove independent execution, experimental validity, safety, execution
time, or the absence of off-ledger work.
