"""Real local log persistence, signed proof rejection, and observer policy."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from demo.sessions import build_clean
from vatlog.bundle import build_bundle, Bundle
from vatlog.notary import issue_receipt
from vatlog.roles import Keyring
from vatlog.transparency import TransparencyLog, verify_membership
from vatlog.verify import verify_bundle


def submitted(root, ring):
    session, *_ = build_clean(root, keyring=ring)
    bundle = build_bundle(session)
    bundle.notary_receipt = issue_receipt(bundle, ring.notary, ring.trust_bundle())
    return bundle


def test_historical_proofs_retry_and_stripping(tmp_path):
    ring = Keyring.generate()
    trust = ring.trust_bundle()
    stamp = datetime.now(timezone.utc)
    with TransparencyLog(tmp_path / "log.db", trust, signer=ring.transparency_log, log_id="demo") as log:
        bundles = [submitted(tmp_path / str(i), ring) for i in range(3)]
        for i, bundle in enumerate(bundles):
            bundle.transparency_evidence = log.append(bundle, now=stamp + timedelta(seconds=i))
            assert verify_bundle(bundle, trust, require_transparency=True).exit_code == 0
        first, second = [b.transparency_evidence.checkpoint for b in bundles[:2]]
        proof = log.consistency(first, second)
        assert verify_bundle(bundles[1], trust, witness_checkpoint=first,
                             witness_consistency_proof=proof).exit_code == 0
        assert len(log.export().entries) == 3
        retry = log.append(bundles[0])
        assert retry.entry == bundles[0].transparency_evidence.entry
        verify_membership(retry)
        assert len(log.export().entries) == 3
        bundles[0].notary_receipt = issue_receipt(bundles[0], ring.notary, trust)
        with pytest.raises(ValueError, match="Conflicting"):
            log.append(bundles[0])
        bare = bundles[1].model_copy(deep=True)
        bare.notary_receipt = bare.transparency_evidence = None
        assert verify_bundle(bare, trust).exit_code == 0
        assert verify_bundle(bare, trust, require_transparency=True).exit_code == 4
        bundles[1].save(tmp_path / "bundle.zip")
        assert verify_bundle(Bundle.load(tmp_path / "bundle.zip"), trust, require_transparency=True).exit_code == 0
    with TransparencyLog(tmp_path / "log.db", trust) as reopened:
        assert reopened.consistency(first, second) == proof


@pytest.mark.parametrize("target", ["index", "size", "signature", "receipt", "prior", "witness"])
def test_rejects_altered_evidence(tmp_path, target):
    ring = Keyring.generate()
    trust = ring.trust_bundle()
    with TransparencyLog(tmp_path / "log.db", trust, signer=ring.transparency_log, log_id="test") as log:
        first = submitted(tmp_path / "first", ring)
        anchor = log.append(first).checkpoint
        bundle = submitted(tmp_path / "second", ring)
        bundle.transparency_evidence = log.append(bundle)
        evidence = bundle.transparency_evidence
        expected = 4
        if target == "index":
            evidence.inclusion_proof["leaf_index"] = 0
        elif target == "size":
            evidence.inclusion_proof["metadata"]["size"] += 1
        elif target == "signature":
            evidence.checkpoint.signature = "AAAA"
            expected = 3
        elif target == "receipt":
            bundle.notary_receipt = first.notary_receipt
            expected = 3
        elif target == "prior":
            evidence.consistency_proof["earlier_size"] = 2
        else:
            wrong = log.consistency(anchor, anchor)
            assert verify_bundle(bundle, trust, witness_checkpoint=anchor,
                                 witness_consistency_proof=wrong).exit_code == 4
            return
        assert verify_bundle(bundle, trust).exit_code == expected


def test_transaction_failure_clock_and_corruption(tmp_path):
    ring = Keyring.generate()
    trust = ring.trust_bundle()
    path = tmp_path / "log.db"
    with TransparencyLog(path, trust, signer=ring.transparency_log, log_id="test") as log:
        first = submitted(tmp_path / "first", ring)
        stamp = datetime.now(timezone.utc)
        log.append(first, now=stamp)
        second = submitted(tmp_path / "second", ring)
        with pytest.raises(ValueError, match="backwards"):
            log.append(second, now=stamp - timedelta(seconds=1))
        log.db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON entries BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            log.append(second)
        assert len(log.export().entries) == 1
        log.db.execute("DROP TRIGGER fail_insert")
        log.append(second)
        log.db.execute("UPDATE entries SET position=9 WHERE position=1")
    with pytest.raises(ValueError, match="Corrupt"):
        TransparencyLog(path, trust)


def test_concurrent_writers_preserve_one_ordered_history(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    ring = Keyring.generate()
    trust = ring.trust_bundle()
    path = tmp_path / "log.db"
    with TransparencyLog(path, trust, signer=ring.transparency_log, log_id="concurrent"):
        pass
    bundles = [submitted(tmp_path / str(i), ring) for i in range(4)]
    def append(bundle):
        with TransparencyLog(path, trust, signer=ring.transparency_log) as log:
            return log.append(bundle).entry.leaf_index
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(append, bundles)) == [0, 1, 2, 3]
    with TransparencyLog(path, trust) as log:
        assert len(log.export().entries) == 4


def test_observer_cli_targets_historical_checkpoint(tmp_path):
    from vatlog.cli import main
    from vatlog.transparency_cli import atomic_write
    ring = Keyring.generate()
    trust = ring.trust_bundle()
    trust.save(tmp_path / "trust.json")
    with TransparencyLog(tmp_path / "log.db", trust, signer=ring.transparency_log, log_id="cli") as log:
        bundles = [submitted(tmp_path / str(i), ring) for i in range(3)]
        for bundle in bundles:
            bundle.transparency_evidence = log.append(bundle)
        atomic_write(tmp_path / "first.json", bundles[0].transparency_evidence.checkpoint.canonical())
        atomic_write(tmp_path / "target.json", bundles[1].transparency_evidence.checkpoint.canonical())
        bundles[1].save(tmp_path / "bundle.zip")
    assert main(["log-consistency", "--store", str(tmp_path / "log.db"), "--trust", str(tmp_path / "trust.json"),
                 "--from-checkpoint", str(tmp_path / "first.json"), "--to-checkpoint", str(tmp_path / "target.json"),
                 "--out", str(tmp_path / "proof.json")]) == 0
    assert main(["verify", str(tmp_path / "bundle.zip"), "--trust", str(tmp_path / "trust.json"),
                 "--require-transparency", "--witness-checkpoint", str(tmp_path / "first.json"),
                 "--witness-consistency-proof", str(tmp_path / "proof.json")]) == 0


def test_consistency_sizes_cannot_be_relabelled():
    from vatlog.merkle import RecordTree, check_consistency
    for size in range(1, 34):
        tree = RecordTree.from_leaves([str(i).encode() for i in range(size)])
        for earlier in range(1, size + 1):
            proof = tree.prove_consistency(earlier)
            assert check_consistency(tree.root_at(earlier), tree.root_hex, proof,
                                     earlier_size=earlier, later_size=size)
            assert not check_consistency(tree.root_at(earlier), tree.root_hex, proof,
                                         earlier_size=earlier + 1, later_size=size)


def test_saved_witness_detects_resigned_backdating(tmp_path):
    from vatlog.merkle import RecordTree
    ring = Keyring.generate()
    trust = ring.trust_bundle()
    stamp = datetime.now(timezone.utc)
    with TransparencyLog(tmp_path / "log.db", trust, signer=ring.transparency_log, log_id="witness") as log:
        first = log.append(submitted(tmp_path / "first", ring), now=stamp)
        bundle = submitted(tmp_path / "second", ring)
        evidence = log.append(bundle, now=stamp + timedelta(seconds=1))
    # Simulate a log operator re-signing a coherent alternate view with an earlier local time.
    evidence.entry.accepted_at = stamp - timedelta(seconds=1)
    evidence.prior_checkpoint = first.checkpoint.model_copy(deep=True)
    evidence.prior_checkpoint.timestamp = stamp - timedelta(seconds=2)
    evidence.prior_checkpoint.signature = ring.transparency_log.sign(evidence.prior_checkpoint.payload())
    tree = RecordTree.from_leaves([first.entry.canonical(), evidence.entry.canonical()])
    evidence.checkpoint.root_hash = tree.root_hex
    evidence.checkpoint.signature = ring.transparency_log.sign(evidence.checkpoint.payload())
    evidence.inclusion_proof = tree.prove_inclusion(1)
    evidence.consistency_proof = tree.prove_consistency(1)
    bundle.transparency_evidence = evidence
    assert verify_bundle(bundle, trust, require_transparency=True).exit_code == 0
    from vatlog.transparency import ConsistencyEvidence
    proof = ConsistencyEvidence(from_checkpoint=first.checkpoint, to_checkpoint=evidence.checkpoint,
                                proof=tree.prove_consistency(1))
    assert verify_bundle(bundle, trust, witness_checkpoint=first.checkpoint,
                         witness_consistency_proof=proof).exit_code == 4
