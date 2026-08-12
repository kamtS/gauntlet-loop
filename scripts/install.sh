#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [--copy] [all|claude|codex|tfcode]

Install Gauntlet Loop for one supported client or all three.
The default mode creates a symlink to this checkout; --copy creates a copy.
Existing installations and symlinks to another checkout are never replaced.
EOF
}

mode="link"
client="all"

for arg in "$@"; do
  case "$arg" in
    --copy) mode="copy" ;;
    all|claude|codex|tfcode) client="$arg" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"

if [ ! -f "$repo_dir/SKILL.md" ]; then
  echo "error: SKILL.md not found at $repo_dir" >&2
  exit 1
fi

base_dir_for() {
  case "$1" in
    claude) printf '%s\n' "${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}" ;;
    # This installation discovered gauntlet-loop via ~/.codex/skills.
    codex) printf '%s\n' "${CODEX_SKILLS_DIR:-$HOME/.codex/skills}" ;;
    # The TFCode setup used to validate this release reads the Claude skills
    # directory as its compatibility fallback. TFCODE_SKILLS_DIR remains
    # available for installations with a documented native directory.
    tfcode) printf '%s\n' "${TFCODE_SKILLS_DIR:-$HOME/.claude/skills}" ;;
  esac
}

destination_for() {
  printf '%s/gauntlet-loop\n' "$(base_dir_for "$1")"
}

clients_for() {
  if [ "$1" = "all" ]; then
    printf '%s\n' claude codex tfcode
  else
    printf '%s\n' "$1"
  fi
}

# One entry per unique destination path, even when multiple clients share it
# (e.g. tfcode's default falls back to the same directory as claude's).
unique_destinations() {
  clients_for "$client" | while read -r c; do destination_for "$c"; done | sort -u
}

clients_at_destination() {
  target="$1"
  clients_for "$client" | while read -r c; do
    if [ "$(destination_for "$c")" = "$target" ]; then
      printf '%s\n' "$c"
    fi
  done
}

preflight_one() {
  destination="$1"

  if [ -L "$destination" ]; then
    current_target="$(readlink "$destination")"
    if [ "$mode" = "link" ] && [ "$current_target" = "$repo_dir" ]; then
      return 0
    fi
    echo "error: refusing to replace existing symlink $destination -> $current_target" >&2
    return 1
  fi

  if [ -e "$destination" ]; then
    echo "error: refusing to overwrite existing $destination" >&2
    return 1
  fi
}

install_one() {
  destination="$1"
  base_dir="$(dirname "$destination")"
  mkdir -p "$base_dir"

  if [ -L "$destination" ]; then
    # Preflight permits only a link that already points to this checkout.
    :
  elif [ "$mode" = "link" ]; then
    ln -s "$repo_dir" "$destination"
  else
    mkdir "$destination"
    cp "$repo_dir/SKILL.md" "$destination/SKILL.md"
    cp "$repo_dir/LICENSE" "$destination/LICENSE"
    cp -R "$repo_dir/agents" "$destination/agents"
    cp -R "$repo_dir/scripts" "$destination/scripts"
    cp -R "$repo_dir/examples" "$destination/examples"
  fi

  clients="$(clients_at_destination "$destination" | paste -sd, -)"
  echo "installed for $clients: $destination ($mode)"
}

# Preflight every destination first and collect every conflict before making
# any change, so a single conflicting client never leaves the others
# partially installed.
conflicts=0
while IFS= read -r destination; do
  if ! preflight_one "$destination"; then
    conflicts=1
  fi
done <<< "$(unique_destinations)"

if [ "$conflicts" -ne 0 ]; then
  echo "error: install aborted, no changes were made" >&2
  exit 1
fi

while IFS= read -r destination; do
  install_one "$destination"
done <<< "$(unique_destinations)"

if [ "$client" = "all" ] || [ "$client" = "claude" ]; then
  echo
  echo "Before running runtime mode ('gauntlet.py run') from inside Claude Code,"
  echo "allowlist the runner so a mid-loop permission prompt doesn't get read as"
  echo "a sandbox-evasion attempt by the auto-mode classifier:"
  echo "  /permissions -> add: Bash(python3 $repo_dir/scripts/gauntlet.py:*)"
fi
