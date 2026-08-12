#!/usr/bin/env bash

# Self-contained validation for gauntlet-loop. No network access required;
# every check runs against this checkout and disposable temp directories.

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_root="$(mktemp -d)"
trap 'rm -rf "$tmp_root"' EXIT

pass=0
fail=0

ok() { pass=$((pass + 1)); echo "ok - $1"; }
bad() { fail=$((fail + 1)); echo "FAIL - $1"; }

fake_home() {
  home_dir="$tmp_root/$1"
  mkdir -p "$home_dir"
  printf '%s\n' "$home_dir"
}

# --- shell syntax ------------------------------------------------------

if bash -n "$repo_dir/scripts/install.sh"; then
  ok "scripts/install.sh has valid shell syntax"
else
  bad "scripts/install.sh has invalid shell syntax"
fi

if bash -n "$repo_dir/scripts/validate.sh"; then
  ok "scripts/validate.sh has valid shell syntax"
else
  bad "scripts/validate.sh has invalid shell syntax"
fi

if PYTHONPYCACHEPREFIX="$tmp_root/pycache" python3 -m py_compile \
  "$repo_dir/scripts/gauntlet.py" "$repo_dir/scripts/test_gauntlet.py"; then
  ok "Python runner and tests compile"
else
  bad "Python runner or tests have invalid syntax"
fi

# --- SKILL.md frontmatter ------------------------------------------------

skill_md="$repo_dir/SKILL.md"

if [ "$(sed -n '1p' "$skill_md")" = "---" ]; then
  ok "SKILL.md opens with a frontmatter fence"
else
  bad "SKILL.md does not open with a frontmatter fence"
fi

second_fence_line="$(awk '/^---$/ {n++; if (n == 2) {print NR; exit}}' "$skill_md")"
if [ -n "${second_fence_line:-}" ]; then
  ok "SKILL.md frontmatter fence closes"
else
  bad "SKILL.md frontmatter fence never closes"
fi

frontmatter="$(sed -n "2,$(( ${second_fence_line:-1} - 1 ))p" "$skill_md")"

if printf '%s\n' "$frontmatter" | grep -q '^name: gauntlet-loop$'; then
  ok "SKILL.md declares name: gauntlet-loop"
else
  bad "SKILL.md is missing name: gauntlet-loop"
fi

if printf '%s\n' "$frontmatter" | grep -q '^description: .\+'; then
  ok "SKILL.md declares a non-empty description"
else
  bad "SKILL.md is missing a non-empty description"
fi

if [ -n "${second_fence_line:-}" ] && sed -n "$((second_fence_line + 1)),\$p" "$skill_md" | grep -q '[^[:space:]]'; then
  ok "SKILL.md has content after the frontmatter"
else
  bad "SKILL.md has no content after the frontmatter"
fi

# --- clean link install (all clients, default destinations) -------------

link_home="$(fake_home link-all)"
if output="$(HOME="$link_home" "$repo_dir/scripts/install.sh" all 2>&1)"; then
  ok "link install for all clients succeeds"
else
  bad "link install for all clients failed: $output"
fi

claude_dest="$link_home/.claude/skills/gauntlet-loop"
codex_dest="$link_home/.codex/skills/gauntlet-loop"

if [ -L "$claude_dest" ] && [ "$(readlink "$claude_dest")" = "$repo_dir" ]; then
  ok "claude destination links to this checkout"
else
  bad "claude destination is not a symlink to this checkout"
fi

if [ -L "$codex_dest" ] && [ "$(readlink "$codex_dest")" = "$repo_dir" ]; then
  ok "codex destination links to this checkout"
else
  bad "codex destination is not a symlink to this checkout"
fi

# tfcode's default falls back to the same directory as claude's, so no
# separate ~/.tfcode/skill entry should be created by default.
if [ ! -e "$link_home/.tfcode/skill/gauntlet-loop" ]; then
  ok "tfcode default install shares claude's destination (no separate copy)"
else
  bad "tfcode default install unexpectedly created a separate destination"
fi

# --- clean copy install, including LICENSE -------------------------------

copy_home="$(fake_home copy-all)"
if output="$(HOME="$copy_home" "$repo_dir/scripts/install.sh" --copy all 2>&1)"; then
  ok "copy install for all clients succeeds"
else
  bad "copy install for all clients failed: $output"
fi

claude_copy_dest="$copy_home/.claude/skills/gauntlet-loop"
codex_copy_dest="$copy_home/.codex/skills/gauntlet-loop"

if [ -d "$claude_copy_dest" ] && [ ! -L "$claude_copy_dest" ]; then
  ok "claude copy destination is a real directory"
else
  bad "claude copy destination is missing or is a symlink"
fi

if [ -f "$claude_copy_dest/SKILL.md" ] && [ -f "$claude_copy_dest/LICENSE" ] &&
   [ -d "$claude_copy_dest/agents" ] && [ -f "$claude_copy_dest/scripts/gauntlet.py" ] &&
   [ -f "$claude_copy_dest/examples/gauntlet.json" ]; then
  ok "claude copy destination includes the skill, license, runner, and example"
else
  bad "claude copy destination is missing expected files"
fi

if [ -f "$codex_copy_dest/LICENSE" ]; then
  ok "codex copy destination includes LICENSE"
else
  bad "codex copy destination is missing LICENSE"
fi

if diff -q "$repo_dir/LICENSE" "$claude_copy_dest/LICENSE" >/dev/null 2>&1; then
  ok "copied LICENSE matches this checkout's LICENSE"
else
  bad "copied LICENSE does not match this checkout's LICENSE"
fi

# --- conflict collection and no partial "all" install --------------------

conflict_home="$(fake_home conflict-all)"
mkdir -p "$conflict_home/.codex/skills" "$conflict_home/.claude/skills" "$conflict_home/elsewhere"
# Two distinct conflicts prove that preflight reports all destinations rather
# than exiting after the first: a plain file for Codex and a foreign symlink
# at the shared Claude/tfcode destination.
: > "$conflict_home/.codex/skills/gauntlet-loop"
ln -s "$conflict_home/elsewhere" "$conflict_home/.claude/skills/gauntlet-loop"

if output="$(HOME="$conflict_home" "$repo_dir/scripts/install.sh" all 2>&1)"; then
  bad "install did not fail despite a pre-existing codex conflict"
else
  ok "install fails when any selected client has a conflict"
fi

if printf '%s' "$output" | grep -q 'no changes were made'; then
  ok "conflict output reports that no changes were made"
else
  bad "conflict output did not report an aborted install"
fi

if printf '%s' "$output" | grep -q "$conflict_home/.codex/skills/gauntlet-loop" &&
   printf '%s' "$output" | grep -q "$conflict_home/.claude/skills/gauntlet-loop"; then
  ok "preflight reports every conflicting destination in one run"
else
  bad "preflight did not report every conflicting destination"
fi

if [ -L "$conflict_home/.claude/skills/gauntlet-loop" ] &&
   [ "$(readlink "$conflict_home/.claude/skills/gauntlet-loop")" = "$conflict_home/elsewhere" ]; then
  ok "conflicting shared claude/tfcode symlink was left untouched"
else
  bad "conflicting shared claude/tfcode symlink was modified"
fi

if [ -f "$conflict_home/.codex/skills/gauntlet-loop" ] && [ ! -L "$conflict_home/.codex/skills/gauntlet-loop" ]; then
  ok "conflicting codex destination was left untouched, not replaced"
else
  bad "conflicting codex destination was modified"
fi

# --- foreign symlink safety ----------------------------------------------

foreign_home="$(fake_home foreign-symlink)"
mkdir -p "$foreign_home/.claude/skills" "$foreign_home/elsewhere"
ln -s "$foreign_home/elsewhere" "$foreign_home/.claude/skills/gauntlet-loop"

if output="$(HOME="$foreign_home" "$repo_dir/scripts/install.sh" claude 2>&1)"; then
  bad "install did not refuse a symlink pointing outside this checkout"
else
  ok "install refuses to replace a symlink pointing outside this checkout"
fi

foreign_link="$foreign_home/.claude/skills/gauntlet-loop"
if [ -L "$foreign_link" ] && [ "$(readlink "$foreign_link")" = "$foreign_home/elsewhere" ]; then
  ok "foreign symlink target is unchanged after the refused install"
else
  bad "foreign symlink target was modified"
fi

# --- summary ---------------------------------------------------------------

if python3 "$repo_dir/scripts/test_gauntlet.py"; then
  ok "cross-runtime routing tests pass"
else
  bad "cross-runtime routing tests failed"
fi

echo
echo "$pass passed, $fail failed"
if [ "$fail" -ne 0 ]; then
  exit 1
fi
