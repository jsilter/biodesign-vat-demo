"""Opaque transcript commitments, availability and record-granularity reporting."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from demo.sessions import CallArtifacts, DESIGNS, build_clean, build_doctored, build_partial
from vatlog.bundle import Bundle, build_bundle
from vatlog.roles import Keyring
from vatlog.schema import RecordType
from vatlog.store import SessionLog
from vatlog.verify import ArtifactCounts, Outcome, verify_bundle


INSTRUCTIONS = b"Preserve the supplied audit evidence."
TRANSCRIPT = json.dumps([{"timestamp": "2026-09-22T14:00:00Z", "text": "fixture result"}]).encode()


@pytest.fixture
def captured(tmp_path):
    ring = Keyring.generate()
    calls = [CallArtifacts(prompt=b"request", transcript=TRANSCRIPT,
                           reasoning=b"provider-supplied fixture text" if i == 0 else None) for i in range(3)]
    log, *_ = build_clean(tmp_path, keyring=ring, instructions=INSTRUCTIONS,
                         calls=calls, granularity="design_run")
    return ring, log


def test_artifacts_attach_to_opening_and_call_groups(captured):
    ring, log = captured
    opening = log.records[0]
    assert opening.record_type is RecordType.SESSION_OPEN
    assert [(a.kind, log.blobs.get(a.sha256)) for a in opening.inputs] == [("prompt", INSTRUCTIONS)]
    for index, record in enumerate(log.records[1:]):
        assert any(a.kind == "prompt" for a in record.inputs)
        assert any(a.kind == "transcript" for a in record.results)
        assert any(a.kind == "reasoning" for a in record.results) == (index == 0)
    report = verify_bundle(build_bundle(log), ring.trust_bundle(), submitted_sequence=DESIGNS[2])
    assert report.outcome is Outcome.OK
    assert report.instructions == "present"
    assert report.leaf_counts == {"session_open": 1, "design_run": 3}
    assert report.transcripts == ArtifactCounts(present=3, absent=1)
    assert report.reasoning == ArtifactCounts(present=1, absent=3)
    assert "reasoning (revealed records): present=1, withheld=0" in report.render()
    assert "reasoning (revealed records): present=1, withheld=0, absent" not in report.render()


def test_altered_transcript_is_tampering_after_zip_round_trip(captured, tmp_path):
    ring, log = captured
    bundle = build_bundle(log)
    transcript = next(a for a in log.records[1].results if a.kind == "transcript")
    bundle.blobs[transcript.sha256] = b"redacted after hashing"
    path = tmp_path / "changed.zip"
    bundle.save(path)
    report = verify_bundle(Bundle.load(path), ring.trust_bundle())
    assert report.outcome is Outcome.TAMPERED and report.exit_code == 2
    assert report.checks[-1].number == 4
    assert "blob digest mismatch" in report.checks[-1].detail


def test_signed_transcript_length_mismatch_is_tampering(captured, tmp_path):
    ring, source = captured
    log = SessionLog(tmp_path / "resealed", source.session_uuid)
    for record in source.records:
        record = record.model_copy(deep=True)
        for artifact in record.inputs + record.results:
            log.blobs.put(source.blobs.get(artifact.sha256))
            if artifact.kind == "transcript":
                artifact.bytes_len += 1
        log.append(record)
    log.seal(ring.recorder, recorder_identity="test", environment=source.records[0].environment)
    report = verify_bundle(build_bundle(log), ring.trust_bundle())
    assert report.outcome is Outcome.TAMPERED and report.checks[-1].number == 4
    assert "blob size mismatch" in report.checks[-1].detail


def test_digest_only_reports_withheld_and_remains_valid(captured, tmp_path):
    ring, log = captured
    path = tmp_path / "digests.zip"
    build_bundle(log, include_blobs=False).save(path)
    report = verify_bundle(Bundle.load(path), ring.trust_bundle())
    assert report.outcome is Outcome.OK
    assert report.instructions == "withheld"
    assert report.transcripts == ArtifactCounts(withheld=3, absent=1)
    assert report.reasoning == ArtifactCounts(withheld=1, absent=3)


def test_no_transcripts_or_reasoning_is_ordinary(tmp_path):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring)
    report = verify_bundle(build_bundle(log), ring.trust_bundle())
    assert report.outcome is Outcome.OK
    assert report.instructions == "absent"
    assert report.transcripts == ArtifactCounts(absent=4)
    assert report.reasoning == ArtifactCounts(absent=4)
    assert "reasoning" not in report.render()


def test_opening_withheld_reports_absent_instructions_and_unknown_leaf_type(captured):
    ring, log = captured
    report = verify_bundle(build_bundle(log, reveal=[1, 2, 3]), ring.trust_bundle())
    assert report.outcome is Outcome.INCOMPLETE
    assert all(c.passed for c in report.checks[:4])
    assert report.instructions == "absent"
    assert report.leaf_counts == {"design_run": 3}
    assert report.unrevealed_leaves == 1
    assert "missing leaves [0]" in report.checks[-1].detail
    assert "unrevealed=1 (types unknown)" in report.render()


def test_repeated_bytes_store_one_blob_and_each_record_verifies(captured):
    ring, log = captured
    transcripts = [next(a for a in r.results if a.kind == "transcript") for r in log.records[1:]]
    assert len({a.sha256 for a in transcripts}) == 1
    digest = transcripts[0].sha256
    assert len(list(log.blobs.root.glob(digest))) == 1
    bundle = build_bundle(log)
    assert list(bundle.blobs).count(digest) == 1
    for index in range(1, 4):
        report = verify_bundle(build_bundle(log, reveal=[0, index]), ring.trust_bundle())
        assert report.outcome is Outcome.INCOMPLETE
        assert all(c.passed for c in report.checks[:4])


def test_agent_call_leaf_can_be_withheld_independently(tmp_path):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring, instructions=INSTRUCTIONS,
                         calls=[CallArtifacts(transcript=TRANSCRIPT)] * 3, granularity="agent_call")
    report = verify_bundle(build_bundle(log), ring.trust_bundle(), submitted_sequence=DESIGNS[2])
    assert report.outcome is Outcome.OK
    assert report.leaf_counts == {"session_open": 1, "agent_call": 3, "design_run": 3}
    assert report.tree_size == 7
    for record in log.records:
        assert any(a.kind == "transcript" for a in record.results) == (record.record_type is RecordType.AGENT_CALL)
    partial = verify_bundle(build_bundle(log, reveal=[0, 2, 3, 4, 5, 6]), ring.trust_bundle())
    assert partial.outcome is Outcome.INCOMPLETE
    assert "missing leaves [1]" in partial.checks[-1].detail
    assert partial.leaf_counts == {"session_open": 1, "agent_call": 2, "design_run": 3}
    assert partial.unrevealed_leaves == 1


@pytest.mark.parametrize("data", [b"", b"\xff\x00not JSON", b"redacted before hashing", b"x" * 150_000])
def test_transcripts_are_opaque_uncapped_bytes(tmp_path, data):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring, instructions=b"",
                         calls=[CallArtifacts(transcript=data)] * 3, granularity="design_run")
    path = tmp_path / "opaque.zip"
    build_bundle(log).save(path)
    report = verify_bundle(Bundle.load(path), ring.trust_bundle())
    assert report.outcome is Outcome.OK
    assert report.instructions == "present"
    assert report.transcripts.present == 3


def test_mixed_availability_counts_records_not_artifacts(captured):
    ring, log = captured
    bundle = build_bundle(log)
    prompt = next(a for a in bundle.records[1].inputs if a.kind == "prompt")
    # Give one run a second transcript, then seal the authentic metadata anew.
    record = log.records[1].model_copy(deep=True)
    record.results.append(log.blobs.artifact("transcript", "second.txt", b"second"))
    rebuilt = SessionLog(log.root / "mixed", log.session_uuid)
    for index, source in enumerate(log.records):
        rebuilt.append(record if index == 1 else source)
        for artifact in (record if index == 1 else source).inputs + (record if index == 1 else source).results:
            rebuilt.blobs.put(log.blobs.get(artifact.sha256))
    rebuilt.seal(ring.recorder, recorder_identity="test", environment=record.environment)
    bundle = build_bundle(rebuilt)
    del bundle.blobs[record.results[-1].sha256]
    del bundle.blobs[prompt.sha256]
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.OK
    assert report.transcripts == ArtifactCounts(present=3, withheld=1, absent=1)
    assert report.instructions == "present"


@pytest.mark.parametrize("digest_only", [False, True])
def test_show_prints_artifact_metadata_and_availability(captured, tmp_path, digest_only):
    _, log = captured
    path = tmp_path / "show.zip"
    build_bundle(log, include_blobs=not digest_only).save(path)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-m", "vatlog.cli", "show", str(path)],
                            env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "[0] session_open" in result.stdout and "[1] design_run" in result.stdout
    transcript = next(a for a in log.records[1].results if a.kind == "transcript")
    status = "withheld (digest only)" if digest_only else "present"
    assert (f"results: transcript {transcript.name} sha256={transcript.sha256[:16]}... "
            f"bytes={len(TRANSCRIPT)} {status}") in result.stdout


def test_demo_builders_exercise_both_granularities(tmp_path):
    ring = Keyring.generate()
    doctored, *_ = build_doctored(tmp_path, keyring=ring)
    partial, *_ = build_partial(tmp_path, keyring=ring)
    for log, expected in [(doctored, {"session_open": 1, "agent_call": 3, "design_run": 3}),
                          (partial, {"session_open": 1, "design_run": 9})]:
        report = verify_bundle(build_bundle(log), ring.trust_bundle())
        assert report.outcome is Outcome.OK
        assert report.leaf_counts == expected
        assert report.transcripts.present > 0
        for record in log.records:
            for artifact in record.results:
                if artifact.kind == "transcript":
                    transcript = json.loads(log.blobs.get(artifact.sha256))
                    assert isinstance(transcript, list) and "timestamp" in transcript[0]


@pytest.mark.parametrize("calls,granularity", [([CallArtifacts()], "design_run"),
                                              ([CallArtifacts()] * 3, None),
                                              ([CallArtifacts()] * 3, "unknown")])
def test_builder_rejects_ambiguous_call_mapping_before_writing(tmp_path, calls, granularity):
    with pytest.raises(ValueError):
        build_clean(tmp_path, calls=calls, granularity=granularity)
    assert not (tmp_path / "clean-0001").exists()
