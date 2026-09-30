"""Verify demo input identity at the actual public-download boundary, without network."""

import runpy
from pathlib import Path

import httpx
import pytest

from test_design_workflow import coordinates

prepare = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/prepare_demo_target.py"))["prepare"]


def test_prepared_target_is_exactly_the_supplied_variant_and_is_labelled_predicted():
    def upstream(request):
        if "/api/prediction/" in request.url.path:
            return httpx.Response(200, json=[{"cifUrl": "https://alphafold.ebi.ac.uk/files/model.cif", "uniprotSequence": "AC"}])
        return httpx.Response(200, content=coordinates({"A": "AC"}))
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        raw, metadata = prepare("P12345", "AC", client)
        assert raw == coordinates({"A": "AC"})
        assert metadata["residues"] == 2
        assert "no mature chromophore" in metadata["representation"]
        with pytest.raises(ValueError, match="does not match"):
            prepare("P12345", "AA", client)


def test_demo_input_downloader_never_follows_an_unexpected_origin():
    calls = []
    def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[{"cifUrl": "http://127.0.0.1/private", "uniprotSequence": "AC"}])
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(ValueError, match="download origin"):
            prepare("P12345", "AC", client)
        assert len(calls) == 1
