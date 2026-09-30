"""Read-only live MCP checks; no model calls, credentials, or paid requests."""

import asyncio
import json

from runner.research import ResearchTools, result_failed


def summarize(result: dict) -> dict:
    """Print diagnostic fields, not full articles or abstracts."""
    data = result.get("structuredContent")
    if data is None:
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    data = json.loads(block["text"])
                    break
                except ValueError:
                    pass
    if not isinstance(data, dict):
        data = {}
    return {"failed": result_failed(result), "fields": sorted(data),
            "count": data.get("count", data.get("returned_count", len(data.get("collection", data.get("resultList", {}).get("result", []))))),
            "total": data.get("total", data.get("hitCount", data.get("total_count"))),
            "title": (data.get("preprint") or {}).get("title"), "error": data.get("error", result.get("error"))}


async def main() -> None:
    checks = []
    async with ResearchTools(print) as tools:
        print("Available:", ", ".join(tool["function"]["name"] for tool in tools.tools))
        result = await tools.call("pubmed__search_articles", {"query": "GFP nanobody", "max_results": 2})
        print("PubMed search:", json.dumps(summarize(result)))
        checks.append(not result_failed(result))
        result = await tools.call("europepmc__search_preprints", {"query": "GFP nanobody", "page_size": 2})
        print("Europe PMC preprint keywords:", json.dumps(summarize(result)))
        checks.append(not result_failed(result))
        for doi in ["10.1101/2025.11.20.689494", "not-a-doi"]:
            result = await tools.call("biorxiv__get_preprint", {"doi": doi})
            print("bioRxiv DOI", doi, json.dumps(summarize(result)))
            checks.append(result_failed(result) if doi == "not-a-doi" else not result_failed(result))
    if not all(checks):
        raise SystemExit("One or more live MCP checks failed; see diagnostics above.")
    print("All live research MCP/API checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
