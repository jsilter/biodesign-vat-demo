"""CPU-only, real-data search checks; this is not a deployed search service."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import time
from uuid import uuid4

import modal

app = modal.App("biodesign-vat-opentargets-search-check")
volume = modal.Volume.from_name("biodesign-vat-opentargets")
image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "duckdb==1.4.4", "pyarrow==21.0.0")
MANIFEST_SHA256 = "b5afb8d140e737b6fce3c2c60f841d0cadbe4fc4409b9af2926c7393711ed784"


def run_checks(root_path: str) -> dict:
    """Compare fixed DuckDB searches with independent PyArrow reads of staged bytes."""
    import duckdb
    import pyarrow
    import pyarrow.dataset as ds

    pyarrow.set_cpu_count(2)
    started = time.monotonic()
    root = Path(root_path)
    raw_manifest = (root / "staging-manifest.json").read_bytes()
    if hashlib.sha256(raw_manifest).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Unexpected staged release manifest")
    if json.loads((root / "ready.json").read_text())["manifest_sha256"] != MANIFEST_SHA256:
        raise ValueError("Dataset is not ready at the pinned identity")
    inventory = json.loads(raw_manifest)["files"]
    report: dict = {"release": "26.06", "manifest_sha256": MANIFEST_SHA256,
                    "duckdb": duckdb.__version__, "pyarrow": pyarrow.__version__, "checks": [], "queries": []}
    connection = duckdb.connect(config={"threads": 2, "memory_limit": "2GB",
                                       "temp_directory": "/tmp/duckdb",
                                       "max_temp_directory_size": "8GB",
                                       "autoinstall_known_extensions": False,
                                       "autoload_known_extensions": False})

    def verify(name: str, condition: bool, detail: object = None) -> None:
        report["checks"].append({"name": name, "passed": bool(condition), "detail": detail})

    def query(name: str, sql: str, params: list | None = None) -> list[dict]:
        before = time.monotonic()
        cursor = connection.execute(sql, params or [])
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, values)) for values in cursor.fetchall()]
        report["queries"].append({"name": name, "seconds": round(time.monotonic()-before, 4),
                                  "rows": len(rows)})
        return rows

    tables = ["disease", "target", "association_overall_direct", "association_overall_indirect",
              "association_by_datasource_direct", "evidence_eva", "baseline_expression",
              "target_prioritisation", "drug_mechanism_of_action"]
    source_files: dict[str, list[str]] = {}
    for table in tables:
        files = [item for item in inventory if item["path"].startswith(f"output/{table}/")]
        if not files:
            raise ValueError(f"Missing source table {table}")
        for item in files:
            if (root / item["path"]).stat().st_size != item["bytes"]:
                raise ValueError(f"Changed source file length: {item['path']}")
        source_files[table] = [str(root / item["path"]) for item in files]
        connection.read_parquet(source_files[table]).create_view(table)

    disease_sql = """
        SELECT id, name FROM disease
        WHERE contains(lower(id), lower(?)) OR contains(lower(name), lower(?))
           OR EXISTS (SELECT 1 FROM unnest(exactSynonyms) AS s(synonym)
                      WHERE contains(lower(synonym), lower(?)))
        ORDER BY CASE WHEN lower(id)=lower(?) OR lower(name)=lower(?) THEN 0
                      WHEN EXISTS (SELECT 1 FROM unnest(exactSynonyms) AS s(synonym)
                                   WHERE lower(synonym)=lower(?)) THEN 1
                      WHEN starts_with(lower(name),lower(?)) THEN 2 ELSE 3 END, id
        LIMIT 20
    """

    def search(text: str, label: str) -> list[dict]:
        return query(label, disease_sql, [text] * 7)

    names = ["cystic fibrosis", "Alzheimer disease", "breast cancer", "type 2 diabetes mellitus",
             "Crohn's disease"]
    diseases = {}
    for name in names:
        rows = search(name, f"disease:{name}:first")
        repeated = search(name.upper(), f"disease:{name}:repeat_casefold")
        verify(f"case-insensitive disease lookup: {name}", rows == repeated and bool(rows))
        canonical_name = "Crohn disease" if name == "Crohn's disease" else name
        exact = [row for row in rows if row["name"].lower() == canonical_name.lower()]
        verify(f"canonical disease or exact alias first: {name}", len(exact) == 1 and rows[:1] == exact)
        if exact:
            diseases[name] = exact[0]
    report["diseases"] = diseases

    disease_rows = ds.dataset(source_files["disease"], format="parquet").to_table(
        columns=["id", "name", "exactSynonyms", "descendants"]).to_pylist()
    for name, disease in diseases.items():
        verify(f"disease ID/name agrees with PyArrow: {name}", any(
            row["id"] == disease["id"] and row["name"] == disease["name"]
            and name.lower() in [row["name"].lower(), *[s.lower() for s in row["exactSynonyms"] or []]]
            for row in disease_rows))
    cf = diseases.get("cystic fibrosis")
    if cf is None:
        report["passed"] = False
        return report
    cf_row = next(row for row in disease_rows if row["id"] == cf["id"])
    synonyms = cf_row["exactSynonyms"] or []
    verify("cystic fibrosis synonym available", bool(synonyms))
    if synonyms:
        alias = synonyms[0]
        hits = search(alias, "disease:synonym")
        verify("synonym resolves to source disease", bool(hits) and hits[0]["id"] == cf["id"], alias)
    verify("identifier lookup", search(cf["id"], "disease:identifier")[0]["id"] == cf["id"])
    for text in ["VAT_NONEXISTENT_DISEASE_482913", "' OR 1=1 --", "cystic fibrosiss"]:
        hits = search(text, f"disease:negative:{text}")
        independent = [row for row in disease_rows if any(text.lower() in str(value).lower()
                       for value in [row["id"], row["name"], *(row["exactSynonyms"] or [])])]
        verify(f"literal unmatched input: {text}", hits == [] and independent == [])
    report["fuzzy_search"] = "Not implemented; misspellings can return no matches."

    targets = ds.dataset(source_files["target"], format="parquet").to_table(
        columns=["id", "approvedSymbol", "approvedName"]).to_pylist()
    target_map = {row["id"]: row for row in targets}
    gene_rows = query("target:casefold_symbol", """
        SELECT id, approvedSymbol, approvedName FROM target
        WHERE lower(approvedSymbol) = lower(?) ORDER BY id
    """, ["cftr"])
    expected_gene = sorted([x for x in targets if x["approvedSymbol"].lower() == "cftr"], key=lambda x: x["id"])
    verify("gene symbol lookup agrees with PyArrow", gene_rows == expected_gene and len(gene_rows) == 1)
    if len(gene_rows) != 1:
        report["passed"] = False
        return report
    gene_id = gene_rows[0]["id"]
    report["target"] = gene_rows[0]

    ranking_sql = """
        SELECT a.targetId, t.approvedSymbol, a.associationScore, a.evidenceCount
        FROM association_overall_direct a JOIN target t ON a.targetId=t.id
        WHERE a.diseaseId=? ORDER BY a.associationScore DESC, a.targetId LIMIT ? OFFSET ?
    """
    results = {}
    for name, disease in diseases.items():
        rows = query(f"rank:{name}:first", ranking_sql, [disease["id"], 10, 0])
        warm = query(f"rank:{name}:repeat", ranking_sql, [disease["id"], 10, 0])
        verify(f"stable repeated ranking: {name}", rows == warm)
        results[name] = rows
    report["rankings"] = results
    ids = [row["id"] for row in diseases.values()]
    association_rows = ds.dataset(source_files["association_overall_direct"], format="parquet").to_table(
        columns=["diseaseId", "targetId", "associationScore", "evidenceCount"],
        filter=ds.field("diseaseId").isin(ids)).to_pylist()
    for name, disease in diseases.items():
        source = [row for row in association_rows if row["diseaseId"] == disease["id"]]
        verify(f"unique target/disease rows: {name}", len(source) == len({x["targetId"] for x in source}))
        verify(f"all associated target IDs resolve: {name}", all(x["targetId"] in target_map for x in source))
        ordered = sorted(source, key=lambda x: (-x["associationScore"], x["targetId"]))[:10]
        expected = [{"targetId": row["targetId"], "approvedSymbol": target_map[row["targetId"]]["approvedSymbol"],
                     "associationScore": row["associationScore"], "evidenceCount": row["evidenceCount"]}
                    for row in ordered]
        verify(f"scores/counts/top ten agree with PyArrow: {name}", results[name] == expected and bool(expected))
    first_page = query("rank:page_one", ranking_sql, [cf["id"], 5, 0])
    second_page = query("rank:page_two", ranking_sql, [cf["id"], 5, 5])
    verify("two pages equal independently checked top ten", first_page+second_page == results["cystic fibrosis"])
    report["pagination_limit"] = "Tests stable LIMIT/OFFSET; production cursors are not implemented."

    evidence_sql = """SELECT id,targetId,diseaseId,score,literature,clinicalSignificances
        FROM evidence_eva WHERE diseaseId=? AND targetId=? ORDER BY id"""
    evidence = query("evidence:CFTR_cystic_fibrosis", evidence_sql, [cf["id"], gene_id])
    evidence_source = ds.dataset(source_files["evidence_eva"], format="parquet").to_table(
        columns=["id", "targetId", "diseaseId", "score", "literature", "clinicalSignificances"],
        filter=(ds.field("diseaseId") == cf["id"]) & (ds.field("targetId") == gene_id)).to_pylist()
    verify("evidence rows and citations agree with PyArrow", evidence == sorted(evidence_source, key=lambda x:x["id"])
           and bool(evidence))
    report["evidence"] = {"count": len(evidence), "sample": evidence[:3]}
    sources = query("association:source_breakdown", """SELECT aggregationType,aggregationValue,
        associationScore,evidenceCount FROM association_by_datasource_direct
        WHERE diseaseId=? AND targetId=? ORDER BY aggregationValue""", [cf["id"],gene_id])
    report["source_breakdown"] = sources
    eva = [x for x in sources if x["aggregationValue"] == "eva"]
    positive_scores = sorted([row["score"] for row in evidence_source if row["score"] > 0], reverse=True)
    zero_count = sum(row["score"] == 0 for row in evidence_source)
    report["evidence"].update({"positive_score_count": len(positive_scores), "zero_score_count": zero_count})
    verify("raw EVA evidence retained including zero scores", len(evidence) == len(positive_scores) + zero_count
           and zero_count > 0)
    verify("EVA association count matches positive-score evidence",
           len(eva) == 1 and eva[0]["evidenceCount"] == len(positive_scores))
    reconstructed = math.fsum(score / rank**2 for rank, score in enumerate(positive_scores[:50], 1)) / math.fsum(
        1 / rank**2 for rank in range(1, 1001))
    verify("EVA score agrees with independent top-50 harmonic reconstruction",
           len(eva) == 1 and math.isclose(eva[0]["associationScore"], reconstructed, rel_tol=0, abs_tol=1e-12),
           {"reconstructed": reconstructed, "stored": eva[0]["associationScore"] if eva else None})

    breast = diseases.get("breast cancer")
    if breast:
        direct_count = query("scope:direct_count", "SELECT count(*) AS n FROM association_overall_direct WHERE diseaseId=?", [breast["id"]])[0]["n"]
        indirect_count = query("scope:indirect_count", "SELECT count(*) AS n FROM association_overall_indirect WHERE diseaseId=?", [breast["id"]])[0]["n"]
        indirect_source = ds.dataset(source_files["association_overall_indirect"], format="parquet").count_rows(
            filter=ds.field("diseaseId")==breast["id"])
        verify("descendant-inclusive count agrees with PyArrow", indirect_count == indirect_source)
        verify("direct and descendant-inclusive scope differ", indirect_count > direct_count,
               {"direct": direct_count,"including_descendants": indirect_count})

    detail = query("target:prioritisation", "SELECT targetId,isInMembrane,isSecreted,hasSafetyEvent,maxClinicalStage FROM target_prioritisation WHERE targetId=?", [gene_id])
    verify("target prioritisation annotation joins", len(detail)==1 and detail[0]["targetId"]==gene_id)
    report["prioritisation"] = detail
    expression = query("target:expression", """SELECT datasourceId,unit,count(*) AS n,
        count(median) AS nonnull_medians FROM baseline_expression WHERE targetId=?
        GROUP BY datasourceId,unit ORDER BY datasourceId,unit""", [gene_id])
    expression_count = ds.dataset(source_files["baseline_expression"], format="parquet").count_rows(filter=ds.field("targetId")==gene_id)
    verify("expression counts agree with PyArrow", sum(x["n"] for x in expression)==expression_count and expression_count>0)
    report["expression"] = expression
    mechanisms = query("target:drug_mechanisms", """SELECT actionType,mechanismOfAction,chemblIds
        FROM drug_mechanism_of_action WHERE list_contains(targets,?)
        ORDER BY actionType,mechanismOfAction""", [gene_id])
    mechanism_source = ds.dataset(source_files["drug_mechanism_of_action"], format="parquet").to_table().to_pylist()
    verify("drug mechanism lookup count agrees with PyArrow", len(mechanisms)==sum(gene_id in (x["targets"] or []) for x in mechanism_source) and bool(mechanisms))
    report["drug_mechanisms"] = mechanisms[:5]
    report["elapsed_seconds"] = round(time.monotonic()-started, 3)
    report["passed"] = all(check["passed"] for check in report["checks"])
    report["limits"] = ["Ad hoc fixed-query harness, not an application endpoint",
                        "No fuzzy search or production cursor tests",
                        "First/repeat timings share a process; OS cache may already be warm",
                        "Not a therapeutic efficacy or binder-suitability assessment",
                        "Manifest hash and used file lengths checked; content was hashed during acquisition",
                        "No independent egress probe performed",
                        "No VAT query capture implemented"]
    connection.close()
    return report


@app.function(image=image, volumes={"/data": volume}, cpu=(2, 2), memory=(8192, 8192),
              timeout=900, retries=0, max_containers=1, block_network=True)
def check_search() -> dict:
    """Run the fixed checks in a network-blocked Modal worker when authorized."""
    return {**run_checks("/data/26.06"), "execution": "Modal"}


if __name__ == "__main__":
    from dotenv import load_dotenv
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="Run the bounded CPU-only live-data check.")
    mode.add_argument("--collect", type=Path, help="Collect the call recorded in this JSON file.")
    mode.add_argument("--local", type=Path, help="Test a local copy without starting Modal compute.")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    load_dotenv(repository / ".env", override=False, interpolate=False)
    state = repository / ".local/opentargets"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    if args.local:
        call_file = state / f"search-check-local-{uuid4()}.json"
        result = {**run_checks(str(args.local)), "execution": "local"}
    elif args.collect:
        call_file = args.collect
        saved = json.loads(call_file.read_text())
        if saved.get("status") != "accepted":
            raise SystemExit("Call acceptance is unresolved; do not resubmit")
        result = modal.FunctionCall.from_id(saved["call_id"]).get()
    elif args.run:
        call_file = state / f"search-check-{uuid4()}.json"
        call_file.write_text(json.dumps({"status":"dispatch_intent"}))
        with modal.enable_output(), app.run(detach=True):
            call = check_search.spawn()
            call_file.write_text(json.dumps({"status":"accepted", "call_id":call.object_id}))
            print("Accepted", call.object_id, "receipt", str(call_file), flush=True)
            result = call.get()
    else:
        parser.error("Choose --local PATH, --run, or --collect PATH")
    destination = call_file.with_suffix(".result.json")
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("Saved", destination)
    raise SystemExit(0 if result["passed"] else 1)
