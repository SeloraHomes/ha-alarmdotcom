#!/usr/bin/env python3
"""
Check files for trailing whitespace, blank lines at EOF, and conflict markers.

Usage: check_whitespace.py FILE...

Takes the files as arguments (unlike `git diff --cached --check`), so the same
check works on staged files in the hook and on every file in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CONFLICT_MARKER = re.compile(rb"^(<{7}|={7}|>{7})( |$)")
# Markdown uses two trailing spaces as a hard line break.
TRAILING_SPACE_ALLOWED = {".md"}


def problems(path: Path) -> list[str]:
    """Return the problems found in one file."""
    data = path.read_bytes()
    if b"\0" in data[:8000]:
        return []  # binary
    found = []
    lines = data.split(b"\n")
    for number, line in enumerate(lines, start=1):
        if CONFLICT_MARKER.match(line):
            found.append(f"{path}:{number}: conflict marker")
        if line.rstrip(b"\r") != line.rstrip() and path.suffix not in TRAILING_SPACE_ALLOWED:
            found.append(f"{path}:{number}: trailing whitespace")
    if data.endswith(b"\n\n"):
        found.append(f"{path}: blank line at end of file")
    return found


def main(argv: list[str]) -> int:
    """Check every file given on the command line."""
    found = [problem for name in argv[1:] if (path := Path(name)).is_file() for problem in problems(path)]
    if found:
        print("\n".join(found), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
