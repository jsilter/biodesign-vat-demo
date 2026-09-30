"""The self-contained evidence page embeds the verifier's projection and fetches nothing."""

import json
from pathlib import Path

import pytest

from runner import claude_science_mcp as connector
from runner import evidence_html
from demo.demo import prepare_fixture
from runner.graph import project

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ('<!doctype html><html><head><title>VAT evidence</title>'
            f'{evidence_html.PLACEHOLDER}</head><body><div id="root"></div></body></html>')


def embedded(page: str) -> dict:
    start = page.index('<script id="vat-evidence" type="application/json">') + len('<script id="vat-evidence" type="application/json">')
    return json.loads(page[start:page.index("</script>", start)])


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    return project(*prepare_fixture("clean", tmp_path_factory.mktemp("fixtures")))


def test_page_embeds_the_projection_exactly_and_escapes_every_tag_end(graph):
    hostile = {**graph, "trust": 'keys </script><script>alert(1)</script> & <!-- -->'}
    page = evidence_html.render(hostile, "Clean <run> & co", TEMPLATE)
    assert page.count("<script") == 1 and "alert(1)</script>" not in page
    assert "<title>Clean &lt;run&gt; &amp; co</title>" in page
    assert embedded(page) == hostile


def test_render_refuses_an_absent_template_and_a_wrong_one(graph, tmp_path):
    with pytest.raises(FileNotFoundError, match="npm --prefix web run build:static"):
        evidence_html.load_template(tmp_path / "missing.html")
    (tmp_path / "other.html").write_text("<html></html>")
    with pytest.raises(ValueError, match="not the static evidence viewer"):
        evidence_html.load_template(tmp_path / "other.html")
    with pytest.raises(ValueError, match="verification graph"):
        evidence_html.render({"nodes": []}, "x", TEMPLATE)
    with pytest.raises(ValueError, match="Title"):
        evidence_html.render(graph, " ", TEMPLATE)


def test_cli_verifies_offline_and_writes_the_page(tmp_path, capsys):
    if not evidence_html.TEMPLATE.is_file():
        pytest.skip(f"static viewer not built: {evidence_html.BUILD_COMMAND}")
    bundle, trust, _ = prepare_fixture("tampered", tmp_path)
    bundle.save(tmp_path / "bundle.zip")
    trust.save(tmp_path / "trust.json")
    out = tmp_path / "tampered.html"
    assert evidence_html.main([str(tmp_path / "bundle.zip"), str(tmp_path / "trust.json"),
                               "--out", str(out), "--title", "Tampered"]) == 0
    page = out.read_text(encoding="utf-8")
    assert embedded(page)["bundle_report"]["outcome"] == "TAMPERED"
    assert "<title>Tampered</title>" in page and 'src="./' not in page and 'href="./' not in page
    assert f"wrote {out}" in capsys.readouterr().out


def test_connector_writes_pages_only_as_html_under_the_evidence_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("VAT_EVIDENCE_DIR", str(tmp_path))
    (tmp_path / "pages").mkdir()
    outside = tmp_path.parent / "escape.html"
    assert connector.write_local("pages/one.html", b"x") == tmp_path / "pages" / "one.html"
    for path in ("../escape.html", str(outside), "notes.txt", "missing/dir.html", "."):
        with pytest.raises(RuntimeError):
            connector.write_local(path, b"x")
    assert not outside.exists()


def test_connector_renders_a_loaded_demo_in_process(tmp_path, monkeypatch):
    if not evidence_html.TEMPLATE.is_file():
        pytest.skip(f"static viewer not built: {evidence_html.BUILD_COMMAND}")
    monkeypatch.delenv("VAT_BACKEND_URL", raising=False)
    monkeypatch.setenv("VAT_BACKEND_MODE", "in-process")
    monkeypatch.setenv("VAT_EVIDENCE_DIR", str(tmp_path))
    result = connector.render_demo_evidence_page("clean", "clean.html")
    assert result["outcome"] == "OK" and result["path"] == str(tmp_path / "clean.html")
    page = (tmp_path / "clean.html").read_text(encoding="utf-8")
    assert result["bytes"] == len(page.encode()) and "<title>VAT demo evidence: clean</title>" in page
    assert embedded(page)["bundle_report"]["outcome"] == "OK"
