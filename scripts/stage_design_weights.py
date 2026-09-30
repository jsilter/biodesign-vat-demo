"""Stage pinned weight repositories using hf; optionally upload after local review.

This downloads tens of GB. No GPU execution is started. Run from the repo environment.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from runner.design_workers import (BOLTZ_REVISION, ESM_REVISION, ESM3_REVISION, PROTEINMPNN_REVISION,
                                   PROTEINMPNN_REPOSITORY, PROTEINMPNN_CHECKPOINT,
                                   PROTEINMPNN_SOLUBLE_CHECKPOINT, PROTEINMPNN_LICENSE,
                                   WEIGHTS, ESM3_WEIGHTS, CHEMISTRY, validate_weight_lock)
from runner.design_store import json_bytes

MODEL_WEIGHTS = {**WEIGHTS, "esm3": ESM3_WEIGHTS}


def volume_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,62})", value):
        raise argparse.ArgumentTypeError("volume must be a lowercase Modal name up to 63 characters")
    return value


def hf_environment(*, public: bool) -> dict[str, str]:
    """Build an explicit subprocess environment; public mode cannot leak a Hub token."""
    environment = os.environ.copy()
    if public:
        for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"):
            environment.pop(name, None)
        environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--review-and-upload", action="store_true")
    parser.add_argument("--public-download", action="store_true",
                        help="download public repositories without sending a configured Hub token")
    parser.add_argument("--volume", type=volume_name, default="biodesign-vat-weights")
    parser.add_argument("--tool", action="append", choices=[*MODEL_WEIGHTS, "proteinmpnn", "proteinmpnn-soluble"],
                        help="Download one tool's weights; repeat as needed. Default downloads all.")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    selected = args.tool or [*MODEL_WEIGHTS, "proteinmpnn", "proteinmpnn-soluble"]
    download_environment = hf_environment(public=args.public_download)
    if args.download:
        hf = shutil.which("hf")
        if hf is None and any(tool in MODEL_WEIGHTS for tool in selected):
            parser.error("Install huggingface_hub CLI; the command is hf, not huggingface-cli.")
        for tool in (tool for tool in selected if tool in MODEL_WEIGHTS):
            repo, revision = MODEL_WEIGHTS[tool]
            subprocess.run([hf, "download", repo, "--revision", revision, "--local-dir", str(root/tool)],
                           check=True, env=download_environment)
        if "esmfold2" in selected:
            subprocess.run([hf, "download", CHEMISTRY[0], "ccd.pkl", "--revision", CHEMISTRY[1],
                            "--local-dir", str(root/"esmfold2")], check=True, env=download_environment)
        if "boltzgen" in selected:
            subprocess.run([hf, "download", "boltzgen/inference-data", "--type", "dataset", "--revision",
                            "c3d36fd276e9caf098c75d4113c6d5eb320b1a4c", "--local-dir", str(root/"mols")],
                           check=True, env=download_environment)
        if {"proteinmpnn", "proteinmpnn-soluble"}.intersection(selected):
            with tempfile.TemporaryDirectory(prefix="proteinmpnn-source-") as temporary:
                repository = Path(temporary) / "repository"
                subprocess.run(["git", "init", str(repository)], check=True)
                subprocess.run(["git", "-C", str(repository), "remote", "add", "origin",
                                PROTEINMPNN_REPOSITORY], check=True)
                subprocess.run(["git", "-C", str(repository), "fetch", "--depth", "1", "origin",
                                PROTEINMPNN_REVISION], check=True)
                variants = [("proteinmpnn", "vanilla_model_weights/v_48_020.pt", PROTEINMPNN_CHECKPOINT),
                            ("proteinmpnn-soluble", "soluble_model_weights/v_48_020.pt", PROTEINMPNN_SOLUBLE_CHECKPOINT)]
                for tool, source, digest in variants:
                    if tool not in selected:
                        continue
                    destination = root / tool
                    destination.mkdir(exist_ok=True)
                    for source_name, name in [(source, "v_48_020.pt"), ("LICENSE", "LICENSE")]:
                        raw = subprocess.run(["git", "-C", str(repository), "show", f"FETCH_HEAD:{source_name}"],
                                             check=True, capture_output=True).stdout
                        (destination / name).write_bytes(raw)
                    if (hashlib.sha256((destination / "v_48_020.pt").read_bytes()).hexdigest() != digest
                            or hashlib.sha256((destination / "LICENSE").read_bytes()).hexdigest() != PROTEINMPNN_LICENSE):
                        parser.error(f"Downloaded {tool} checkpoint or licence does not match the reviewed digest.")
    files = {}
    staged = [*selected, *(["mols"] if "boltzgen" in selected else [])]
    for tool in staged:
        directory = root/tool
        if not directory.is_dir():
            parser.error(f"Missing staged {tool} directory; use --download first.")
        for path in sorted(directory.rglob("*")):
            if path.is_file() and ".cache" not in path.parts:
                if path.is_symlink():
                    parser.error("Staged files cannot be symlinks.")
                with path.open("rb") as stream:
                    files[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    lock = {"reviewed": args.review_and_upload, "esm_revision": ESM_REVISION,
            "esm3_revision": ESM3_REVISION, "boltz_revision": BOLTZ_REVISION,
            "proteinmpnn_revision": PROTEINMPNN_REVISION,
            "proteinmpnn_repository": PROTEINMPNN_REPOSITORY,
            "repositories": {k: list(v) for k, v in MODEL_WEIGHTS.items()}, "chemistry": list(CHEMISTRY), "files": files}
    lock["identity"] = hashlib.sha256(json_bytes(lock)).hexdigest()
    (root/"deployment-lock.json").write_text(json.dumps(lock, indent=2))
    print("Wrote deployment lock. Review versions, licences and hashes before enabling GPU execution.")
    if args.review_and_upload:
        for tool in selected:
            validate_weight_lock(root, lock, tool)
        import modal
        volume = modal.Volume.from_name(args.volume, create_if_missing=True)
        with volume.batch_upload() as upload:
            for name in files:
                upload.put_file(root / name, "/" + name)
            upload.put_file(root / "deployment-lock.json", "/deployment-lock.json")
        print("Uploaded reviewed weights. This is not a successful GPU validation report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
