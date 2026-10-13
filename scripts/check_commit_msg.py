#!/usr/bin/env python3
"""
Check that a commit message (or PR title) follows Conventional Commits.

Usage:
    check_commit_msg.py <commit-msg-file>   # lefthook commit-msg hook
    check_commit_msg.py --title "<text>"    # CI check on the PR title

PRs are squash-merged with the PR title as the commit subject, so the title is
what lands on main and what the release notes are built from.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TYPES = ("build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test")
PATTERN = re.compile(rf"^({'|'.join(TYPES)})(\([\w./-]+\))?!?: \S.*$")
# Subjects git writes itself; let them through untouched.
EXEMPT = re.compile(r"^(Merge |Revert \"|fixup! |squash! |amend! )")


def check(subject: str) -> str | None:
    """Return an error message, or None when the subject is valid."""
    if EXEMPT.match(subject) or PATTERN.match(subject):
        return None
    return (
        f"Not a Conventional Commit: {subject!r}\n"
        f"Expected '<type>[(scope)][!]: <description>', where type is one of: {', '.join(TYPES)}.\n"
        "Example: 'fix(camera): refresh tokens before they expire'"
    )


def main(argv: list[str]) -> int:
    """Check the subject passed on the command line."""
    if len(argv) == 3 and argv[1] == "--title":
        subject = argv[2].strip()
    elif len(argv) == 2:
        lines = Path(argv[1]).read_text(encoding="utf-8").splitlines()
        subject = next((line for line in lines if line.strip() and not line.startswith("#")), "")
    else:
        print(__doc__, file=sys.stderr)
        return 2

    if error := check(subject):
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
