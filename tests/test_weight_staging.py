import argparse
import importlib.util
from pathlib import Path

import modal
import pytest


def script():
    path = Path(__file__).resolve().parents[1] / "scripts/stage_design_weights.py"
    spec = importlib.util.spec_from_file_location("stage_design_weights", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_hugging_face_environment_removes_all_known_token_variables(monkeypatch):
    module = script()
    monkeypatch.setenv("HF_TOKEN", "secret-one")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "secret-two")
    monkeypatch.setenv("HUGGINGFACEHUB_API_TOKEN", "secret-three")
    environment = module.hf_environment(public=True)
    assert environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert not {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"} & environment.keys()
    assert module.hf_environment(public=False)["HF_TOKEN"] == "secret-one"


@pytest.mark.parametrize("name", ["UPPER", "bad_name", "-leading", "a" * 64])
def test_modal_volume_name_rejects_ambiguous_or_invalid_values(name):
    with pytest.raises(argparse.ArgumentTypeError):
        script().volume_name(name)


def test_review_upload_can_validate_one_tool_into_an_isolated_volume(tmp_path, monkeypatch):
    module = script()
    weights = tmp_path / "weights" / "esmc"
    weights.mkdir(parents=True)
    (weights / "config.json").write_bytes(b"{}")
    (weights / "model.safetensors").write_bytes(b"public-checkpoint-test")
    selected_volume, uploaded = [], []

    class Upload:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def put_file(self, source, destination):
            uploaded.append((Path(source), destination))

    class Volume:
        def batch_upload(self):
            return Upload()

    def from_name(name, *, create_if_missing):
        selected_volume.append((name, create_if_missing))
        return Volume()

    monkeypatch.setattr(modal.Volume, "from_name", from_name)
    monkeypatch.setattr("sys.argv", [
        "stage_design_weights.py",
        str(tmp_path / "weights"),
        "--review-and-upload",
        "--tool", "esmc",
        "--volume", "biodesign-vat-benchbb-esmc-weights",
    ])
    assert module.main() == 0
    assert selected_volume == [("biodesign-vat-benchbb-esmc-weights", True)]
    assert {destination for _, destination in uploaded} == {
        "/esmc/config.json", "/esmc/model.safetensors", "/deployment-lock.json"
    }
    assert (tmp_path / "weights" / "deployment-lock.json").is_file()


def test_benchbb_esmc_app_exposes_only_scoring_and_recovery_boundaries():
    path = Path(__file__).resolve().parents[1] / "scripts/benchbb_esmc_app.py"
    spec = importlib.util.spec_from_file_location("benchbb_esmc_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from vatlog.modal import VatFunction

    assert module.app.name == "biodesign-vat-benchbb-esmc"
    assert isinstance(module.esmc, VatFunction)
    assert module.esmc.identity.app_name == module.app.name
    assert module.esmc.identity.function_name == "esmc"
    assert isinstance(module.recover, modal.Function)
