# Lit note — selective disclosure has a solved-in-principle core and an unsolvable-by-crypto residual

Literature note supporting the completeness discussion in `VISION.md`.
Purpose: the "each record honest, the SET incomplete" gap described in `VISION.md` is a
named problem in ≥3 mature literatures. It is
unsolved at the *record/invocation* layer only. Committing to the set (its
cardinality / a search-tree root) BEFORE results are seen, then proving completeness
against that commitment, is worked machinery. Maps onto three mitigation directions:
(a) commit to a search tree, (b) commit to a run count or monotonic counter, and
(c) accept that the negative is unsolvable at the record layer and rely on a reviewer. Every claim below carries an author/venue/year; spot-check page ranges
before quoting them in anything public.

## The problem, field-neutral
Prover runs N candidate procedures, logs all N faithfully (valid hashes/signatures),
submits an audit bundle for 1. Every record is honest; the SET is partial. The
omission lives *between* records, so per-invocation logging cannot express it.

## Headline (what the three literatures agree on)
Cryptography proves **positives** and can **bind the set up front**; it cannot prove
the **negative** ("no other runs happened").
- Provable and buildable: membership / non-membership against a prior commitment,
  append-only consistency, split-view prevention (with witnesses), and binding N +
  node identity before results exist so the committed set cannot be padded with decoys.
- NOT provable by any primitive: that the prover did not run an (N+1)th procedure
  entirely outside the commitment and never mention it. Completeness is always
  *relative to the commitment the prover chose to reference*.
- The negative is reachable only by a **chokepoint**: a mandated execution/logging
  path (policy) or trusted-hardware attestation (relocates trust to a TEE). All three
  fields independently land here. This is the honest ceiling, and the thing to say
  plainly in any writeup.

---

## 1. Cryptographic core — completeness against a commitment (ZK sets, accumulators, VCs)

**Zero-knowledge sets are the WRONG primitive here.**
Micali-Rabin-Kilian ZK sets ([FOCS 2003](https://dblp.org/rec/conf/focs/MicaliRK03.html))
and mercurial commitments ([Chase-Healy-Lysyanskaya-Malkin-Reyzin, EUROCRYPT 2005](https://cs.brown.edu/~mchase/papers/merc.pdf))
are engineered so proofs leak *nothing about |S|*. Anti-omission needs the cardinality
*public and bound*, the opposite. Same disqualifier applies to zero-knowledge
accumulators ([Ghosh-Ohrimenko-Papadopoulos-Tamassia-Triandopoulos, ASIACRYPT 2016 / ePrint 2015/404](https://eprint.iacr.org/2015/404.pdf))
unless you specifically need to hide the withheld candidates.

The right tools:
- **Non-membership stops secret APPENDS, not omission.** Universal RSA accumulators
  ([Li-Li-Xue, ACNS 2007](https://www.cs.purdue.edu/homes/ninghui/papers/accumulator_acns07.pdf))
  prove x ∉ S. Omission is withholding a y that IS in S; non-membership says nothing
  about it. So non-membership is not the lever.
- **The construction that works is a fixed-structure commitment with a bound number of
  positions, committed before results.** Either (a) a Merkle tree over exactly N
  record-hashes where N is part of the committed root, or (b) a **vector commitment**
  ([Catalano-Fiore, PKC 2013 / ePrint 2011/495](https://eprint.iacr.org/2011/495.pdf))
  whose position-binding fixes "slot j = h_j" and whose length is a public parameter.
  Completeness is then proven by **reconstruction/accounting**: reveal all N leaf
  hashes (cheap; leaks only that N runs happened), recompute the root, check it equals
  the pre-committed value and that exactly N leaves are present. Omitting a committed
  leaf changes the root, so it is caught. **Batch/subvector openings**
  ([Campanelli et al., CCS 2022 / ePrint 2021/1672](https://eprint.iacr.org/2021/1672.pdf))
  make "reveal all N" succinct if N is large.
- **The systems-grade precedent is Certificate Transparency's data structure:**
  append-only authenticated dictionaries
  ([Tomescu-Bhupatiraju-Papadopoulos-Papamanthou-Triandopoulos-Devadas, CCS 2019 / ePrint 2018/721](https://eprint.iacr.org/2018/721),
  [code](https://github.com/alinush/aad-paper)) give membership + non-membership +
  append-only all polylog. This is the closest 1:1 match to "prover must not secretly
  omit/append records."

**Load-bearing requirement:** you cannot prove "1-of-N with N−1 withheld" unless N (or
a structure pinning N) was fixed before results were known. That is direction (b)
made precise. What crypto still cannot give: completeness is relative to *this*
commitment; a second, unpublished commitment over a different batch is invisible
without a single public append-only anchor (→ §2, §5).

---

## 2. Transparency logs — three guarantee tiers, and where each stops

CT ([RFC 6962, 2013](https://www.rfc-editor.org/rfc/rfc6962.html);
[RFC 9162 / CT 2.0, 2021](https://www.rfc-editor.org/rfc/rfc9162.html)) is the deployed
version of exactly this fight. The taxonomy that matters:

- **Tier A — append-only Merkle proofs, one log, no help.** Inclusion (O(log n) audit
  path) and consistency (append-only prefix) proofs. Stops retroactive edit/deletion.
  Efficient *non-membership* needs an ordered/prefix tree (the CONIKS contribution),
  not a plain chronological 6962 log. **Does NOT stop split-view**: one log can sign
  two internally-consistent-but-different tree heads to two audiences
  ([draft-ietf-trans-gossip-05, 2018](https://datatracker.ietf.org/doc/html/draft-ietf-trans-gossip-05)).
- **Tier B — defeat split-view.** *Gossip* (STH pollination, SCT feedback) is
  detection-after-the-fact and needs wide client adoption; it never became an RFC, and
  that non-adoption is the documented weak point. *Witness cosigning* is the modern,
  preventive replacement: a quorum of k independent witnesses each verify the
  consistency proof and cosign each head; a client policy rejects any head lacking k
  cosignatures ([Sigsum](https://git.sigsum.org/sigsum/plain/doc/design.md);
  [C2SP tlog-cosignature](https://c2sp.org/tlog-cosignature) /
  [tlog-policy](https://c2sp.org/tlog-policy);
  [Go checksum db, proposal 25530](https://go.googlesource.com/proposal/+/master/design/25530-sumdb.md)).
- **Tier C — force submission in the first place. NEITHER crypto nor witnesses give
  this.** A CA that never logs a cert, or a designer who runs candidates off-platform
  and never anchors them, is invisible; no proof is owed for records that do not exist.

**The count-committing primitive** for direction (b): key-transparency directories
prove a mapping is *complete*. CONIKS
([Melara-Blankstein-Bonneau-Felten-Freedman, USENIX Security 2015](https://www.usenix.org/system/files/conference/usenixsecurity15/sec15-paper-melara.pdf))
gives authenticated non-membership (proof of absence); SEEMless
([Chase-Deshpande-Ghosh-Malvai, CCS 2019 / ePrint 2018/607](https://eprint.iacr.org/2018/607.pdf))
builds a Verifiable Key Directory from an append-only ZK set and proves each epoch
grew append-only while leaking nothing else; Merkle²
([IEEE S&P 2021](https://eprint.iacr.org/2021/453)) and Parakeet
([NDSS 2023](https://eprint.iacr.org/2023/081)) make it low-latency and production-grade.
A contiguous sequence number per run makes "1-of-N" become "head says N=9, you gave me 1,"
detectable from the head alone. TUF's snapshot role
([The Update Framework](https://theupdateframework.io/)) is the same "metadata commits
to the whole current set, so you cannot serve a valid subset" idea outside KT.

**The enforcement boundary (the exact blind spot, stated sharply).** CT's completeness
is not cryptographic; it is manufactured at *consumption time*. Chrome (v68, enforcing
for certs issued after 30 Apr 2018) shows a full-page interstitial for any
publicly-trusted cert without a valid SCT, and requires SCTs from ≥2 distinct log
operators (the "≥1 Google log" rule was removed Apr 2022 for pure operator-diversity;
[Chromium ct-policy](https://groups.google.com/a/chromium.org/g/ct-policy/c/507lPdbbwSk)).
Causal chain: **browser refuses unlogged cert → CA cannot sell one → CA logs.** Remove
the mandate and CT degrades to a voluntary registry a rogue CA ignores. There is no
"browser" at the moment a biodesign candidate executes. The selective-disclosure
attacker does not fight your Merkle tree; they route around it by never creating the
N−1 entries.

---

## 3. Commit-then-prove — strong at the node, blind on the count

Kroll et al., "Accountable Algorithms," 165 U. Pa. L. Rev. 633 (2017)
([Penn](https://scholarship.law.upenn.edu/penn_law_review/vol165/iss3/3/);
[Kroll thesis, Princeton 2015](https://www.jkroll.com/papers/dissertation.pdf)) proves
*procedural regularity*: commit to the procedure before results (commitment), prove
each output came from the committed program on that input (verifiable computation /
zk-SNARK), and if randomness is used, commit the seed. **Its guarantee is per-decision
and assumes the SET of cases is externally fixed and public** (every voter, every
applicant). That is the assumption the biodesign setting breaks: the audited party
chooses the set of runs.

- **Formal glue:** commit-and-prove SNARKs
  ([LegoSNARK, Campanelli-Fiore-Querol, CCS 2019 / ePrint 2019/142](https://eprint.iacr.org/2019/142),
  [code](https://github.com/imdea-software/legosnark)) bind a previously-committed value
  to the execution proof, so "committed procedure/input" and "the run" refer to the same
  object. Gives node-level integrity; not designed to bound the *number* of commitments.
- **The count blind spot is structural, not an implementation gap.** Run P1…Pn, each with
  a valid commit-then-prove bundle, submit only Pk. Every proof verifies. A proof is a
  statement about a presented artifact; there is no artifact for "the runs I chose not
  to mention."
- **The ML community rediscovered this exact gap.** Proof-of-Learning
  (Jia et al., IEEE S&P 2021) is cheaply spoofable
  ([Fang-Jia-Thudi et al., "Proof-of-Learning Is Currently More Broken Than You Think,"
  EuroS&P 2023, arXiv:2208.03567](https://arxiv.org/abs/2208.03567)). The most on-point
  recent work, verifiable fine-tuning
  ([Akgul et al., 2025, arXiv:2510.16830](https://arxiv.org/pdf/2510.16830)),
  pre-commits dataset/hyperparameters/seed/policy and proves in ZK that the disclosed
  run honored them, defeating checkpoint/seed manipulation *within a run*, then states
  its own limit verbatim: it "does not prevent running multiple independent training
  experiments and disclosing only the most favorable result... verifies what happened in
  disclosed runs, not how many undisclosed runs occurred." The gap is named and unsolved
  in the newest crypto-ML literature.
- **zkVM cost wall (why proving a REAL computation is out of scope).** General-purpose
  zkVMs run 4-6 orders of magnitude over native execution
  ([a16z crypto](https://a16zcrypto.com/posts/article/secure-efficient-zkvms-progress/):
  even Jolt is ~500,000x slower than native RISC-V). 2025 GPU benchmarks
  ([Fenbushi](https://fenbushi.vc/2025/08/29/benchmarking-zkvms-current-state-and-prospects/)):
  100k-iteration Fibonacci proves in ~3.4 s (SP1) / 3.6 s (RISC Zero); proof sizes
  222 KB-1.8 MB. A toy scoring function is demoable; a docking sweep, an ESM/AlphaFold
  forward pass, or an MD trajectory inside a zkVM is not, on a solo/no-large-GPU budget.
- **The only handle on the negative is trusted hardware, and it relocates trust rather
  than proving anything.** A TEE (SGX/TDX/SEV-SNP) with remote attestation plus an
  in-enclave monotonic run-counter makes every *in-enclave* execution countable, IFF a
  mandate forbids running outside the enclave. It does not prove the negative; it
  enforces a chokepoint and then trusts the policy and the TEE (which has a long
  side-channel/rollback history). VDFs / proof-of-sequential-work
  ([Boneh-Bonneau-Bünz-Fisch, CRYPTO 2018](https://link.springer.com/content/pdf/10.1007/978-3-319-96884-1_25.pdf))
  prove a *lower* bound on effort, the wrong direction, and parallel hardware defeats any
  attempt to bound run count from below.

---

## 4. The synthesis — a two-layer scheme, and what it does/doesn't buy

- **Upper layer (set completeness, from §1/§2).** Before any evaluation, publish a
  single commitment to the entire set {P1…PN} (Merkle root over N leaves, or a vector
  commitment), timestamped to a public append-only anchor. Fixes N and every node's
  identity before results exist. Disclosure reveals one leaf + path, so the auditor sees
  the disclosed run AND that it was 1-of-N pre-committed siblings AND sees N.
- **Lower layer (per-node integrity, from §3).** Each leaf is a commit-then-prove
  obligation binding that node to its real procedure + inputs (CP-SNARK/zkVM, or at
  minimum a commitment to real inputs the auditor can spot-check). Without it, the set
  can be padded with N−1 cheap decoys ("procedure that returns 0") so a large N looks
  like diligence while being theater.

Neither layer alone suffices:
- §1/§2 without §3: proves N runs happened, not WHAT each was → decoy-padding.
- §3 without §1/§2: proves the one shown run is honest, not that it was the only run →
  the original hole.
- Together: N is committed and (witness-)anchored so it cannot shrink, and each of the N
  is bound to its real procedure so it cannot be a decoy.

**What it still cannot do:** stop an (N+1)th run under a fresh, unpublished commitment,
or off-platform. That is §5, and it is not a crypto problem.

Mapping to the three mitigation directions:
- **(a) commit-to-search-tree** = the upper layer, §1. The standard construction; not a
  hunch. Buildable.
- **(b) run-count / monotonic-counter commitment** = §1's cardinality binding + §2's
  count-committing sequence (CONIKS/SEEMless/TUF-snapshot). This is the SAME requirement
  as the attestation gap: the monotonic anchor must be issued by something outside the
  designer's control (Modal control plane, a witness quorum, a TSA).
- **(c) accept-unsolvable-at-record-layer + reviewer** = correct for the *negative*.
  The residual is a review-capacity/governance gap (the RAND prose gap), and a
  Claude-based reviewer that flags "this bundle covers 1 of an anchored session of N,"
  or "this session is not anchored at all," is the honest artifact.

---

## 5. The negative is a governance problem — the chokepoint

All three fields converge on the same answer for "force submission in the first place":
- CT: consumption-side mandate (browser rejects unlogged certs).
- Verifiable training / commit-then-prove: TEE chokepoint or a mandated logging platform.
- Science: preregistration is only as good as the *registry mandate*
  ([OSF preregistration](https://casrai.org/guides/osf-preregistration); Registered
  Reports). Its known failure mode is exactly study-level selective disclosure (the
  file-drawer problem); the fix is journals/funders refusing unregistered studies, not
  crypto.

For biodesign the leverage point is the **synthesis/ordering step or the
publication/credit step** (the equivalents of the browser): a DNA-synthesis provider, a
journal, or a benchmark leaderboard that refuses a design whose *session* is not
witnessed-anchored. That bootstraps population completeness. It is a policy lever, and an
adopting consumer cannot be supplied by code.

---

## 6. Scope: demonstrated here vs research-grade

**Demonstrated here, no ZK toolchain, post-quantum-safe (hash-only):**
- Merkle/vector set-commitment over N procedure specs at t0, timestamped
  ([pymerkle](https://github.com/fmerg/pymerkle) already does RFC-6962 inclusion +
  consistency; anchor via `rfc3161ng` / OpenTimestamps). Reveal one leaf + path; verifier
  checks the path, the pre-committed root, and displays N. This alone is the
  core claim: "these N were fixed before results, and the disclosed bundle omits none."
- A single witness process cosigning the session head, with the verifier policy requiring
  it (C2SP tlog-cosignature shape). Shows Tier-B split-view prevention.
- A **completeness checker** that FAILS a bundle submitting only run j against a head that
  commits N. Directly demonstrates the 1-of-N failure.
- Optional: toy commit-then-prove on a small deterministic scoring function inside SP1 or
  RISC Zero (~3-4 s/proof on GPU), to show node-integrity and its cost wall honestly.
- **The strongest narrative artifact:** commit N, disclose 1 with a valid proof, then
  *show the surviving attack* (an off-ledger (N+1)th run leaving the scheme silent).
  This makes the blind spot concrete.

**Research-grade, out of scope:**
- zkVM proof-of-execution over a REAL biodesign computation (docking/ESM/MD): 10^5-10^6x
  prover overhead.
- A genuine TEE-attested pipeline with in-enclave run-counter binding ALL executions:
  needs confidential-computing hardware and multi-week TCB work.
- Any actual proof of the negative: it is the open problem, not a build target.
- Real independence of a witness quorum, and the consumption-side mandate: social/
  organizational facts, not code artifacts.

**Summary of scope.** The repository demonstrates Tier A (tamper-evident, count-committing
session log), a sketch of Tier B (witness cosignature), and the two-layer commitment that
stops omission-of-committed and decoy-padding. It does not demonstrate Tier C, and Tier C is
what fully answers selective disclosure. The contribution is the **composition + the
demonstrated surviving attack**: witnessed, count-committed session logs make within-session
subsetting detectable; making the whole design population complete is a deployment-mandate
problem that code cannot supply.

---

## Source index
ZK sets / accumulators: [MRK FOCS 2003](https://dblp.org/rec/conf/focs/MicaliRK03.html) ·
[mercurial (EUROCRYPT 2005)](https://cs.brown.edu/~mchase/papers/merc.pdf) ·
[Benaloh-de Mare 1993](https://www.microsoft.com/en-us/research/wp-content/uploads/1993/01/owa.pdf) ·
[Camenisch-Lysyanskaya CRYPTO 2002](https://cs.brown.edu/people/alysyans/papers/camlys02.pdf) ·
[Li-Li-Xue ACNS 2007](https://www.cs.purdue.edu/homes/ninghui/papers/accumulator_acns07.pdf) ·
[Ghosh et al. ePrint 2015/404](https://eprint.iacr.org/2015/404.pdf) ·
[Catalano-Fiore ePrint 2011/495](https://eprint.iacr.org/2011/495.pdf) ·
[Tomescu AAD ePrint 2018/721](https://eprint.iacr.org/2018/721) ·
[Campanelli batch ePrint 2021/1672](https://eprint.iacr.org/2021/1672.pdf) ·
[Benarroch et al. ePrint 2019/1255](https://eprint.iacr.org/2019/1255.pdf).
Transparency logs: [RFC 6962](https://www.rfc-editor.org/rfc/rfc6962.html) ·
[RFC 9162](https://www.rfc-editor.org/rfc/rfc9162.html) ·
[trans-gossip-05](https://datatracker.ietf.org/doc/html/draft-ietf-trans-gossip-05) ·
[Sigsum](https://git.sigsum.org/sigsum/plain/doc/design.md) ·
[C2SP tlog-cosignature](https://c2sp.org/tlog-cosignature) ·
[Go sumdb 25530](https://go.googlesource.com/proposal/+/master/design/25530-sumdb.md) ·
[TUF](https://theupdateframework.io/) ·
[CONIKS USENIX Sec 2015](https://www.usenix.org/system/files/conference/usenixsecurity15/sec15-paper-melara.pdf) ·
[SEEMless ePrint 2018/607](https://eprint.iacr.org/2018/607.pdf) ·
[Merkle² ePrint 2021/453](https://eprint.iacr.org/2021/453) ·
[Parakeet ePrint 2023/081](https://eprint.iacr.org/2023/081) ·
[Sigstore Rekor](https://docs.sigstore.dev/logging/overview/) ·
[Chrome CT enforcement](https://www.securityweek.com/chrome-browser-now-enforces-certificate-transparency/) ·
[SoK log transparency 2023](https://arxiv.org/pdf/2305.01378).
Commit-then-prove / negative: [Accountable Algorithms 2017](https://scholarship.law.upenn.edu/penn_law_review/vol165/iss3/3/) ·
[Kroll thesis 2015](https://www.jkroll.com/papers/dissertation.pdf) ·
[Wexler BTLJ 2023](https://btlj.org/wp-content/uploads/2023/04/0001-37-1-Wexler.pdf) ·
[LegoSNARK ePrint 2019/142](https://eprint.iacr.org/2019/142) ·
[a16z zkVM progress](https://a16zcrypto.com/posts/article/secure-efficient-zkvms-progress/) ·
[Fenbushi zkVM benchmarks 2025](https://fenbushi.vc/2025/08/29/benchmarking-zkvms-current-state-and-prospects/) ·
[PoL broken, EuroS&P 2023](https://arxiv.org/abs/2208.03567) ·
[Verifiable fine-tuning 2025](https://arxiv.org/pdf/2510.16830) ·
[VDF CRYPTO 2018](https://link.springer.com/content/pdf/10.1007/978-3-319-96884-1_25.pdf) ·
[OSF preregistration](https://casrai.org/guides/osf-preregistration).

Unverified: page ranges not line-checked against proceedings PDFs; Proof-of-Learning
(Jia et al., IEEE S&P 2021) cited via the 2023 rebuttal, not the original. Confirm
before any public use.
