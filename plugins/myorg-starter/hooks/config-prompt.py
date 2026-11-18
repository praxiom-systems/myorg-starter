#!/usr/bin/env python3
"""UserPromptSubmit hook: apply ``/myorg:config guards|gate|profile <value>`` from the user's own prompt.

Only the user changes their guard level, gate mode and autonomy profile
(#1089, #1168, #1178). The session guards stop the agent from running
``getmyorg config set`` or editing the settings files, so the change happens
here, on the text the user typed, before the agent sees it. The hook hands
the result to the agent as context; the config skill then reports the
settings.

Matches ``/<plugin>:config guards <level> [--repo]``, ``... gate on|off
[--repo]`` and ``... profile <profile> [--repo]`` (Claude Code), and the same
with ``$<plugin>:config`` (Codex). The plugin is ``myorg`` or
``myorg-starter``. The other forms (show, preset, statusline) and every other
prompt pass through untouched; the skill runs those. In the starter, which
has no binary to show the settings, a bare ``/myorg-starter:config`` gets
them here as context. Never blocks a prompt.

In the myorg plugin the hook runs ``getmyorg config set``. The myorg-starter
plugin ships no getmyorg binary, so there it writes the same settings file
itself: the user's ``config.yaml``, or the repo's gitignored
``.myorg/config.local.yaml`` with ``--repo``. Both products read those files,
so the settings carry over when someone moves from the starter to getmyorg.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from bash_policy_decide import (
    DEFAULT_GATE,
    DEFAULT_LEVEL,
    GATES,
    LEVELS,
    PROFILES,
    _myorg_dir,
    read_autonomy_key,
    repo_override,
    user_config_path,
)
from myorg_message import PLUGIN

_COMMAND = re.compile(
    r"^\s*[/$]myorg(?:-starter)?:config\s+(?P<key>guards|gate|profile)(?:\s+(?P<value>\S+))?(?P<repo>\s+--repo)?\s*$",
    re.IGNORECASE,
)

_VALUES = {"guards": ("guard level", LEVELS), "gate": ("gate mode", GATES), "profile": ("profile", PROFILES)}

# The settings file's key for each command word.
_FILE_KEY = {"guards": "hook_strictness", "gate": "gate", "profile": "profile"}

_SHOW = re.compile(r"^\s*[/$]myorg-starter:config\s*$", re.IGNORECASE)

_MEANING = {
    "strict": "only known-safe commands run without a second look; risky ones are stopped",
    "guarded": "everyday commands run; risky ones are stopped or bounced back to the agent",
    "relaxed": "everything runs except never-OK commands and force-pushes to protected branches",
    "off": "the session guards do nothing",
    "on": "commands that aren't routine ask you in Claude Code's permission prompt",
}

__all__ = ["describe_settings", "setting_request", "set_autonomy_key", "main"]


def setting_request(prompt: str) -> tuple[str, str, bool] | None:
    """``(key, value, repo_only)`` of a ``/myorg:config guards|gate|profile
    <value>`` prompt, lowercased, or None."""
    m = _COMMAND.match(prompt or "")
    if not m or not m.group("value"):
        return None
    return m.group("key").lower(), m.group("value").lower(), bool(m.group("repo"))


def _getmyorg() -> str:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT")
    if root:
        shim = os.path.join(root, "bin", "getmyorg")
        if os.access(shim, os.X_OK):
            return shim
    return "getmyorg"


def _context(text: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }))


_TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def set_autonomy_key(path: Path, key: str, value: str) -> None:
    """Set ``autonomy.<key>`` in the YAML file at ``path``, keeping the rest.

    A line editor, like the hooks' reader, so it needs no PyYAML. It updates
    the key inside a block-form ``autonomy:`` section, adds it to that
    section, or appends the section. Writes atomically, private to the user.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    start = next((i for i, line in enumerate(lines) if re.match(r"^autonomy\s*:\s*(#.*)?$", line)), None)
    if start is None:
        if any(re.match(r"^autonomy\s*:", line) for line in lines):
            raise ValueError(f"{path} writes autonomy on one line; edit it by hand")
        lines += ["autonomy:", f"  {key}: {value}"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if _TOP_KEY.match(lines[i])), len(lines))
        # Keep the section's own indentation; refuse one this editor can't
        # keep (tabs, mixed depths) rather than write broken YAML.
        indents = {
            re.match(r"^\s*", line).group(0)
            for line in lines[start + 1:end]
            if line.strip() and not line.lstrip().startswith("#")
        }
        if len(indents) > 1 or any("\t" in i for i in indents):
            raise ValueError(f"{path}: the autonomy section's indentation is mixed; edit it by hand")
        entry = f"{indents.pop() if indents else '  '}{key}: {value}"
        own = re.compile(r"^\s+" + re.escape(key) + r"\s*:")
        hit = next((i for i in range(start + 1, end) if own.match(lines[i])), None)
        if hit is None:
            lines.insert(start + 1, entry)
        else:
            lines[hit] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _resolved(cwd: str, key: str, env: str, allowed: tuple[str, ...], default: str) -> tuple[str, str]:
    """(value, where it came from), in the order the guards read them."""
    if os.environ.get(env, "").strip().lower() in allowed:
        return os.environ[env].strip().lower(), f"the {env} environment variable"
    for path, label in ((repo_override(cwd)[0], "this repo only"), (user_config_path(), "your setting")):
        if path is None:
            continue
        try:
            value = read_autonomy_key(path.read_text(encoding="utf-8", errors="replace"), key)
        except OSError:
            continue
        if value in allowed:
            return value, f"{label} ({path})"
    return default, "the default"


def _claude_prompts_off(cwd: str) -> str:
    """Why Claude Code's own Bash prompts look off, or "" (as getmyorg's
    claudePromptsOff): a settings file allows all of Bash or bypasses."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    paths = [base / "settings.json"]
    root = next((p for p in (Path(cwd), *Path(cwd).parents) if (p / ".git").exists()), None)
    if root is not None:
        paths += [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    for path in paths:
        try:
            perms = json.loads(path.read_text(encoding="utf-8")).get("permissions") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if perms.get("defaultMode") == "bypassPermissions":
            return f"bypassPermissions in {path}"
        for rule in perms.get("allow") or []:
            if rule in ("Bash", "Bash(*)", "Bash(:*)"):
                return f"{rule} allowed in {path}"
    return ""


def _claude_remote_control_on(cwd: str) -> bool:
    """Whether every Claude Code session here connects to Remote Control (as
    getmyorg's claudeRemoteControlOn): the user's remoteControlAtStartup is
    true and no repo settings file turns it off. A repo's true is ignored."""
    def read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("remoteControlAtStartup")
        except (OSError, ValueError, AttributeError):
            return None

    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if read(base / "settings.json") is not True:
        return False
    root = next((p for p in (Path(cwd), *Path(cwd).parents) if (p / ".git").exists()), None)
    if root is not None:
        for name in ("settings.json", "settings.local.json"):
            if read(root / ".claude" / name) is False:
                return False
    return True


def _away_note(cwd: str) -> list[str]:
    if _claude_remote_control_on(cwd):
        return [
            "  Away from your desk: Remote Control is set to connect every Claude Code session, so",
            "  prompts and questions can reach claude.ai/code and the Claude app. Check it shows",
            "  connected before you step away. Other ways, Codex included: getmyorg.com/docs/away",
        ]
    return [
        "  Away from your desk, prompts and questions wait in the terminal. To answer them from",
        "  your phone, turn on Enable Remote Control for all sessions and Push when actions",
        "  required in Claude Code's /config. Other ways, Codex included: getmyorg.com/docs/away",
    ]


def describe_settings(cwd: str) -> str:
    """The starter's settings, for a bare /myorg-starter:config."""
    level, level_from = _resolved(cwd, "hook_strictness", "MYORG_HOOK_STRICTNESS", LEVELS, DEFAULT_LEVEL)
    gate, gate_from = _resolved(cwd, "gate", "MYORG_HOOK_GATE", GATES, DEFAULT_GATE)
    lines = [
        "myorg-starter settings",
        "",
        f"  guards  {level:8} {level_from}",
        f"          {_MEANING[level]}",
        f"  gate    {gate:8} {gate_from}",
    ]
    ignored = repo_override(cwd)[1]
    if gate == "on":
        lines.append(f"          {_MEANING['on']} (Claude Code only)")
    elif level != "off" and (why := _claude_prompts_off(cwd)):
        lines += [
            "",
            f"  Claude Code's own prompts look off ({why}), so commands no guard rule",
            "  knows run without asking you. /myorg-starter:config gate on has the guard ask you instead.",
        ]
    if ignored is not None:
        lines += [
            "",
            f"  {ignored} is committed to this repo, so it's ignored: settings are yours,",
            "  not the repo's. Remove it from git (git rm --cached) if it was meant to be yours.",
        ]
    lines += ["", *_away_note(cwd)]
    lines += [
        "",
        "Change them with /myorg-starter:config guards <strict|guarded|relaxed|off> or",
        "/myorg-starter:config gate on|off (add --repo for this repo only).",
    ]
    return "\n".join(lines)


def _set_directly(key: str, value: str, repo_only: bool, cwd: str) -> tuple[bool, str]:
    """The starter's setter: write the settings file itself."""
    if key == "profile":
        return False, "The autonomy profile is a getmyorg setting; the starter has no profiles."
    if repo_only:
        myorg = _myorg_dir(cwd)
        if myorg is None:
            root = next((p for p in (Path(cwd), *Path(cwd).parents) if (p / ".git").exists()), None)
            if root is None:
                return False, "--repo needs a git repository."
            myorg = root / ".myorg"
        path = myorg / "config.local.yaml"
        if repo_override(cwd)[1] is not None:
            return False, f"{path} is committed to this repo, so the guards ignore it; remove it from git first."
    else:
        path = user_config_path()
    try:
        set_autonomy_key(path, _FILE_KEY[key], value)
    except (OSError, ValueError) as e:
        return False, str(e)
    return True, f"Set {_VALUES[key][0]} to {value} in {path}."


def _set_with_getmyorg(key: str, value: str, repo_only: bool, cwd: str) -> tuple[bool, str]:
    args = [_getmyorg(), "config", "set", key, value] + (["--repo"] if repo_only else [])
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=20)
        return out.returncode == 0, (out.stdout + out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)
    prompt = data.get("prompt", "")
    cwd = data.get("cwd") or os.getcwd()
    if PLUGIN == "myorg-starter" and _SHOW.match(prompt):
        _context("The user's settings, from the config command. Show them verbatim:\n\n" + describe_settings(cwd))
        return
    request = setting_request(prompt)
    if request is None:
        sys.exit(0)
    key, value, repo_only = request
    noun, allowed = _VALUES[key]
    if value not in allowed:
        _context(
            f"The user asked for {noun} {value!r}, which doesn't exist. "
            f"The choices are {', '.join(allowed)}. Nothing changed; tell the user."
        )
        return
    setter = _set_directly if PLUGIN == "myorg-starter" else _set_with_getmyorg
    ok, result = setter(key, value, repo_only, cwd)
    if ok:
        note = ""
        if key == "gate" and (prompt.lstrip().startswith("$") or os.environ.get("PLUGIN_ROOT")):
            note = (
                "\nTell the user gate mode only works in Claude Code: Codex's hooks can "
                "stop a command but can't ask the user, so in Codex the guard works as before."
            )
        _context(
            f"The user's config command already set their {noun} to {value}. "
            f"Don't change it yourself. Output:\n{result}{note}"
        )
    else:
        _context(
            f"The user asked to set their {noun} to {value}, but it didn't change. "
            f"Tell the user what went wrong. Output:\n{result}"
        )


if __name__ == "__main__":
    main()
