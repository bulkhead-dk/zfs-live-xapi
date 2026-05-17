# Contributing to `zfs-live`

Thanks for your interest in `zfs-live`. A few notes on where work lands and how to engage.

## Where to file issues and merge requests

**Issues and merge requests are tracked on [`git.bulkhead.dk/storage/zfs-live`](https://git.bulkhead.dk/storage/zfs-live).** That's the canonical home for the project. The `github.com/bulkhead-dk/zfs-live` repo is a read-only mirror — Issues are disabled there, and any pull request opened against the GitHub mirror is auto-closed with a redirect comment by the workflow under redirect-prs.yml. This is intentional: it keeps the tracker single-canonical so we don't have to reconcile two parallel discussions.

## Licensing of contributions

By opening a merge request you agree your contribution is licensed under the terms of [`LICENSE.md`](LICENSE.md) — inbound = outbound. The license is source-available with a free personal/noncommercial allowance and a paid commercial-use tier; the [README](README.md#license) summarises it. Commercial licensing inquiries: `jakob@wolffhechel.dk`.

## Building and testing

The driver runs on XAPI hosts (verified on XCP-ng 8.3 and Citrix XenServer 8.4.0).

- [`README.md`](README.md) — quick start, architecture overview, known limitations.
- [`docs/`](docs/) — operator documentation: installation, configuration, tuning, troubleshooting.

### Lint checks

```bash
shellcheck -S error installer.sh
```

## Style

- Follow the patterns already in the file you're editing. Where the code is non-obvious, add a *why* comment, not a *what* one.
- New tests for new behaviour. Regression tests are explicit about the bug they capture.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Short version: email `jakob@wolffhechel.dk` directly for security-sensitive findings; public-facing bugs without a security angle go on the canonical tracker as normal.
