#!/usr/bin/env bash
# myorg-starter doctor: report on the installed safety layer.
#
# Run from the root of a myorg-starter clone:
#   bash bin/doctor.sh
#
# Exit 0 if all required hooks are present; exit 1 otherwise.

set -eu

HOOKS_DIR="blueprint/hooks"
LOG_DIR="${MYORG_HOOK_LOG_DIR:-/tmp/claude-hook-logs}"

REQUIRED_HOOKS=(
  "validate-bash.sh"
  "validate-bash.py"
  "post-bash.sh"
  "hook-lib.sh"
  "validate-read.sh"
  "validate-read.py"
  "validate-edit.sh"
  "validate-edit.py"
)

count_sections() {
  # $1 = toml file, $2 = section prefix (deny / ask / allow / advise)
  local file="$1" section="$2"
  if [ ! -r "$file" ]; then echo 0; return; fi
  grep -cE "^\[${section}\." "$file" 2>/dev/null || echo 0
}

count_recent_log_entries() {
  # $1 = EVENT name as written by hooklib_log_event (e.g. BashDeny, ReadDeny,
  # EditAdvise, EditDeny). Counts EVENT lines in any *.log under LOG_DIR
  # mtime'd within the last 24h. Returns "n/a" if LOG_DIR is absent.
  #
  # Wrappers log via hooklib_log_event to <date>-<project>.log with blocks
  # like `EVENT:  BashDeny`. The doctor counts those event tokens directly,
  # which is portable across BSD/GNU userlands (no xargs -r, no grep -c
  # single-file colon-parsing) and stays correct when a user has multiple
  # projects logging into the same shared LOG_DIR.
  local event="$1"
  if [ ! -d "$LOG_DIR" ]; then printf 'n/a'; return; fi
  local n
  n=$(find "$LOG_DIR" -name '*.log' -mtime -1 -exec cat {} + 2>/dev/null \
        | grep -F -c "EVENT:  ${event}" 2>/dev/null || printf '0')
  printf '%s' "${n:-0}"
}

print_header() {
  echo "myorg-starter doctor"
  echo "===================="
  echo
}

print_install_section() {
  local missing=0
  echo "Hook installation"
  for hook in "${REQUIRED_HOOKS[@]}"; do
    local path="$HOOKS_DIR/$hook"
    if [ -r "$path" ]; then
      echo "  ✓ $path"
    else
      echo "  ✗ $path  (missing)"
      missing=1
    fi
  done
  echo
  return $missing
}

print_bash_policy() {
  echo "Bash policy"
  local toml="$HOOKS_DIR/bash-patterns.toml"
  echo "  patterns: $(count_sections "$toml" deny) deny / $(count_sections "$toml" ask) ask / $(count_sections "$toml" allow) allow   ($toml)"
  echo "  default mode: ${MYORG_BASH_DEFAULT:-allow}                    (override: MYORG_BASH_DEFAULT=ask)"
  echo "  recent denies (last 24h): $(count_recent_log_entries BashDeny)"
  echo
}

print_read_guard() {
  echo "Read content guard"
  local toml="$HOOKS_DIR/injection-patterns.toml"
  echo "  patterns: $(count_sections "$toml" deny)                              ($toml)"
  echo "  recent flags: $(count_recent_log_entries ReadDeny)"
  echo
}

print_edit_guard() {
  echo "Edit band-aid detector"
  local toml="$HOOKS_DIR/band-aid-patterns.toml"
  echo "  patterns: $(count_sections "$toml" deny) deny / $(count_sections "$toml" advise) advise   ($toml)"
  echo "  mode: ${MYORG_BANDAID_MODE:-deny+advise}                       (override: MYORG_BANDAID_MODE=all-advise|off)"
  echo "  recent advisories: $(count_recent_log_entries EditAdvise)"
  echo
}

print_footer() {
  echo "Logs: ${LOG_DIR}/             (override: MYORG_HOOK_LOG_DIR)"
}

main() {
  print_header
  if ! print_install_section; then
    print_bash_policy 2>/dev/null || true
    print_read_guard 2>/dev/null || true
    print_edit_guard 2>/dev/null || true
    print_footer
    echo
    echo "✗ One or more required hooks are missing. Re-run the install steps in README.md."
    exit 1
  fi
  print_bash_policy
  print_read_guard
  print_edit_guard
  print_footer
}

main "$@"
