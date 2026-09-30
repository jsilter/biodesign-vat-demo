---
name: vat-protein-workflows
description: Preview, dispatch, record, sign, publish, and verify bounded protein workflows directly, including explicitly authorized Modal execution; MCP is not required.
---

# VAT protein workflows

Use this skill for the end-to-end VAT demonstration: inspect the registered tools, preview the
frozen BenchBB operation, dispatch an explicitly authorized Modal call, preserve its execution
identity, finalize signed evidence, append its notarized commitment to the simulated local public
log, and verify the resulting bundle.

This skill invokes the repository implementation directly. The upload ZIP includes the VAT,
runner, and demonstration source used for canonical hashing, Ed25519 role signatures, Merkle
proofs, notarization, transparency publication, recovery, provenance, and verification. It does
not use the VAT MCP connector.

## Commands

Run the helper with the repository-contained environment. From the repository root:

```bash
.claude-science-venv/bin/python \
  .claude/skills/vat-protein-workflows/scripts/vat_operator.py catalog
.claude-science-venv/bin/python \
  .claude/skills/vat-protein-workflows/scripts/vat_operator.py benchbb-preview
```

After showing the preview, obtain explicit owner approval for the displayed application, tool,
new-call limit, output directory, and compute budget. Then dispatch at most that approved work:

```bash
.claude-science-venv/bin/python \
  .claude/skills/vat-protein-workflows/scripts/vat_operator.py benchbb-run \
  --tool esmfold2 --max-new-calls 1 --compute-budget-usd 2.00 --authorized
```

`--authorized` enables the existing `--execute-paid` path; without it the helper refuses to
dispatch. A retry resumes the durable execution identity and must not create a replacement call.
Do not increase an approved limit or add a tool without renewed approval.

After at least one completed fold outcome:

```bash
.claude-science-venv/bin/python \
  .claude/skills/vat-protein-workflows/scripts/vat_operator.py benchbb-finalize
.claude-science-venv/bin/python \
  .claude/skills/vat-protein-workflows/scripts/vat_operator.py benchbb-publish
```

Finalization creates the signed complete, partial, and tampered bundles. Publication adds the
notary receipt and a recoverable inclusion record in the laptop-local transparency log, leaving
the pre-publication bundle unchanged. Preserve the output directory, its private recorder state,
role keys, publication database, and transparency database together.

## Assurance boundary

Modal execution is real when dispatch succeeds, but the local recorder observes and signs the
returned bytes; it is not Modal platform attestation. The simulated notary and transparency-log
operator are not independent. VAT evidence establishes integrity, declared provenance, and
detectable post-record alteration relative to its keys. Scientific acceptance, experimental
binding, safety, execution time, and absence of off-ledger work remain separate claims.

Read [assurance.md](references/assurance.md) before explaining a result. Read
[workflow-reference.md](references/workflow-reference.md) only when composing a registered or
generated workflow outside the frozen BenchBB demonstration.
