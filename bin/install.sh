#!/usr/bin/env bash
# Install myorg-starter: register its plugin in Claude Code and Codex.
#
#   curl -fsSL https://raw.githubusercontent.com/praxiom-systems/myorg-starter/main/bin/install.sh | bash
#
# Options (after `bash -s --` when piped):
#   --workflows   also copy the Claude GitHub workflows into this repo's
#                 .github/workflows/ (@claude mentions and PR review)
#
# The plugin installs once for you and works in every project; each host
# keeps it updated. Run it again any time: it skips what's already there.
set -euo pipefail

REPO="praxiom-systems/myorg-starter"
RAW="https://raw.githubusercontent.com/$REPO/main"
PLUGIN="myorg-starter@myorg-starter"
WORKFLOWS=0

for arg in "$@"; do
  case "$arg" in
    --workflows) WORKFLOWS=1 ;;
    -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

hosts=0

if command -v claude >/dev/null 2>&1; then
  hosts=$((hosts + 1))
  if ! claude plugin marketplace list 2>/dev/null | grep -q 'myorg-starter'; then
    claude plugin marketplace add "$REPO"
  else
    claude plugin marketplace update myorg-starter >/dev/null 2>&1 || true
  fi
  claude plugin install "$PLUGIN"
  echo "Claude Code: installed myorg-starter (10 agents, 2 skills, the session guards)."
fi

if command -v codex >/dev/null 2>&1; then
  hosts=$((hosts + 1))
  if ! codex plugin marketplace list 2>/dev/null | grep -q '^myorg-starter '; then
    codex plugin marketplace add "$REPO"
  else
    codex plugin marketplace upgrade myorg-starter >/dev/null 2>&1 || true
  fi
  codex plugin add "$PLUGIN"
  echo "Codex: installed myorg-starter (the session guards and 2 skills)."
  echo "  Codex runs plugin hooks only once you trust them: open /hooks in Codex and trust myorg-starter's."
fi

if [ "$hosts" -eq 0 ]; then
  echo "Neither Claude Code (claude) nor Codex (codex) is on your PATH. Install one, then run this again." >&2
  exit 1
fi

if [ "$WORKFLOWS" -eq 1 ]; then
  root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "--workflows: run this from inside the git repo that should get the workflows." >&2
    exit 1
  }
  mkdir -p "$root/.github/workflows"
  for wf in claude.yml claude-code-review.yml; do
    dest="$root/.github/workflows/$wf"
    if [ -e "$dest" ]; then
      echo "Kept your existing .github/workflows/$wf."
      continue
    fi
    curl -fsSL "$RAW/templates/workflows/$wf" -o "$dest"
    echo "Added .github/workflows/$wf."
  done
  echo "  The workflows need a CLAUDE_CODE_OAUTH_TOKEN repo secret: run 'claude setup-token' to make one."
fi

echo
echo "Restart Claude Code or Codex to load the plugin, then check it:"
echo "  curl -fsSL $RAW/bin/doctor.sh | bash"
