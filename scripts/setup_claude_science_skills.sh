#!/bin/bash
# Validate, link, package, and preview the repository-managed Claude Science skills.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.claude-science-venv/bin/python"
CLAUDE_SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
SKILLS=(vat-protein-workflows vat-evidence-review vat-benchbb-validation)

if [[ ! -x "$PYTHON" ]]; then
  echo "repository-contained Claude Science Python is missing: $PYTHON" >&2
  exit 1
fi
if ! "$PYTHON" -c "import cryptography, modal, pydantic, pymerkle, rfc8785"; then
  echo "the existing Claude Science environment lacks required VAT dependencies" >&2
  exit 1
fi

mkdir -p "$CLAUDE_SKILLS"
for name in "${SKILLS[@]}"; do
  source="$ROOT/.claude/skills/$name"
  target="$CLAUDE_SKILLS/$name"
  if [[ -L "$target" ]]; then
    if [[ "$(readlink -f "$target")" != "$(readlink -f "$source")" ]]; then
      echo "refusing to replace a symlink to another skill: $target" >&2
      exit 1
    fi
  elif [[ -e "$target" ]]; then
    echo "refusing to replace an existing non-symlink: $target" >&2
    exit 1
  else
    ln -s "$source" "$target"
  fi
done

"$PYTHON" "$ROOT/scripts/package_claude_science_skill.py"
"$PYTHON" "$ROOT/.claude/skills/vat-protein-workflows/scripts/vat_operator.py" \
  --repo "$ROOT" benchbb-preview >/dev/null

echo
echo "Local setup passed. Claude skills are linked from:"
echo "  $ROOT/.claude/skills"
echo
echo "Upload this file in Claude Science:"
echo "  $ROOT/build/vat-protein-workflows.zip"
