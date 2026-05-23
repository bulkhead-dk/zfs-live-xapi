#!/usr/bin/env python3
# Extract the body of a single `## [X.Y.Z]` block from CHANGELOG.md.
#
# Used by mirror-releases.yml (#92): when a tag
# arrives at the GitHub mirror via push-mirror from git.bulkhead.dk,
# this script emits the matching changelog block to stdout so the
# workflow can hand it to `gh release create --notes-file`. Pure
# stdlib so it runs on the GitHub-hosted runner without an extra
# pip install step.

import argparse
import re
import sys

# `## [X.Y.Z]` optionally followed by ` - YYYY-MM-DD` (the Keep a
# Changelog convention). Capture the bracketed name only — the
# date suffix isn't load-bearing for matching, just style.
_HEADING = re.compile(r"^##\s+\[([^\]]+)\](?:\s*-\s*\S+)?\s*$")


def extract(text, version):
    """Return the body of the `[version]` section, stripped of its heading.

    Raises KeyError if no section with that bracketed name exists.
    Leading/trailing blank lines are trimmed; the result ends with
    exactly one newline so it slots cleanly into a release-notes file.
    """
    in_block = False
    found = False
    out = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            if m.group(1) == version:
                in_block = True
                found = True
                continue
            if in_block:
                # Hit the next section — stop.
                break
            continue
        if in_block:
            out.append(line)
    if not found:
        raise KeyError(version)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("version", help='Section name to extract, e.g. "1.0.0" (no leading v).')
    p.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        help="Path to CHANGELOG.md (default: CHANGELOG.md in cwd).",
    )
    args = p.parse_args(argv)
    with open(args.changelog) as f:
        sys.stdout.write(extract(f.read(), args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
