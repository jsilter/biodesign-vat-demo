# Anthropic, "Claude accelerates protein design" (citation record)

**Published:** 2026-08-18. **Publisher:** Anthropic; no individual authors credited.
**URL:** https://www.anthropic.com/research/Claude-accelerates-protein-design
**Checked:** 2026-09-13.

The page carries no open licence (the footer points at Anthropic's terms of service), so only the
citation, the source URLs, and short quotations are recorded here. The prompts it released are CC BY
4.0; `protein_binder_protocol/README.md` cites and links them.

## What it released

- Prompts and design data: https://huggingface.co/datasets/Anthropic/claude-protein-binder-design/tree/main
- Protein design technical report: https://www-cdn.anthropic.com/30bf50e22a01388bb29bf077ee3f244531594b7a.pdf
- Chemical analysis technical report: https://www-cdn.anthropic.com/9f08da5189ac269b3242ca760de9823805c3f5f6.pdf

Quoted: "we are sharing the prompts we used for these campaigns, as well as all *in vitro* and
*in silico* data we generated."

## Reports cited (not redistributed)

Neither report states a licence, so the PDFs are not included here; fetch them from the URLs
above. SHA-256 of the copies read (fetched 2026-09-13):

- protein design technical report: 2361bb06922f18db55f76ad5da0ed6e6150df24b2aadbc30fd4ff3c54d32d4b0
- chemical analysis technical report: 1f1946b2b4379cfa10578f514b42f3b4fcd1dd3216e8ddca871cc7fa6eddc76e

## Why it is here

It is the announcement this project's agentic-capture question came from, and it is the source a
reader is most likely to have seen. Two things in it are worth holding against the prompt text,
because the prompts contradict the impression the page leaves:

- Autonomy: "This campaign was carried out with minimal human involvement beyond the information we
  provided Claude in our initial prompt", with humans "approving certain requests made by Claude
  (e.g., network access requests, code execution requests), resolving infrastructure issues outside
  of the protein design sessions, and ordering the generated designs for experimental validation."
- Structure: the page reads as a single autonomous agent. The released prompts specify a sub-agent
  hierarchy (`host.delegate`, a blocking SETUP sub-agent, standing CLOCK / BUDGET / WATCHDOG /
  SCRIBE / DISPATCH agents, one monitor per ten live jobs), and state that the campaign prompt is
  the system prompt "every sub-agent at every depth carries".

Results as reported on the page: 354 binders against 14 of 15 targets, from 1,320 designs.
