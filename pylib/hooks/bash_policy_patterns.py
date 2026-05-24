"""Pattern loading and OS-overlay merging for the bash validator.

Extracted from ``validate-bash.py`` (issue #584) — owns the *pattern* slice:
TOML config loading, OS-suffix detection, additive overlay merge, regex
compilation, and dynamic git-root pattern injection. No I/O outside of
``load_config`` reading the TOML file and stderr warnings on bad regexes.

The validator entrypoint composes these helpers; behavior is exercised
through the validate-bash end-to-end tests.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        print(
            "Error: Python 3.11+ required, or install 'tomli' package for older versions",
            file=sys.stderr,
        )
        sys.exit(1)


@dataclass
class CompiledPattern:
    """A pre-compiled regex pattern with metadata."""
    regex: re.Pattern
    section: str
    original: str


def load_config(config_path: str) -> dict:
    """Load and validate TOML configuration."""
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: Invalid TOML in {config_path}: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)


def detect_os() -> str:
    """Detect OS platform and return the suffix for OS-specific config files.

    Returns 'linux', 'darwin', or 'windows'. Falls back to sys.platform value.
    """
    platform = sys.platform
    if platform.startswith("linux"):
        return "linux"
    elif platform == "darwin":
        return "darwin"
    elif platform in ("win32", "cygwin", "msys"):
        return "windows"
    return platform


def merge_os_config(base: dict, overlay: dict) -> dict:
    """Merge OS-specific config into base config with additive-append semantics.

    For each pattern category (deny/reflect/allow), OS patterns are appended
    to matching base sections. New sections from the overlay are added as-is.
    Base patterns are never removed or replaced.

    #923: `reflect` joined the category set when [ask.*] was renamed to
    [reflect.*]. Without it, OS-specific reflect sections (e.g. Linux
    network/disk changes) silently dropped during overlay merge — so under
    hook_strictness=lax they became silent allows. `ask` is retained as a
    backward-compatible alias for any user pattern files that haven't been
    migrated by scripts/migrate-ask-to-reflect.py yet.
    """
    for category in ("deny", "reflect", "ask", "allow"):
        overlay_sections = overlay.get(category, {})
        if not overlay_sections:
            continue
        base.setdefault(category, {})
        for section_name, section_data in overlay_sections.items():
            if not isinstance(section_data, dict) or "patterns" not in section_data:
                continue
            if section_name in base[category] and isinstance(base[category][section_name], dict):
                # Append OS patterns after base patterns
                base[category][section_name]["patterns"] = (
                    base[category][section_name].get("patterns", [])
                    + section_data["patterns"]
                )
            else:
                # New section from OS overlay
                base[category][section_name] = section_data
    return base


def compile_patterns(config: dict, category: str) -> list[CompiledPattern]:
    """Extract and compile patterns for a category (deny/ask/allow).

    Pre-compiling regex patterns improves performance significantly
    when validating many commands.
    """
    compiled = []
    for section_name, section in config.get(category, {}).items():
        if isinstance(section, dict) and "patterns" in section:
            for pattern in section["patterns"]:
                try:
                    regex = re.compile(pattern)
                    compiled.append(CompiledPattern(
                        regex=regex,
                        section=f"{category}.{section_name}",
                        original=pattern
                    ))
                except re.error as e:
                    print(f"Warning: Invalid regex '{pattern}' in {category}.{section_name}: {e}",
                          file=sys.stderr)
    return compiled


def _add_to_negative_lookahead(pattern: str, exclusion: str) -> str:
    """Prepend an exclusion alternative to the first negative lookahead in a pattern.

    Transforms: /(?!foo|bar) -> /(?!<exclusion>|foo|bar)
    If no lookahead exists, returns the pattern unchanged.
    """
    if "(?!" not in pattern:
        return pattern
    return pattern.replace("(?!", f"(?!{exclusion}|", 1)


def inject_git_root_patterns(config: dict, git_root: str) -> None:
    """Dynamically inject git root path into validation patterns.

    When Claude runs from a git repository, rm within that repo is
    auto-approved (lockstep: excluded from ask, added to allow).
    The .git metadata directory itself is protected from deletion.

    Must be called BEFORE compile_patterns().
    """
    # Lookahead exclusion: path after the leading '/' must not start with git_root
    # E.g. /home/rrl/github/myproject -> home/rrl/github/myproject/
    escaped_for_lookahead = re.escape(git_root.lstrip("/")) + "/"

    # 1. Narrow reflect.file_deletion to exclude paths within the git root.
    #    This must be paired with the allow entry below (evaluated before allow).
    reflect_file_del = config.get("reflect", {}).get("file_deletion", {})
    if isinstance(reflect_file_del, dict) and "patterns" in reflect_file_del:
        reflect_file_del["patterns"] = [
            _add_to_negative_lookahead(p, escaped_for_lookahead)
            for p in reflect_file_del["patterns"]
        ]

    # 2. Allow rm on any path within the git root (lockstep with exclusion above).
    escaped_abs = re.escape(git_root)
    config.setdefault("allow", {})["git_project_files"] = {
        "description": f"File removal within git repository at {git_root}",
        "patterns": [f"^rm\\s+(-[a-zA-Z]+\\s+)*{escaped_abs}/"],
    }

    # 3. Protect the .git directory itself from deletion (added to reflect).
    escaped_git_dir = re.escape(os.path.join(git_root, ".git"))
    config.setdefault("reflect", {})["git_metadata_protection"] = {
        "description": "Protect .git metadata directory from deletion",
        "patterns": [f"^rm\\b.*{escaped_git_dir}(/|$)"],
    }
