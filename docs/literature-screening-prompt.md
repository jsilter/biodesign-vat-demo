# VAT literature-screening prompt

Conduct a bounded, VAT-recorded literature-screening exercise for this question:

> **Among randomized, placebo-controlled trials published by March 31, 2022, does intranasal
> oxytocin improve hyperphagia in people with Prader–Willi syndrome?**

Search PubMed and Europe PMC using terms covering:

- “Prader-Willi syndrome”
- Intranasal oxytocin
- Randomized, placebo-controlled, crossover, or clinical trial
- Hyperphagia, appetite, hunger, or eating behavior

This is not a full systematic review or meta-analysis. The objective is to identify the
publications that should enter a later evidence review and to demonstrate that no retrieved record
silently disappears during screening.

Before screening, use VAT to record:

1. The exact query submitted to each database.
2. The database name and retrieval time.
3. The complete returned citation set.
4. Stable identifiers such as PMID, PMCID, or DOI.
5. The number of records returned before and after deduplication.
6. A canonical digest of the retrieved set.

Screen titles and abstracts using these criteria.

## Include

- Human participants with Prader–Willi syndrome.
- Intranasal oxytocin compared with placebo.
- Randomized parallel-group or crossover design.
- Publication date no later than March 31, 2022.
- A reported hyperphagia, appetite, hunger, or eating-behavior outcome.

## Exclude

- Reviews, meta-analyses, protocols, case reports, and observational studies.
- Studies without a placebo comparator.
- Studies of oxytocin analogues such as carbetocin rather than oxytocin.
- Studies without a relevant eating-behavior outcome.
- Studies published after the cutoff.

Assign every deduplicated record exactly one disposition: `included`, `excluded`, or `unresolved`.
Give a specific reason for every exclusion. Do not infer missing study details; mark the record
`unresolved` when the title and abstract are insufficient.

Produce:

- A concise PRISMA-style count from retrieval through inclusion.
- A table of included publications with citation, year, PMID or DOI, design, population,
  intervention, comparator, and relevant outcome.
- A table accounting for every excluded or unresolved record.
- A VAT completeness statement confirming whether every retrieved record received a disposition.
- A short statement of limitations.

Do not calculate a pooled effect, judge efficacy, dispatch Modal work, or claim that the databases
contain every relevant publication. VAT should establish completeness relative to the declared
search results, not completeness of the worldwide literature.
