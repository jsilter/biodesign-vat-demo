"""Reproducible Claude Science skill and unpaid proposal assets."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

from runner.dynamic_contracts import DynamicProposal
from runner.design_store import DesignStore
from runner.workflow_contracts import WorkflowProposal
from demo.demo import prepare_fixture


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "vat-protein-workflows"
LOCAL_SKILLS = ("vat-evidence-review", "vat-benchbb-validation")


def test_committed_skill_schemas_match_backend_contracts():
    assert json.loads((SKILL / "references/vat-workflow.schema.json").read_text()) == \
        WorkflowProposal.model_json_schema()
    assert json.loads((SKILL / "references/vat-dynamic-job.schema.json").read_text()) == \
        DynamicProposal.model_json_schema()


def test_skill_package_is_deterministic_and_has_one_top_level_directory():
    command = [sys.executable, str(ROOT / "scripts/package_claude_science_skill.py")]
    subprocess.run(command, check=True, cwd=ROOT, stdout=subprocess.PIPE)
    archive = ROOT / "build/vat-protein-workflows.zip"
    first = archive.read_bytes()
    subprocess.run(command, check=True, cwd=ROOT, stdout=subprocess.PIPE)
    assert archive.read_bytes() == first
    with ZipFile(archive) as bundle:
        assert all(name.startswith("vat-protein-workflows/") for name in bundle.namelist())
        assert "vat-protein-workflows/SKILL.md" in bundle.namelist()
        assert "vat-protein-workflows/scripts/vat_operator.py" in bundle.namelist()
        assert "vat-protein-workflows/runtime/src/vatlog/verify.py" in bundle.namelist()
        assert "vat-protein-workflows/runtime/src/vatlog/roles.py" in bundle.namelist()
        assert "vat-protein-workflows/runtime/src/runner/publication.py" in bundle.namelist()
        assert "vat-protein-workflows/runtime/scripts/run_benchbb_reference.py" in bundle.namelist()
        assert "vat-protein-workflows/runtime/fixtures/benchbb_bbf14_panel.json" in bundle.namelist()


def test_direct_operator_previews_without_modal_dispatch():
    script = ROOT / ".claude/skills/vat-protein-workflows/scripts/vat_operator.py"
    completed = subprocess.run(
        [sys.executable, str(script), "benchbb-preview"], check=True, cwd=ROOT,
        stdout=subprocess.PIPE, text=True,
    )
    result = json.loads(completed.stdout)
    assert result["reviewed_panel"] is True
    assert result["first_call"].endswith(":esmfold2")


def test_direct_operator_refuses_unapproved_modal_dispatch(tmp_path):
    script = ROOT / ".claude/skills/vat-protein-workflows/scripts/vat_operator.py"
    completed = subprocess.run(
        [sys.executable, str(script), "benchbb-run", "--output", str(tmp_path),
         "--compute-budget-usd", "2.0"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert completed.returncode == 2
    assert "explicit owner approval" in json.loads(completed.stderr)["error"]


def test_direct_operator_publication_is_recoverable_and_idempotent(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    store = DesignStore(output / "private")
    try:
        keys = store.keys()
        bundle, _, _ = prepare_fixture("clean", tmp_path / "fixture", keyring=keys)
        bundle.save(output / "bundle.zip")
        (output / "report.json").write_text("{}", encoding="utf-8")
        (output / "operator-state.json").write_text(
            json.dumps({"finalized": True, "session_id": bundle.session_uuid}), encoding="utf-8"
        )
    finally:
        store.close()
    functions = runpy.run_path(
        str(ROOT / ".claude/skills/vat-protein-workflows/scripts/vat_operator.py")
    )
    first = functions["publish"](ROOT, output)
    second = functions["publish"](ROOT, output)
    assert first == second
    assert first["outcome"] == "OK"
    assert first["tree_size"] == 1


def test_local_skill_packages_are_deterministic_and_contain_helpers():
    command = [sys.executable, str(ROOT / "scripts/package_claude_science_skill.py"), *LOCAL_SKILLS]
    subprocess.run(command, check=True, cwd=ROOT, stdout=subprocess.PIPE)
    first = {name: (ROOT / f"build/{name}.zip").read_bytes() for name in LOCAL_SKILLS}
    subprocess.run(command, check=True, cwd=ROOT, stdout=subprocess.PIPE)
    for name in LOCAL_SKILLS:
        archive = ROOT / f"build/{name}.zip"
        assert archive.read_bytes() == first[name]
        with ZipFile(archive) as bundle:
            assert all(path.startswith(f"{name}/") for path in bundle.namelist())
            assert f"{name}/SKILL.md" in bundle.namelist()
            assert any(path.startswith(f"{name}/scripts/") for path in bundle.namelist())


def test_local_evidence_skill_lists_and_inspects_fixtures():
    script = ROOT / ".claude/skills/vat-evidence-review/scripts/review_evidence.py"
    listed = subprocess.run(
        [sys.executable, str(script), "list"], check=True, cwd=ROOT,
        stdout=subprocess.PIPE, text=True,
    )
    assert [(item["fixture"], item["outcome"]) for item in json.loads(listed.stdout)["demos"]] == [
        ("clean", "OK"), ("tampered", "TAMPERED"), ("partial", "INCOMPLETE")
    ]
    inspected = subprocess.run(
        [sys.executable, str(script), "inspect", "clean"], check=True, cwd=ROOT,
        stdout=subprocess.PIPE, text=True,
    )
    report = json.loads(inspected.stdout)["graph"]["bundle_report"]
    assert report["outcome"] == "OK"
    assert len(report["checks"]) == 5
    assert all(check["passed"] for check in report["checks"])


def test_benchbb_skill_recalculates_frozen_panel():
    script = ROOT / ".claude/skills/vat-benchbb-validation/scripts/validate_panel.py"
    completed = subprocess.run(
        [sys.executable, str(script)], check=True, cwd=ROOT,
        stdout=subprocess.PIPE, text=True,
    )
    result = json.loads(completed.stdout)
    assert result["panel_sha256"] == "880ebce29826a9b0b52740bd86b91d69383cc5af878ee016272ed0de68c829fd"
    assert result["analysis"]["binders"]["median"] == 0.5981500744819641
    assert result["analysis"]["non_binders"]["median"] == 0.777061402797699
    assert result["analysis"]["auc"] == 11 / 27
    assert result["analysis"]["two_sided_exact_p"] == 160 / 220
    assert result["analysis"]["permutations"] == 220


def test_demo_generators_validate_without_authorizing_or_starting_work(tmp_path):
    cif = tmp_path / "target.cif"
    cif.write_text("data_target\n#\n", encoding="utf-8")
    registered = subprocess.run(
        [sys.executable, str(ROOT / "scripts/claude_science_demo.py"), "registered",
         "--target-cif", str(cif)], check=True, cwd=ROOT, stdout=subprocess.PIPE,
        env={"PYTHONPATH": str(ROOT / "src")}, text=True)
    registered_value = json.loads(registered.stdout)
    assert all(node["authorized"] is False for node in registered_value["proposal"]["nodes"])
    assert registered_value["dry_run"]["dispatchable"] is False
    generated = subprocess.run(
        [sys.executable, str(ROOT / "scripts/claude_science_demo.py"), "dynamic"],
        check=True, cwd=ROOT, stdout=subprocess.PIPE, env={"PYTHONPATH": str(ROOT / "src")}, text=True)
    dynamic_value = json.loads(generated.stdout)
    assert dynamic_value["proposal"]["authorized"] is False
    assert dynamic_value["proposal"]["resources"]["network"] is False
