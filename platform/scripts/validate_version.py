#!/usr/bin/env python3
"""
validate_version.py — Validates version.txt and computes the final artifact version tag.

Usage:
    python validate_version.py --branch <branch> [--tag <tag>]
                                [--build-number <n>] [--sha <sha>]
                                [--version-file <path>]

On success:
    Prints the computed version tag to stdout (e.g. "1.2.0-SNAPSHOT-a3f1c2d")
    Exits 0

On failure:
    Prints error message to stderr
    Exits 1 (blocking — Jenkins treats non-zero exit as stage failure)

Branch rules:
  feature/*    → validate only, not published → outputs <version>
  poc/*        → validate only, outputs poc-<poc-name>
  develop      → SNAPSHOT: <version>-SNAPSHOT-<sha>
  release/X.Y.Z→ RC: version.txt must equal X.Y.Z → <version>-rc.<build_number>
  main (tag)   → Release: tag vX.Y.Z must equal version.txt → <version>
  main (no tag)→ Abort — no release build triggered without a tag
  hotfix/X.Y.Z → Hotfix: validate only, no coherence with develop → <version>
  other        → validate only → <version>
"""

import argparse
import re
import sys
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _fail(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_version_txt(path: Path) -> str:
    if not path.exists():
        _fail(
            f"version.txt not found at {path.absolute()}. "
            "Every service repo must contain a version.txt with a semver value (X.Y.Z)."
        )
    version = path.read_text().strip()
    if not version:
        _fail("version.txt is empty. It must contain a semver value (e.g. 1.2.0).")
    if not SEMVER_RE.match(version):
        _fail(
            f"version.txt contains '{version}' which is not valid semver (X.Y.Z). "
            "Use exactly three dot-separated non-negative integers, e.g. 1.2.0"
        )
    return version


def compute_version(branch: str, tag: str, build_number: str, sha: str,
                    version_file: Path) -> str:
    """
    Validate version.txt and return the computed artifact version tag for the branch.
    Exits 1 on any coherence violation.
    """
    version = read_version_txt(version_file)

    # ── feature/* ──────────────────────────────────────────────────────────────
    if branch.startswith("feature/"):
        # Validate only — feature branches are not published
        return version

    # ── poc/* ──────────────────────────────────────────────────────────────────
    if branch.startswith("poc/"):
        poc_name = branch[len("poc/"):]
        if not poc_name:
            _fail(f"POC branch '{branch}' has no name after 'poc/'. Use 'poc/<name>'.")
        return f"poc-{poc_name}"

    # ── develop ────────────────────────────────────────────────────────────────
    if branch == "develop":
        if not sha:
            _fail(
                "--sha is required for the develop branch. "
                "Pass the short git SHA so the snapshot tag is unique: "
                "--sha $(git rev-parse --short HEAD)"
            )
        return f"{version}-SNAPSHOT-{sha}"

    # ── release/X.Y.Z ──────────────────────────────────────────────────────────
    if branch.startswith("release/"):
        branch_version = branch[len("release/"):]
        if not SEMVER_RE.match(branch_version):
            _fail(
                f"Release branch '{branch}' does not follow the 'release/X.Y.Z' naming convention."
            )
        if branch_version != version:
            _fail(
                f"Version mismatch: release branch implies version '{branch_version}' "
                f"but version.txt contains '{version}'. "
                "Update version.txt to match the release branch name before building."
            )
        return f"{version}-rc.{build_number}"

    # ── main ───────────────────────────────────────────────────────────────────
    if branch == "main":
        if not tag:
            _fail(
                "Building on 'main' without a git tag — aborting. "
                "No release artifact is produced unless the commit is tagged. "
                "Tag the commit with 'vX.Y.Z' matching version.txt to trigger a release build."
            )
        expected_tag = f"v{version}"
        if tag != expected_tag:
            _fail(
                f"Tag mismatch: version.txt contains '{version}' "
                f"(expected tag '{expected_tag}') but the current tag is '{tag}'. "
                "Ensure the git tag matches version.txt exactly."
            )
        return version

    # ── hotfix/X.Y.Z ───────────────────────────────────────────────────────────
    if branch.startswith("hotfix/"):
        # Hotfix branches from main; no coherence check with develop required.
        # The hotfix version may be ahead of develop's current version.txt.
        return version

    # ── other branches ─────────────────────────────────────────────────────────
    # Unknown branch pattern: validate version.txt format only
    return version


def main():
    parser = argparse.ArgumentParser(
        description="Validate version.txt and compute the artifact version tag"
    )
    parser.add_argument("--branch", required=True,
                        help="Current git branch name (e.g. develop, release/1.2.0, main)")
    parser.add_argument("--tag", default="",
                        help="Current git tag on the commit, if any (e.g. v1.2.0)")
    parser.add_argument("--build-number", default="1",
                        help="CI build number used for RC tags (e.g. $BUILD_NUMBER in Jenkins)")
    parser.add_argument("--sha", default="",
                        help="Short git SHA appended to SNAPSHOT tags (e.g. a3f1c2d)")
    parser.add_argument("--version-file", default="version.txt",
                        help="Path to version.txt (default: version.txt in current directory)")
    args = parser.parse_args()

    result = compute_version(
        branch=args.branch,
        tag=args.tag.strip(),
        build_number=args.build_number.strip(),
        sha=args.sha.strip(),
        version_file=Path(args.version_file),
    )
    print(result)


if __name__ == "__main__":
    main()
