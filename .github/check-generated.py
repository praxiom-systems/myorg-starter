#!/usr/bin/env python3
"""Check that this repo's files are exactly what getmyorg last published.

myorg-starter is generated: each publish from getmyorg rebuilds the whole tree
and records every file's hash in PUBLISH_MANIFEST.json. A file that differs
from the manifest, or isn't in it, or is missing, was edited by hand and would
be overwritten by the next publish.

Two places run it:
  - myorg-starter's CI, on every PR, so a hand-edit can't merge;
  - getmyorg's publish workflow, on the public tree before it regenerates,
    which also catches a direct push.

  python3 .github/check-generated.py [--tree DIR] [--manifest FILE] [--allow-placeholder]

The manifest defaults to DIR/PUBLISH_MANIFEST.json. PUBLISH_MANIFEST.json
itself and .git are excluded from the comparison. A missing manifest fails,
except with --allow-placeholder on a tree that holds only a README.md: the
first publish into a new starter repo, where there's nothing to overwrite.

Exit codes: 0 in sync, 1 divergence, 2 usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

EXCLUDE_TOP = {".git"}
EXCLUDE_FILES = {"PUBLISH_MANIFEST.json"}
# What a new starter repo holds before its first publish (see the runbook).
PLACEHOLDER = {"README.md"}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if not rel_dir.parts:
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_TOP]
        for f in filenames:
            if not rel_dir.parts and f in EXCLUDE_FILES:
                continue
            rel_file = rel_dir / f
            out[rel_file.as_posix()] = sha256_of(root / rel_file)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tree", type=Path, default=Path("."))
    p.add_argument("--manifest", type=Path, help="default: TREE/PUBLISH_MANIFEST.json")
    p.add_argument(
        "--allow-placeholder", action="store_true",
        help="pass a tree with no manifest if it holds nothing but a README.md"
        " (a new starter repo before its first publish)",
    )
    args = p.parse_args(argv)
    if args.manifest is None:
        args.manifest = args.tree / "PUBLISH_MANIFEST.json"

    if not args.tree.is_dir():
        p.error(f"tree not found: {args.tree}")
    actual = walk_tree(args.tree)
    if not args.manifest.is_file():
        if args.allow_placeholder and set(actual) <= PLACEHOLDER:
            print(f"No manifest, and {args.tree} holds only a placeholder: nothing to protect.")
            return 0
        p.error(f"manifest not found: {args.manifest}")

    try:
        manifest = json.loads(args.manifest.read_text())
        expected = {f["path"]: f["sha256"] for f in manifest["files"]}
    except (ValueError, KeyError, TypeError) as exc:
        p.error(f"manifest {args.manifest} is malformed: {exc!r}")

    violations: list[str] = []
    for path in sorted(set(expected) | set(actual)):
        e, a = expected.get(path), actual.get(path)
        if e is None:
            violations.append(f"unexpected file in tree: {path}")
        elif a is None:
            violations.append(f"file from manifest missing in tree: {path}")
        elif e != a:
            violations.append(f"content differs: {path}")

    if violations:
        for v in violations:
            print(f"DIVERGENCE: {v}", file=sys.stderr)
        print(
            "\nThis repo is generated from getmyorg, and these files no longer match"
            " what it published. Make the change in getmyorg instead: its next"
            " publish opens the regen PR here.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
