#!/usr/bin/env python3
"""
Plan a CalVer release from the Conventional Commits since the last release.

Versions follow Home Assistant's own scheme, YYYY.M.N, where N counts the
releases in the month from 0: 2026.11.0, 2026.11.1. A pre-release of the next
version is YYYY.M.N-bK: 2026.11.2-b0, 2026.11.2-b1, then 2026.11.2.

Home Assistant and HACS offer an update only when the new version sorts above
the installed one (AwesomeVersion), so:
- N continues from the highest one already used in the month. Releases up to
  2026.10.13.2 used YYYY.M.D.N, so October 2026 continues at 2026.10.14.
- The pre-release suffix needs the dash: AwesomeVersion ignores a "b0" that
  directly follows a two-digit N, so 2026.11.12b0 would compare equal to
  2026.11.12, while 2026.11.12-b0 sorts below it.

Usage:
    release.py plan [--prerelease] [--notes-file PATH]
        Print `version=`, `previous=` and `releasable=` lines (GitHub Actions
        output format) and write the release notes to PATH.
    release.py stamp VERSION
        Write VERSION into manifest.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "alarmdotcom" / "manifest.json"
REPO_URL = "https://github.com/SeloraHomes/ha-alarmdotcom"

# YYYY.M.N[-bK], plus the earlier YYYY.M.D[.N][bK] tags.
TAG = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)(?:\.(\d+))?(?:-?b(\d+))?$")
COMMIT = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<description>.+)$")
SECTIONS = {"feat": "Features", "fix": "Bug fixes", "perf": "Performance"}


@dataclass(frozen=True)
class Commit:
    """A Conventional Commit on the release range."""

    sha: str
    type: str
    scope: str | None
    description: str
    breaking: bool


def git(*args: str) -> str:
    """Run git in the repository root and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def tag_key(tag: str) -> tuple[int, int, int, int, int, int]:
    """Sort key for a release tag; a pre-release sorts below its final release."""
    match = TAG.match(tag)
    if not match:
        raise ValueError(f"Not a release tag: {tag}")
    year, month, number, serial, pre = match.groups()
    is_final = pre is None
    return (int(year), int(month), int(number), int(serial or 0), int(is_final), int(pre or 0))


def is_stable(tag: str) -> bool:
    """Whether a release tag is a final release rather than a pre-release."""
    return bool(tag_key(tag)[4])


def release_tags() -> list[str]:
    """All release tags, oldest first."""
    return sorted((tag for tag in git("tag", "--list").splitlines() if TAG.match(tag)), key=tag_key)


def next_version(tags: list[str], today: dt.date, *, prerelease: bool) -> str:
    """Return the next free version for this month."""
    month = [tag_key(tag) for tag in tags if tag_key(tag)[:2] == (today.year, today.month)]
    released = [key[2] for key in month if key[4]]
    number = max(released) + 1 if released else 0
    version = f"{today.year}.{today.month}.{number}"
    if not prerelease:
        return version
    betas = [key[5] for key in month if key[2:5] == (number, 0, 0)]
    return f"{version}-b{max(betas) + 1 if betas else 0}"


def commits_since(ref: str | None) -> list[Commit]:
    """Conventional Commits reachable from HEAD but not from ref."""
    log = git("log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", f"{ref}..HEAD" if ref else "HEAD")
    commits = []
    for record in filter(None, (entry.strip() for entry in log.split("\x1e"))):
        sha, subject, body = [*record.split("\x1f"), "", ""][:3]
        if match := COMMIT.match(subject):
            commits.append(
                Commit(
                    sha=sha,
                    type=match["type"],
                    scope=match["scope"],
                    description=match["description"],
                    breaking=bool(match["breaking"]) or "BREAKING CHANGE" in body,
                )
            )
    return commits


def render_notes(commits: list[Commit], previous: str | None, version: str) -> str:
    """Release notes grouped by change type."""

    def line(commit: Commit) -> str:
        scope = f"**{commit.scope}:** " if commit.scope else ""
        return f"- {scope}{commit.description} ({commit.sha[:7]})"

    groups = [("Breaking changes", [c for c in commits if c.breaking])]
    groups += [(title, [c for c in commits if c.type == kind and not c.breaking]) for kind, title in SECTIONS.items()]
    parts = [f"## {title}\n\n" + "\n".join(map(line, entries)) for title, entries in groups if entries]
    if previous:
        parts.append(f"**Full changelog:** {REPO_URL}/compare/{previous}...{version}")
    return "\n\n".join(parts) + "\n"


def plan(args: argparse.Namespace) -> None:
    """Print the release plan and write the notes."""
    tags = release_tags()
    # Stable releases are cut from everything since the last stable one, so a
    # stable release also lists what earlier pre-releases already shipped.
    candidates = tags if args.prerelease else [tag for tag in tags if is_stable(tag)]
    previous = candidates[-1] if candidates else None
    commits = commits_since(previous)
    version = next_version(tags, dt.datetime.now(dt.UTC).date(), prerelease=args.prerelease)
    releasable = any(c.breaking or c.type in SECTIONS for c in commits)

    if args.notes_file:
        Path(args.notes_file).write_text(render_notes(commits, previous, version), encoding="utf-8")
    print(f"version={version}")
    print(f"previous={previous or ''}")
    print(f"releasable={'true' if releasable else 'false'}")


def stamp(args: argparse.Namespace) -> None:
    """Write the version into manifest.json, keeping its formatting."""
    if not TAG.match(args.version):
        raise SystemExit(f"Not a release version: {args.version}")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["version"] = args.version
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--prerelease", action="store_true")
    plan_parser.add_argument("--notes-file")
    plan_parser.set_defaults(func=plan)
    stamp_parser = commands.add_parser("stamp")
    stamp_parser.add_argument("version")
    stamp_parser.set_defaults(func=stamp)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
