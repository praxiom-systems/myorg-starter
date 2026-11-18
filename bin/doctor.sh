#!/usr/bin/env bash
# myorg-starter doctor: check that the plugin is installed and its guards fire.
#
#   curl -fsSL https://raw.githubusercontent.com/praxiom-systems/myorg-starter/main/bin/doctor.sh | bash
#
# For each host on your PATH (Claude Code, Codex) it checks that the
# myorg-starter plugin is installed and enabled, then sends a never-OK
# command through the installed Bash guard and expects it to be stopped.
# Exits 1 when a host has no plugin or its guard doesn't stop the command.

set -u

PLUGIN="myorg-starter@myorg-starter"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
LOG_DIR="${MYORG_HOOK_LOG_DIR:-/tmp/claude-hook-logs}"
failed=0
hosts=0
plugin_root=""

ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; failed=1; }
note() { echo "  · $*"; }

# claude_plugin_root prints the installed plugin's directory, or nothing.
claude_plugin_root() {
  python3 - "$CLAUDE_DIR" "$PLUGIN" <<'PY'
import json, os, sys
base, plugin = sys.argv[1], sys.argv[2]
try:
    with open(os.path.join(base, "plugins", "installed_plugins.json")) as f:
        data = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
entries = (data.get("plugins") or {}).get(plugin) or []
for entry in entries if isinstance(entries, list) else [entries]:
    path = entry.get("installPath") if isinstance(entry, dict) else None
    if path and os.path.isdir(path):
        print(path)
        break
PY
}

claude_plugin_enabled() {
  python3 - "$CLAUDE_DIR/settings.json" "$PLUGIN" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        enabled = (json.load(f).get("enabledPlugins") or {}).get(sys.argv[2])
except (OSError, ValueError):
    enabled = None
sys.exit(0 if enabled else 1)
PY
}

codex_plugin_enabled() {
  [ -r "$CODEX_DIR/config.toml" ] || return 1
  awk -v want="[plugins.\"$PLUGIN\"]" '
    /^\[/ { on = ($0 == want); next }
    on && /^[[:space:]]*enabled[[:space:]]*=[[:space:]]*true/ { found = 1 }
    END { exit !found }
  ' "$CODEX_DIR/config.toml"
}

codex_plugin_root() {
  local hooks
  hooks=$(find "$CODEX_DIR/plugins/cache/myorg-starter/myorg-starter" -maxdepth 3 -path '*/hooks/hooks.json' 2>/dev/null | sort | tail -1)
  [ -n "$hooks" ] && dirname "$(dirname "$hooks")"
}

# fire_test <plugin root> [env...]: a never-OK command must be stopped.
fire_test() {
  local root="$1"; shift
  local out
  out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},"cwd":"/tmp"}' \
    | env "$@" MYORG_HOOK_STRICTNESS=relaxed MYORG_HOOK_LOG_DIR="" bash "$root/hooks/validate-bash.sh" 2>/dev/null)
  case "$out" in
    *'"permissionDecision": "deny"'*|*'"permissionDecision":"deny"'*) return 0 ;;
  esac
  return 1
}

# getmyorg's own plugin: in Claude Code the starter stands down where it runs;
# in Codex both run, since Codex's trust of its hooks can't be checked here.
# For this project: installed, and enabled by the closest settings file that
# names it (local, then project, then user), as the starter's hooks decide.
myorg_on_claude() {
  python3 - "$CLAUDE_DIR" "$PWD" <<'PY' 2>/dev/null
import json, os, sys
home, project = sys.argv[1], sys.argv[2]
def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
installed = load(os.path.join(home, "plugins", "installed_plugins.json")).get("plugins") or {}
layers = [load(os.path.join(project, ".claude", name)).get("enabledPlugins") or {}
          for name in ("settings.local.json", "settings.json")]
layers.append(load(os.path.join(home, "settings.json")).get("enabledPlugins") or {})
for plugin in {k for layer in layers for k in layer if k.startswith("myorg@")}:
    if plugin in installed:
        for layer in layers:
            if plugin in layer:
                if layer[plugin] is True:
                    sys.exit(0)
                break
sys.exit(1)
PY
}
myorg_on_codex() {
  [ -r "$CODEX_DIR/config.toml" ] && awk '
    /^\[/ { on = ($0 ~ /^\[plugins\."myorg@[^"]+"\]/); next }
    on && /^[[:space:]]*enabled[[:space:]]*=[[:space:]]*true/ { found = 1 }
    END { exit !found }
  ' "$CODEX_DIR/config.toml"
}

echo "myorg-starter doctor"
echo

if command -v claude >/dev/null 2>&1; then
  hosts=$((hosts + 1))
  echo "Claude Code"
  root=$(claude_plugin_root)
  if [ -z "$root" ]; then
    bad "the plugin isn't installed. Run install.sh, or: claude plugin install $PLUGIN"
  elif ! claude_plugin_enabled; then
    bad "the plugin is installed but disabled. Run: claude plugin enable $PLUGIN"
  elif myorg_on_claude; then
    ok "installed at $root"
    note "getmyorg's own plugin is on, so the starter's guards stand down and getmyorg's run."
    note "You can remove the starter: claude plugin uninstall $PLUGIN"
  elif fire_test "$root" PLUGIN_ROOT=; then
    ok "installed at $root"
    ok "the Bash guard stopped a never-OK command"
    plugin_root="$root"
  else
    bad "the plugin is installed, but its Bash guard didn't stop a never-OK command. Check that python3 runs."
  fi
  echo
fi

if command -v codex >/dev/null 2>&1; then
  hosts=$((hosts + 1))
  echo "Codex"
  root=$(codex_plugin_root)
  if [ -z "$root" ]; then
    bad "the plugin isn't installed. Run install.sh, or: codex plugin add $PLUGIN"
  elif ! codex_plugin_enabled; then
    bad "the plugin is installed but disabled. Enable it in Codex's /plugins, or run: codex plugin add $PLUGIN"
  elif fire_test "$root" PLUGIN_ROOT="$root"; then
    ok "installed at $root"
    ok "the Bash guard stopped a never-OK command"
    if grep -q "myorg-starter.*hooks.json:pre_tool_use" "$CODEX_DIR/config.toml" 2>/dev/null; then
      ok "you've trusted its hooks"
    else
      note "Codex runs plugin hooks only once you trust them: open /hooks in Codex and trust myorg-starter's."
    fi
    if myorg_on_codex; then
      note "getmyorg's own plugin is on too. Once you've trusted its hooks in /hooks,"
      note "you can remove the starter: codex plugin remove $PLUGIN"
    fi
    [ -z "$plugin_root" ] && plugin_root="$root"
  else
    bad "the plugin is installed, but its Bash guard didn't stop a never-OK command. Check that python3 runs."
  fi
  echo
fi

if [ "$hosts" -eq 0 ]; then
  echo "Neither Claude Code (claude) nor Codex (codex) is on your PATH."
  exit 1
fi

if [ -n "$plugin_root" ]; then
  # The config hook prints the settings for a bare /myorg-starter:config.
  printf '{"prompt":"/myorg-starter:config","cwd":"%s"}' "$PWD" \
    | python3 "$plugin_root/hooks/config-prompt.py" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    text = json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"]
except Exception:
    sys.exit(0)
print(text.split("\n\n", 1)[1] if "\n\n" in text else text)'
  echo
fi

if [ -d "$LOG_DIR" ]; then
  stops=$(find "$LOG_DIR" -name '*.log' -mtime -1 -exec cat {} + 2>/dev/null | grep -c 'EVENT:  BashDeny' || true)
  echo "Guard stops in the last 24h: ${stops:-0} (logs in $LOG_DIR)"
fi

exit $failed
