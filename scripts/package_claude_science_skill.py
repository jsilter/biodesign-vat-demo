"""Create deterministic upload ZIPs for the Claude Science skills."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


SKILLS = ("vat-protein-workflows", "vat-evidence-review", "vat-benchbb-validation")
RUNTIME_FILES = (
    "pyproject.toml",
    "scripts/run_benchbb_reference.py",
    "fixtures/benchbb_bbf14_panel.json",
)
RUNTIME_TREES = ("src/vatlog", "src/runner", "src/demo")


def archive_file(archive: ZipFile, source: Path, destination: Path) -> None:
    """Write one file with stable metadata."""
    info = ZipInfo(destination.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, source.read_bytes())


def source_files(root: Path):
    """Yield source files while excluding interpreter caches from uploads."""
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


def package(root: Path, name: str) -> Path:
    """Package one named repository skill and return the archive path."""
    source = root / ".claude" / "skills" / name
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"unknown Claude Science skill: {name}")
    target = root / "build" / f"{name}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in source_files(source):
            relative = Path(source.name) / path.relative_to(source)
            archive_file(archive, path, relative)
        if name == "vat-protein-workflows":
            runtime = Path(source.name) / "runtime"
            for relative in RUNTIME_FILES:
                archive_file(archive, root / relative, runtime / relative)
            for tree in RUNTIME_TREES:
                for path in source_files(root / tree):
                    archive_file(archive, path, runtime / path.relative_to(root))
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skills", nargs="*", metavar="SKILL")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    selected = args.skills or SKILLS
    unknown = set(selected) - set(SKILLS)
    if unknown:
        parser.error("unknown skill: " + ", ".join(sorted(unknown)))
    for name in selected:
        print(package(root, name))


if __name__ == "__main__":
    main()
