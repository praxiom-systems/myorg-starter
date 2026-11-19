"""The guard's record of what it stopped, and what happened next (#1195).

Each stop, bounce, note or gate prompt from the Bash guard adds a row to
``guard-events.jsonl`` in the user's getmyorg state directory. Commands the
guard lets through add nothing. When gate mode asks the user, the guard also
leaves a pending marker named after the tool call, and the prompt's answer
arrives later as an outcome row for that id:

- ``ran``: ``guard-outcome.sh``, Claude Code's after-command hook, saw the
  command run, so the user approved;
- ``declined``: the session's transcript shows the user rejected it
  (``resolve_pending``, run by the guard's next hook call);
- ``unknown``: neither within a week.

A prompt counts once its outcome row is written, never before, so a late
answer is never miscounted.

A row holds the command's shape, never the command: the program, its flags,
and known subcommands (``git push --force``, ``docker stop``). The first word
that could be a value ends it, so paths, arguments, URLs, resource and
script names stay out. The file holds only what a user who turns on
``/myorg:config share-stats`` would send, and aggregated at that.
getmyorg's uploader re-applies the same rule before sending; keep its
tables in step with these (a test there compares them).

Unlike the shadow log, this runs for every user. ``MYORG_GUARD_EVENTS=0``
turns it off. Failures are silent: the record must never change a decision.
Runs on python3.9 (macOS), like the rest of the plugin's hooks.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

EVENTS_NAME = "guard-events.jsonl"
PENDING_NAME = "guard-pending"
SCHEMA_VERSION = 1
# One rotation at about 1 MB keeps a few months of stops on a busy machine.
MAX_BYTES = 1_000_000
# A gate prompt with no answer seen within a week gets an "unknown" outcome.
PENDING_TTL_SECONDS = 7 * 24 * 3600
# How much of a transcript to read past where it stood at the prompt.
_TRANSCRIPT_SCAN_BYTES = 32 * 1024 * 1024
# How Claude Code records a rejected permission prompt in the transcript.
_REJECTED = (b'"toolUseResult":"User rejected tool use', b"doesn't want to proceed with this tool use")
OUTCOMES = ("ran", "declined", "unknown")

# Decisions worth a row: everything but a silent pass.
RECORDED = frozenset({"deny", "ask", "note", "gate"})

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# The command word: a plain program name. A word with a path in it is the
# user's own script, and its name stays out.
_COMMAND_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
# Words after it that are part of the shape: flags, and for the tools below,
# a subcommand before the first flag. Any other word ends the shape, so an
# argument (`ssh prodbox`, `cat notes`) never gets in.
# Short flags are a cluster of at most three letters (`-rf`, `-f`), so a
# value glued on (`-ppassword`, `-uroot`, `-p3306`) ends the shape instead;
# long flags are words, and `--name=value` never matches.
_FLAG = re.compile(r"^(?:-[A-Za-z]{1,3}|--[a-z][a-z0-9-]{1,23})$")
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
# The subcommands a shape may keep: the common verbs and nouns of the tools
# below. Any other word in that place (a container, release, script or
# remote name) ends the shape: `docker stop payroll-api` is `docker stop`.
SUBCOMMANDS = frozenset("""
    add alias am api apply apps archive audit auth autoremove blame branch build buildx cache
    cancel checkout cherry-pick ci clean cleanup clone close clusters codespace comment commit
    compose compute config container cp create d1 daemon-reload db default delete deploy
    deployments describe destroy dev diff disable dlx domains down download drain edit enable
    env exec extension fetch fmt force-unlock fork freeze functions gc gen get gist grep
    history i iam image images import init inspect install instances issue kill kv label link
    lint list lock log login logout logs ls merge migration migrations mod mv network new
    output pack package pages patch pip plan plugin port-forward pr projects prune ps publish
    pull purge push queues r2 ready rebase rebuild reflog refresh release reload remote
    remove rename reopen repo reset restart restore resume revert review rm rmi rollback
    rollout run s3 scale secret secrets services set show ssh start state stash status stop
    switch sync system tag tail taint task template test tidy tool top uninstall untaint
    update upgrade up use-context validate variable venv version versions vet view volume
    volumes watch whoami workflow workspace worktree
""".split())
# Wrappers run the command named next; it keeps its own shape.
_WRAPPERS = frozenset({"command", "doas", "env", "exec", "nice", "nohup", "sudo", "time", "timeout", "xargs"})
# Tools whose next word is a subcommand, and those with two levels of them
# (`gh pr merge`, `docker compose up`).
_ONE_LEVEL = frozenset({
    "apt", "apt-get", "brew", "bun", "cargo", "codex", "deno", "dotnet", "flyctl", "git", "go",
    "npm", "npx", "pip", "pip3", "pnpm", "rustup", "systemctl", "terraform", "tofu", "uv",
    "vercel", "yarn",
})
_TWO_LEVELS = frozenset({
    "aws", "az", "docker", "gcloud", "getmyorg", "gh", "helm", "kubectl", "podman", "supabase", "wrangler",
})
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_MAX_SHAPE_WORDS = 2
_MAX_SHAPE_CHARS = 80


def _subcommand_levels(word: str) -> int:
    if word in _TWO_LEVELS:
        return 2
    if word in _ONE_LEVEL:
        return 1
    return 0


def state_dir() -> Path:
    """``$XDG_STATE_HOME/getmyorg``, else ``~/.local/state/getmyorg``.

    ``guard-outcome.sh`` resolves the same directory; keep them in step.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "getmyorg"


def events_path() -> Path:
    return state_dir() / EVENTS_NAME


def enabled() -> bool:
    return os.environ.get("MYORG_GUARD_EVENTS", "").strip() != "0"


def shape(segment: str) -> str:
    """The command's shape: its program and up to two subcommand or flag words.

    ``pkill -f "next start"`` → ``pkill -f``; ``git push --force origin`` →
    ``git push --force``; ``./deploy.sh prod`` → ``<script>``.
    """
    words = segment.strip().split()
    while words and _ENV_ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return ""
    head = words[0]
    if "/" in head:
        return "<script>"
    if not _COMMAND_WORD.match(head):
        return "<other>"
    out = [head]
    wrapped = head in _WRAPPERS  # the next plain word is the command run
    levels = _subcommand_levels(head)
    for word in words[1:]:
        if len(out) > _MAX_SHAPE_WORDS:
            break
        if _FLAG.match(word):
            # After a flag, plain words are its arguments: a wrapper's too
            # (`sudo -u alice`), so the shape ends at the next plain word.
            levels, wrapped = 0, False
        elif wrapped and word[:1].isalpha() and _COMMAND_WORD.match(word):
            wrapped, levels = False, _subcommand_levels(word)
        elif levels and _SUBCOMMAND.match(word) and word in SUBCOMMANDS:
            levels -= 1
        else:
            break
        out.append(word)
    # getmyorg takes shapes of at most 80 characters when they are shared.
    while len(out) > 1 and len(" ".join(out)) > _MAX_SHAPE_CHARS:
        out.pop()
    return " ".join(out)


def build_row(
    *, decision: str, rule: str | None, shape_text: str, host: str, level: str, gate: bool,
    tool_use_id: str | None, now: float | None = None,
) -> dict:
    row = {
        "v": SCHEMA_VERSION,
        "ts": int((now if now is not None else time.time()) * 1000),
        "host": host,
        "level": level,
        "gate": "on" if gate else "off",
        "decision": decision,
        "rule": rule,
        "shape": shape_text,
    }
    if tool_use_id and _ID.match(tool_use_id):
        row["id"] = tool_use_id
    return row


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass  # no record yet, so nothing to rotate
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _mark_pending(tool_use_id: str, transcript_path: str | None) -> None:
    """Leave the marker the prompt's outcome resolves: where the transcript
    stood, so ``resolve_pending`` reads only what came after."""
    pending = state_dir() / PENDING_NAME
    pending.mkdir(parents=True, exist_ok=True)
    marker = {"transcript": "", "offset": 0}
    if transcript_path:
        try:
            marker = {"transcript": transcript_path, "offset": os.path.getsize(transcript_path)}
        except OSError:
            marker = {"transcript": transcript_path, "offset": 0}
    (pending / tool_use_id).write_text(json.dumps(marker), encoding="utf-8")


def _outcome(tool_use_id: str, outcome: str, now: float) -> None:
    _append(events_path(), {"v": SCHEMA_VERSION, "ts": int(now * 1000), "id": tool_use_id, "outcome": outcome})


def _rejected(marker: dict, tool_use_id: str) -> bool:
    """Whether the transcript, after the marker's offset, records the user
    rejecting this tool call."""
    path = marker.get("transcript") or ""
    if not path:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(int(marker.get("offset") or 0))
            data = f.read(_TRANSCRIPT_SCAN_BYTES)
    except (OSError, ValueError):
        return False
    needle = b'"tool_use_id":"' + tool_use_id.encode() + b'"'
    for line in data.splitlines():
        if needle in line and any(r in line for r in _REJECTED):
            return True
    return False


def resolve_pending(now: float | None = None) -> None:
    """Write the outcome of gate prompts that have one by now: ``declined``
    when the transcript shows the user rejected it, ``unknown`` after a
    week. Approvals come from ``guard-outcome.sh``. Cheap when nothing is
    pending: one directory listing. Silent on any failure."""
    if not enabled():
        return
    try:
        pending = state_dir() / PENDING_NAME
        names = os.listdir(pending)
    except OSError:
        return
    now = time.time() if now is None else now
    for name in names:
        if not _ID.match(name):
            continue
        path = pending / name
        try:
            age = now - path.stat().st_mtime
            try:
                marker = json.loads(path.read_text(encoding="utf-8") or "{}")
            except ValueError:
                marker = {}
            if _rejected(marker if isinstance(marker, dict) else {}, name):
                outcome = "declined"
            elif age > PENDING_TTL_SECONDS:
                outcome = "unknown"
            else:
                continue
            path.unlink()
            _outcome(name, outcome, now)
        except OSError:
            continue  # another hook resolved it first


def record(
    *, decision: str, rule: str | None, segment: str, host: str, level: str, gate: bool,
    tool_use_id: str | None, transcript_path: str | None = None,
) -> None:
    """Add the guard's decision to the record. Silent on any failure."""
    if decision not in RECORDED or not enabled():
        return
    try:
        now = time.time()
        row = build_row(
            decision=decision, rule=rule, shape_text=shape(segment), host=host, level=level,
            gate=gate, tool_use_id=tool_use_id, now=now,
        )
        _append(events_path(), row)
        if decision == "gate" and "id" in row:
            _mark_pending(row["id"], transcript_path)
    except Exception:
        return  # observability only: never let the record change a decision


__all__ = [
    "OUTCOMES", "RECORDED", "SUBCOMMANDS", "build_row", "enabled", "events_path", "record",
    "resolve_pending", "shape", "state_dir",
]
