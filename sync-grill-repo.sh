#!/usr/bin/env bash
# Sync the grill-repo skill from its single source of truth to every install target.
#   - Claude Code, Codex, zcode, kimicode, Muse Code, Pi, and Antigravity CLI use SKILL.md verbatim AND get the
#     bundled scripts/ helper (repo-scan.py) copied alongside, so the installed skill can run it.
#   - Cursor consumes an .mdc rule with its own frontmatter (description / globs / alwaysApply),
#     so we strip SKILL.md's YAML frontmatter and prepend the Cursor block below.
#
# Each target's skills dir is canonicalised (pwd -P resolves symlinks) so symlinked agent roots
# are registered once without duplicate writes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$SCRIPT_DIR/SKILL.md}"
SCRIPTS_SRC="${SCRIPTS_SRC:-$SCRIPT_DIR/scripts}"

[ -f "$SRC" ]         || { echo "✗ source not found: $SRC" >&2; exit 1; }
[ -d "$SCRIPTS_SRC" ] || { echo "✗ scripts dir not found: $SCRIPTS_SRC" >&2; exit 1; }

written=""
for root in "$HOME/.claude" "$HOME/.codex" \
  "$HOME/.config/muse" "$HOME/.gemini/config" "$HOME/.pi/agent" "$HOME/.zcode" "$HOME/.kimicode"; do
  skills_dir="$root/skills"
  mkdir -p "$skills_dir"
  canon="$(cd "$skills_dir" && pwd -P)"
  if printf '%s' "$written" | grep -qxF "$canon"; then
    echo "↩ skip    $skills_dir  (symlink → already-synced target; not registering twice)"
    continue
  fi
  written="$written$canon"$'\n'
  skill_dst="$skills_dir/grill-repo"
  mkdir -p "$skill_dst"
  cp "$SRC" "$skill_dst/SKILL.md"
  echo "✓ synced  $skill_dst/SKILL.md"
  rm -rf "$skill_dst/scripts"
  cp -R "$SCRIPTS_SRC" "$skill_dst/scripts"
  rm -rf "$skill_dst/scripts/__pycache__"
  rm -f "$skill_dst/scripts"/test_*.py
  echo "✓ synced  $skill_dst/scripts/  (bundled helper — repo-scan.py)"
done

CURSOR_RULES_DIR="${CURSOR_RULES_DIR:-$HOME/.cursor/rules}"
CURSOR="$CURSOR_RULES_DIR/grill-repo.mdc"
mkdir -p "$(dirname "$CURSOR")"
{
  cat <<'CURSOR_FRONTMATTER'
---
description: Use when you want a git repo scanned into a persisted, navigable map — a per-file digest plus a synthesised overview at a stable gitignored path — so future sessions and dispatched subagents read the map instead of re-reading the whole tree, with content-hash fingerprints that make freshness a two-command check and re-runs incremental. It maps the repo; it never grades, reviews, or commits it. Triggers: "map this repo", "scan the repo so subagents can pick it up", "build or refresh the repo map", "is there a repo map / is it fresh", "refresh the repo map".
globs:
alwaysApply: false
---
CURSOR_FRONTMATTER
  awk 'f>=2{print} /^---$/{f++}' "$SRC"
} > "$CURSOR"
echo "✓ synced  $CURSOR  (Cursor frontmatter applied)"

echo "Done — grill-repo synced (SKILL.md + scripts/) to Claude, Codex, zcode, kimicode, muse, antigravity, pi, and Cursor (.mdc)."
