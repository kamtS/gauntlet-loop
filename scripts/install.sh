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
    codex) printf '%s\n' "${CODEX_SKILLS_DIR:-$HOME/.agents/skills}" ;;
    tfcode) printf '%s\n' "${TFCODE_SKILLS_DIR:-$HOME/.tfcode/skill}" ;;
  esac
}

preflight_one() {
  client_name="$1"
  base_dir="$(base_dir_for "$client_name")"
  destination="$base_dir/gauntlet-loop"

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
  client_name="$1"
  base_dir="$(base_dir_for "$client_name")"
  destination="$base_dir/gauntlet-loop"
  mkdir -p "$base_dir"

  if [ -L "$destination" ]; then
    # Preflight permits only a link that already points to this checkout.
    :
  elif [ "$mode" = "link" ]; then
    ln -s "$repo_dir" "$destination"
  else
    mkdir "$destination"
    cp "$repo_dir/SKILL.md" "$destination/SKILL.md"
    cp -R "$repo_dir/agents" "$destination/agents"
  fi

  echo "installed for $client_name: $destination ($mode)"
}

if [ "$client" = "all" ]; then
  preflight_one claude
  preflight_one codex
  preflight_one tfcode
  install_one claude
  install_one codex
  install_one tfcode
else
  preflight_one "$client"
  install_one "$client"
fi
