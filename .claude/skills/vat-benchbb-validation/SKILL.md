---
name: vat-benchbb-validation
description: Recalculate the frozen BBF-14 panel's published-score comparison locally, without MCP, network access, model inference, or paid execution.
---

# VAT BenchBB validation

Use this skill to validate the bounded BBF-14 result used in the VAT demonstration. It analyzes
the committed 12-design panel and the published `ipsae_min_ef2fast` values. It does not rerun a
protein model, reproduce the full BenchBB study, establish biological validity, or start Modal.

From the repository root, run:

```bash
.claude-science-venv/bin/python \
  .claude/skills/vat-benchbb-validation/scripts/validate_panel.py
```

The helper validates the panel contract, reports the file digest and frozen source revision,
then calculates group summaries, rank AUC, and the exact two-sided label-permutation p-value.
Present the result as a verification walkthrough, not as evidence that the published score is a
useful classifier. Explain that the panel is deliberately small and label-enriched and that the
calculation uses published scores rather than fresh model inference.

Do not generalize beyond this panel. Preserve the returned score direction (`higher_is_better`),
sample counts, and all 220 permutations. If the panel digest or contract differs, stop and report
the mismatch rather than substituting another file.
