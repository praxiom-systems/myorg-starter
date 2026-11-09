"""Protected-branch force-push detection for the Bash guard (#1089).

A force-push (or delete) that targets ``main``, ``master`` or the remote's
default branch rewrites history other people depend on. The Bash guard stops
it at every level except ``off``. Pure parsing plus a few cached ``git``
lookups (HEAD, @{push}, the remote's default branch); no getmyorg binary
needed.

Best effort, like the rest of the guard: git aliases, shell functions and
push refspecs set in config (``remote.<name>.push``) aren't seen.
"""

from __future__ import annotations

import functools
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bash_command_parsing import (
    clean_segment,
    extract_command_substitutions,
    split_commands,
    strip_line_continuations,
    unwrap_command,
)

ALWAYS_PROTECTED = frozenset({"main", "master"})

# git options that take a separate value before the subcommand.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# push options that take a separate value.
_PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})

__all__ = ["ALWAYS_PROTECTED", "protected_force_push"]


@functools.lru_cache(maxsize=None)
def _git(cwd: str, *args: str) -> str:
    # Short timeout: the hook's whole budget is 10s and a push check makes
    # up to three lookups.
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd or None, capture_output=True, text=True, timeout=1
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _protected(cwd: str, remote: str) -> set[str]:
    names = set(ALWAYS_PROTECTED)
    head = _git(cwd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if head.startswith(remote + "/"):
        names.add(head[len(remote) + 1:])
    return names


def _branch(ref: str, cwd: str) -> str:
    if ref in ("HEAD", "@"):
        ref = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _push_args(tokens: list[str], cwd: str) -> tuple[list[str], str] | None:
    """Return the arguments after ``git [opts] push`` and the directory git
    runs in (``-C`` moves it), or None if not a push."""
    if not tokens or Path(tokens[0]).name != "git":
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] == "-C" and i + 1 < len(tokens):
            cwd = os.path.join(cwd, os.path.expanduser(tokens[i + 1]))
        i += 2 if tokens[i] in _GIT_VALUE_OPTS else 1
    if i >= len(tokens) or tokens[i] != "push":
        return None
    return tokens[i + 1:], cwd


def _check_push(args: list[str], cwd: str) -> str | None:
    force = delete = mirror = dry_run = every = False
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positionals.extend(args[i + 1:])
            break
        if a in _PUSH_VALUE_OPTS:
            i += 2
            continue
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in ("--force", "--force-with-lease", "--force-if-includes"):
                force = True
            elif name == "--delete":
                delete = True
            elif name == "--mirror":
                mirror = True
            elif name == "--all":
                every = True
            elif name == "--dry-run":
                dry_run = True
        elif a.startswith("-") and len(a) > 1:
            # A cluster like -fu; -o takes the rest of it, or the next
            # token, as its value.
            for j, flag in enumerate(a[1:], start=1):
                if flag == "o":
                    if j == len(a) - 1:
                        i += 1
                    break
                force = force or flag == "f"
                delete = delete or flag == "d"
                dry_run = dry_run or flag == "n"
        else:
            positionals.append(a)
        i += 1

    if dry_run:
        return None
    remote = positionals[0] if positionals else "origin"
    refspecs = positionals[1:]
    protected = _protected(cwd, remote)

    if mirror:
        return "every branch (--mirror)"
    if every and (force or delete):
        return "every branch (--all)"

    if not refspecs:
        if not force:
            return None
        # A bare push goes where @{push} points (push.default and the
        # upstream decide), which may be main from a local branch named
        # something else.
        target = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
        if "/" in target:
            target = target.split("/", 1)[1]
        else:
            target = _branch("HEAD", cwd)
        return target if target in protected else None

    for spec in refspecs:
        plus = spec.startswith("+")
        spec = spec.lstrip("+")
        if ":" in spec:
            src, dst = spec.split(":", 1)
            target = _branch(dst or src, cwd)
            if not src:  # `:main` deletes main
                if target in protected:
                    return target
                continue
        else:
            target = _branch(spec, cwd)
        if (force or plus or delete) and target in protected:
            return target
    return None


def protected_force_push(command: str, cwd: str, _depth: int = 0) -> str | None:
    """Return the protected branch a command force-pushes or deletes, or None.

    Looks through wrapper commands (sudo, env, timeout, …) and into command
    substitutions.
    """
    if "push" not in command or _depth > 4:
        return None
    for segment in split_commands(strip_line_continuations(command)):
        for body in extract_command_substitutions(segment):
            hit = protected_force_push(body, cwd, _depth + 1)
            if hit:
                return hit
        cleaned = unwrap_command(clean_segment(segment))
        if not cleaned or "push" not in cleaned:
            continue
        try:
            tokens = shlex.split(cleaned)
        except ValueError:
            continue
        push = _push_args(tokens, cwd)
        if push is None:
            continue
        hit = _check_push(*push)
        if hit:
            return hit
    return None
