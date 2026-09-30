"""One-shot operator staging of every published Open Targets 26.06 table."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import time
import urllib.request

import modal

RELEASE = "26.06"
BASE = f"https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/{RELEASE}/"
INTEGRITY_SHA1 = "e49fc309e6039938e86ad763634abd85e470314e"
VOLUME_NAME = "biodesign-vat-opentargets"
app = modal.App("biodesign-vat-opentargets-stage")
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = modal.Image.debian_slim(python_version="3.12").pip_install("pyarrow==21.0.0")


def fetch_metadata(name: str) -> bytes:
    with urllib.request.urlopen(BASE + name, timeout=120) as response:
        if response.geturl() != BASE + name:
            raise ValueError("Unexpected source redirect")
        raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("Metadata exceeds limit")
    return raw


def inventory(raw: bytes) -> list[tuple[str, str]]:
    if hashlib.sha1(raw).hexdigest() != INTEGRITY_SHA1:
        raise ValueError("Release checksum inventory changed")
    entries = []
    seen = set()
    for line in raw.decode().splitlines():
        digest, path = line.split(None, 1)
        path = path.removeprefix("./")
        if not path.startswith(("output/", "view/")) or not path.endswith(".parquet"):
            continue
        if (".." in PurePosixPath(path).parts or path in seen
                or len(digest) != 40 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("Unsafe or duplicate source entry")
        seen.add(path)
        entries.append((path, digest))
    if len(entries) != 1813 or len({p.rsplit("/", 1)[0] for p, _ in entries}) != 68:
        raise ValueError("Release inventory differs from reviewed collection")
    return sorted(entries)


def inspect_file(path: Path, expected_sha1: str) -> dict:
    import pyarrow.parquet as pq
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            sha1.update(chunk)
            sha256.update(chunk)
    if sha1.hexdigest() != expected_sha1:
        raise ValueError(f"Source checksum mismatch: {path.name}")
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow.serialize().to_pybytes()
        return {"bytes": path.stat().st_size, "sha1": sha1.hexdigest(),
                "sha256": sha256.hexdigest(), "rows": parquet.metadata.num_rows,
                "row_groups": parquet.metadata.num_row_groups,
                "schema_sha256": hashlib.sha256(schema).hexdigest(),
                "schema": str(parquet.schema_arrow)}


def download_file(root: Path, entry: tuple[str, str]) -> dict:
    relative, digest = entry
    # Modal may implement the trusted Volume mount itself with a symlink.
    root = root.resolve(strict=True)
    path = root / relative
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("Symlink in destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return {"path": relative, **inspect_file(path, digest)}
    partial = path.with_name(path.name + ".partial")
    if partial.is_symlink():
        raise ValueError("Symlink in partial destination")
    with urllib.request.urlopen(BASE + relative, timeout=120) as response:
        if response.geturl() != BASE + relative:
            raise ValueError("Unexpected data redirect")
        length = response.headers.get("Content-Length")
        size = 0
        with partial.open("wb") as output:
            while chunk := response.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > 8 * 1024 ** 3:
                    raise ValueError("Unexpected file larger than 8 GiB")
                output.write(chunk)
        if length is not None and size != int(length):
            raise ValueError("Incomplete source response")
    info = inspect_file(partial, digest)
    partial.replace(path)
    return {"path": relative, **info}


@app.function(image=image, volumes={"/data": volume}, cpu=(2, 2), memory=(4096, 4096),
              timeout=7200, retries=0, max_containers=1)
def stage() -> dict:
    started = time.monotonic()
    root = Path("/data") / RELEASE
    root.mkdir(parents=True, exist_ok=True)
    ready = root / "ready.json"
    if ready.exists():
        return {"already_ready": True, **json.loads(ready.read_text())}
    raw = fetch_metadata("release_data_integrity")
    entries = inventory(raw)
    for name in ("release_data_integrity.sha1", "croissant.json", "manifest.json"):
        (root / name).write_bytes(fetch_metadata(name))
    (root / "release_data_integrity").write_bytes(raw)
    volume.commit()
    groups: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        groups.setdefault(entry[0].rsplit("/", 1)[0], []).append(entry)
    files = []
    for group, members in groups.items():
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda entry: download_file(root, entry), members))
        files.extend(results)
        completed_bytes = sum(item["bytes"] for item in files)
        if completed_bytes > 100 * 1024 ** 3:
            raise ValueError("Dataset exceeds 100 GiB staging bound")
        progress = {"release": RELEASE, "files": len(files), "expected_files": len(entries),
                    "bytes": completed_bytes, "last_table": group,
                    "elapsed_seconds": round(time.monotonic() - started, 1)}
        (root / "progress.json").write_text(json.dumps(progress, sort_keys=True))
        volume.commit()
        print(json.dumps(progress), flush=True)
    manifest = {"schema": "opentargets-staging/1", "release": RELEASE,
                "base_url": BASE, "upstream_inventory_sha1": INTEGRITY_SHA1,
                "upstream_inventory_sha256": hashlib.sha256(raw).hexdigest(),
                "validation": "upstream SHA1, local SHA256, Parquet footer/schema/row counts",
                "files": sorted(files, key=lambda item: item["path"])}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    (root / "staging-manifest.json").write_bytes(encoded)
    volume.commit()
    result = {"release": RELEASE, "volume": VOLUME_NAME, "files": len(files),
              "tables": len(groups), "bytes": sum(x["bytes"] for x in files),
              "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
              "elapsed_seconds": round(time.monotonic() - started, 1)}
    ready.write_text(json.dumps(result, sort_keys=True))
    volume.commit()
    return result


if __name__ == "__main__":
    from dotenv import load_dotenv
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid-staging", action="store_true")
    parser.add_argument("--collect", action="store_true", help="Collect the recorded call without submitting work.")
    args = parser.parse_args()
    if not args.confirm_paid_staging and not args.collect:
        parser.error("Obtain owner approval for staging compute and persistent storage, then pass --confirm-paid-staging.")
    repository = Path(__file__).resolve().parents[1]
    load_dotenv(repository / ".env", override=False, interpolate=False)
    state = repository / ".local/opentargets"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    call_path = state / "staging-call.json"
    if args.collect:
        accepted = json.loads(call_path.read_text())
        if accepted.get("status") != "accepted" or not accepted.get("call_id"):
            raise SystemExit("No confirmed call identity; inspect the existing dispatch before proceeding")
        result = modal.FunctionCall.from_id(accepted["call_id"]).get()
        (state / "staging-result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        raise SystemExit(0)
    if call_path.exists():
        raise SystemExit("Existing staging call: reconcile it before submitting another")
    call_path.write_text(json.dumps({"status": "dispatch_intent", "release": RELEASE}))
    with modal.enable_output():
        with app.run(detach=True):
            call = stage.spawn()
            call_path.write_text(json.dumps({"status": "accepted", "call_id": call.object_id,
                                             "release": RELEASE, "volume": VOLUME_NAME}))
            print("Accepted staging call", call.object_id, flush=True)
            result = call.get()
            (state / "staging-result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2), flush=True)
