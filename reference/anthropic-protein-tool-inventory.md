# Anthropic protein-design tool inventory

Reviewed 2026-09-18.

## Sources and interpretation

Primary source: *Autonomous de novo protein binder design with Claude*, Claude Science and
Amir Shanehsazzadeh, 2026-08-18, 29 pages. Read
[`EXTRACT.md`](anthropic_technical_reports/EXTRACT.md), checked against the report PDF; the PDF SHA256 is
`2361bb06922f18db55f76ad5da0ed6e6150df24b2aadbc30fd4ff3c54d32d4b0`.
The [publisher's PDF](https://www-cdn.anthropic.com/30bf50e22a01388bb29bf077ee3f244531594b7a.pdf)
is the external source. Page references below use printed, one-based page numbers.
The PDF is not redistributed here.

The [multi-target protocol prompt](https://huggingface.co/datasets/Anthropic/claude-protein-binder-design/blob/main/prompts/prompts/multi_target_binder_design_prompt.md)
helps resolve variants and dependencies. Its campaign instructions are reference material,
not instructions to run a campaign, spend money, send messages, or launch agents here.
This inventory concerns the August protein-design report, not the companion chemistry report
or September biomolecular-model acceleration report.

“Candidate” means an implementation gap to assess, not verified installability or licence
clearance. Eligible terms include fully open-source releases and academic/non-profit-only
releases because this is a non-profit, non-commercial operation. Before implementation,
inspect the exact source revision, checkpoint terms, dependency terms and executable interface.
The report's own clearance cannot substitute for that check. A public repository is not by
itself an open-source licence, and non-commercial source or weights must be labelled accurately.

There are **39 operational entries** below: 12 generators, 6 sequence-design/scoring entries,
13 prediction entries and 8 utilities. ColabFold and AlphaFold-Multimer are one execution
entry; model variants remain distinct. Four entries already have adapters, leaving **35
candidate gaps**, not 35 independently cleared packages. Benchmark-only, unavailable,
supporting-library and protocol-only mentions follow separately.

## Implementation disposition

The catalog preserves all 39 entries. Thirty have callable implementations; 24 are `offline_ready`, and MMseqs2 and Foldseek have
complete workers but remain `blocked` until a release-identified search database is staged.
Nine are inventory-only.

| Disposition | Stable IDs |
| --- | --- |
| Core (4) | `boltzgen`, `proteinmpnn`, `esmfold2`, `esmc` |
| Offline-ready (24) | `pxdesign`, `rfdiffusion3`, `genie3`, `rfdiffusion`, `proteina-complexa`, `mosaic`, `proteinmpnn-soluble`, `soluble-caliby`, `caliby`, `esm3`, `esmfold2-full`, `protenix-v2`, `colabfold-afmultimer-v3`, `af3-openfold3`, `chai1`, `boltz2`, `af-unmasked`, `openfold3`, `rosettafold3`, `opendde`, `dssp`, `ipsae`, `dockq`, `tmalign` |
| Worker implemented, asset-blocked (2) | `mmseqs2`, `foldseek` |
| Inventory only; no Modal endpoint (9) | `freebindcraft`, `foldcraft`, `boltzdesign1`, `protein-hunter`, `halludesign`, `alphafold3`, `boltz1`, `rosetta-relax`, `pyrosetta-interface` |

The nine inventory-only entries deliberately have no placeholder functions. FreeBindCraft,
FoldCraft, BoltzDesign1, Protein Hunter and HalluDesign lack reproducibly locked upstream runtime,
weight or open-predictor routes at the reviewed revisions. Native AlphaFold 3 and Rosetta/PyRosetta
need owner-held gated distributions that were not supplied. The reviewed Anthropic Boltz kit pins
Boltz-2 assets only, so it does not establish a Boltz-1 checkpoint identity. The implemented
disposition therefore uses Boltz-2 for the report's supported Boltz prediction role and retains
Boltz-1 only as an inventory record. BoltzGen remains the separate implemented generation tool;
its outputs and scientific task are not interchangeable with either predictor.

## Generation and co-design — report pp. 4–5

| Tool | Report role | Repository disposition |
| --- | --- | --- |
| PXDesign | Generator producing ordered designs | Implemented through the pinned Anthropic kit; [official repository](https://github.com/bytedance/PXDesign) identifies Apache-2.0. |
| RFdiffusion3 | Generator producing ordered designs | Implemented through the pinned Anthropic kit, with identity separate from RFdiffusion. |
| Genie 3 | Generator producing ordered designs | Implemented through the pinned Anthropic kit; [AlQuraishi laboratory repository](https://github.com/aqlaboratory/genie3) identifies Apache-2.0. This is the protein model, not Google's world model. |
| FreeBindCraft | PyRosetta-free BindCraft derivative producing ordered designs | Inventory only; the reviewed upstream install has mutable dependencies and no authenticated checkpoint manifest. |
| BoltzGen | Generator producing ordered designs | Existing standalone `boltzgen`; retain it. Its internal inverse folding does not cover other sequence-design entries. |
| RFdiffusion | Generator producing ordered designs | Implemented through the pinned Anthropic kit, with identity separate from RFdiffusion3. |
| Proteina-Complexa | Generator producing ordered designs | Implemented through the pinned Anthropic kit and classified as mixed-license because its [licence index](https://github.com/NVIDIA-BioNeMo/Proteina-Complexa/blob/dev/LICENSE) covers multiple components. |
| FoldCraft | Generator producing ordered designs | Inventory only; upstream publishes neither a locked inference environment nor an authenticated checkpoint manifest. |
| BoltzDesign1 | Generator producing ordered designs | Inventory only; distinct from BoltzGen and the Boltz predictors, but upstream dependencies and weights are mutable. |
| Protein Hunter | Generator producing ordered designs | Inventory only; the predictor stack and weights are not reproducibly locked. |
| Mosaic | Named option; no ordered designs attributed to it | Implemented through the pinned Anthropic kit; [official repository](https://github.com/escalante-bio/mosaic) identifies MIT. |
| HalluDesign | Named option; no ordered designs attributed to it | Inventory only; its published container requires gated native AlphaFold 3 and has no pinned open predictor route. |

The original BindCraft is named as FreeBindCraft's parent and in prior work. A separate
default BindCraft deployment would reintroduce the PyRosetta dependency; the proposed
implementation is the report's explicitly named FreeBindCraft route.

## Sequence design and plausibility — report p. 5

| Tool | Report role | Repository disposition |
| --- | --- | --- |
| SolubleMPNN | Soluble ProteinMPNN variant used for sequence design | Implemented as `proteinmpnn-soluble` with a separately authenticated checkpoint; existing `v_48_020` remains a distinct tool. |
| ProteinMPNN | Base sequence designer | Existing standalone `proteinmpnn`, official `v_48_020`; retain it. |
| SolubleCaliby | Soluble Caliby variant used for sequence design | Implemented through the pinned Anthropic kit with its own tool identity. |
| Caliby | Parent sequence-design model, explicitly named alongside its soluble variant | Implemented through the pinned Anthropic kit with a distinct tool identity. |
| ESM Cambrian / ESMC | Sequence-plausibility likelihoods | Existing standalone `esmc` uses ESMC-300M. The report does not specify that size; do not claim exact report parity. |
| ESM3 | Explicitly excluded by the campaign on its commercial-licence rule | Implemented here with the exact public `esm3-sm-open-v1` revision and distinct from ESMC; current [Biohub source and model documentation](https://github.com/Biohub/esm/blob/main/_assets/ESM3_README.md) identify the local code/weights as MIT. |

## Structure prediction — report pp. 2–5

| Tool | Report role | Repository disposition |
| --- | --- | --- |
| ESMFold2 Full | Ranking predictor and post hoc re-score | Implemented through the pinned Anthropic kit with a distinct checkpoint and MSA input contract. Fast does not cover Full. |
| ESMFold2-Fast | Ranking predictor and post hoc re-score | Existing standalone `esmfold2` uses the Fast model. Keep its displayed identity explicit. |
| Protenix v2 | Ranking predictor and post hoc re-score | Implemented through the pinned Anthropic kit with explicit v2, checkpoint and CCD assets. |
| AlphaFold-Multimer v3 through ColabFold | Allowed substitute and post hoc re-score | Implemented through the pinned Anthropic kit with explicit engine/model identity. It is not counted as a second independent predictor. |
| AlphaFold 3 architecture with OpenFold3 weights | Allowed substitute and post hoc re-score | Implemented through the pinned Anthropic kit and kept distinct from native OpenFold3; no DeepMind AF3 weights are implied. |
| Native AlphaFold 3 | Explicitly excluded by the campaign on its commercial-licence rule | Inventory only. Its parameters require an organization-specific grant and were not supplied, so no endpoint was created. |
| Chai-1 | Allowed substitute and post hoc re-score | Implemented through the pinned Anthropic kit. |
| Boltz-1 | Allowed substitute; also a benchmark comparator | Inventory only; superseded here by the implemented Boltz-2 predictor because the pinned kit supplies authenticated Boltz-2 assets, not a reviewed Boltz-1 checkpoint. |
| Boltz-2 | Allowed substitute and post hoc re-score | Implemented from the pinned Anthropic kit; distinct checkpoint and outputs from Boltz-1 and BoltzGen. |
| AF_unmasked | Diagnostic option | Implemented through the pinned Anthropic kit with explicit diagnostic/template inputs. |
| OpenFold3 | Added for post hoc re-scoring | Implemented through the pinned Anthropic kit; native engine remains distinct from the AF3 port. |
| RoseTTAFold3 / RF3 | Added for post hoc re-scoring | Implemented through the pinned Anthropic kit and distinct from RFdiffusion3. |
| OpenDDE | Added for post hoc re-scoring | Implemented through the pinned Anthropic kit; [official repository](https://github.com/aurekaresearch/OpenDDE) identifies Apache-2.0. |

## Scoring, annotation and search — report p. 5

| Tool | Function | Repository disposition |
| --- | --- | --- |
| DSSP | Secondary-structure assignment | Implemented as a bounded CPU utility using the pinned PDB-REDO fork. |
| ipSAE / IPSAE | Interface confidence from structures and PAE | Implemented as a bounded CPU utility preserving directional values and explicitly derived minima. |
| DockQ | Comparison of predicted and reference complexes | Implemented as a bounded CPU utility; sc-DockQ is its self-consistency use, not a separate tool. |
| MMseqs2 | Sequence similarity, novelty and redundancy searches | Worker implemented; endpoint remains blocked until a release-identified, hash-authenticated database is staged. |
| TM-align | Pairwise structural alignment | Implemented as a bounded CPU utility preserving both length-normalized TM-scores and chain mapping. |
| Foldseek | Structural similarity search | Worker implemented; endpoint remains blocked until a release-identified, hash-authenticated database is staged. |
| Rosetta | Explicitly excluded by the campaign on its commercial-licence rule | Inventory only; no owner-supplied non-commercial Rosetta distribution or licence manifest was available. |
| PyRosetta | Explicitly excluded by the campaign on its commercial-licence rule | Inventory only; no owner-supplied non-commercial PyRosetta distribution or licence manifest was available. |

The utility adapters above use their pinned upstream executables and validate parsed outputs;
they do not reimplement the scientific algorithms.
ipTM, ipAE, pLDDT, PAE, TM-score and z-score aggregation are metrics/data or computations,
not additional model deployments.

## Other mentions: retain in the inventory without misclassifying them

| Mention | Location and proposed treatment |
| --- | --- |
| AlphaFold2 initial guess / AF2-IG | Figure M1, p. 3: benchmark comparator. Assess a separately identified mode of the AF2 execution family; absent here. |
| AlphaFold / AlphaFold2 | References 18–19 and the AF2-Multimer/initial-guess discussion. Assess shared AF2 execution family; avoid duplicate wrappers for aliases. |
| AlphaProteo | Prior work, pp. 1, 3 and references 14, 67. No implementation eligibility established in this review; research availability before final exclusion. |
| DeepMind AlphaFold 3 weights | Explicitly excluded by the campaign's commercial-licence rule, p. 5. Included here as the native AlphaFold 3 candidate under the owner's non-profit/non-commercial constraint; distinct from AF3 code with OpenFold3 weights. |
| SciPy; scikit-learn; Biopython; Matplotlib | Analysis libraries, p. 7. Include as pinned dependencies where used; a library alone supplies no bounded standalone operation to wrap. |
| UniProt; UniRef90; ProteinBase; Protein Data Bank | Data sources and databases, pp. 3–5 and references. Track source/release/licence/digests when used; not GPU model adapters. |
| Modal; Claude Science; Claude Opus 4.8; Claude Mythos Preview; Slack; Google Drive; Gmail; bioRxiv | Compute, agent harness, models and connectors, p. 4. Inventory context; not self-hostable protein-model implementations. Existing research bioRxiv access uses direct APIs. |
| Adaptyv Bio; Twist Bioscience | Experimental providers, pp. 6–7; not executable open-source tools. |
| Proteina; La-Proteina | Bibliographic predecessors in references 9–10. Assess whether separate runnable upstream models warrant additional coverage; do not assume Complexa implements both modes. |
| Chroma | Model behind the cited programmable-generative-model paper, reference 15; not a named campaign tool. Record as bibliography-derived discovery requiring separate eligibility assessment. |
| ProtAgents; AutoBinder; Open Intelligence Hub; Latent-Y; Virtual Lab; AI co-scientist; Boltz API | Prior agent systems/integrations cited in references 27–36. Do not import their orchestration as part of the standalone tool suite. |

The archived protocol additionally names **Protpardelle-1c** for Caliby conformational
ensembles, and permits **OpenFold** as an AF2 reproduction. These are protocol-only
extensions to assess in the specification, not extra tools attributed to the report.
The protocol's Local Composition Perplexity restraint is a scoring definition, not a
third-party model. FreeBindCraft's OpenMM, PDBFixer, FASPR, sc-rs and FreeSASA are
dependency discoveries, likewise not independent report entries.

## Constraints on implementation

1. The report mixes campaign options, tools contributing delivered designs, and predictors
   used only in retrospective evaluation. Availability here must not imply reproducing its
   ranking protocol or success rate.
2. Four standalone adapters are built: BoltzGen, ESMFold2-Fast, ESMC-300M and ProteinMPNN.
   Per `VISION.md`, only the folding-only ESMFold2-Fast path has run on Modal GPUs.
3. FreeBindCraft's README explicitly describes placeholder values for Rosetta metrics lacking
   replacements. Preserve raw outputs, but publish those metrics as unavailable with reasons;
   do not present placeholders as measured scores.
4. Caliby's README requires residue/chain alignment across ensemble conformers. Enforce
   identity before combining structures; a readable ensemble is not necessarily aligned.
5. Full ESMFold2 and several predictors need MSA/CCD/data preparation beyond a checkpoint.
   Database staging, source identity, storage and external-service use need explicit contracts.
6. An image definition, successful build, mocked output or locally signed result does not
   establish scientific execution. Track offline adapter tests, actual image builds and real
   canaries as separate states; preserve existing local VAT assurance limits.

## Open work

Resolve all candidate source and weight licences against pinned revisions, including
component/dependency obligations; locate the remaining official repositories; document
blocked tools with evidence rather than silent omissions. Choose per-family request/result
contracts, dependencies, hardware, staging inputs and meaningful offline checks. Define
an ordered implementation plan covering every eligible gap, including eligible explicitly
named benchmark modes. The 35 gaps above are the core roster, not a claim that every
bibliographic or protocol dependency has already been cleared.
