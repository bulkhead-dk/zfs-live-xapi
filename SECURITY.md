# Security policy

## Reporting a vulnerability

For any security-sensitive issue in `zfs-live` — credential leakage, sandbox escape, an attacker-controllable input that reaches privileged code, an SR-level data exposure between tenants, or anything else where a public Issue would help an attacker faster than it'd help us — **email [`jakob@wolffhechel.dk`](mailto:jakob@wolffhechel.dk) directly**. Don't open a public Issue or merge request on `git.bulkhead.dk`, the GitHub mirror, or any other tracker.

A clear report lets us respond fast. The minimum:

- A short description of the vulnerability.
- Reproduction steps (the smallest case you have).
- Affected version(s) — output of `xe sm-list type=zfs-live` on the host or the git tag/commit you're running.
- Operator impact: what an attacker gets, in your assessment, on a typical XAPI deployment.

PGP encryption isn't required today — there's no published key. If we publish one, this section will say so. Email-in-the-clear over TLS is fine for now; the Internet has Reasonable Defaults.

## Disclosure expectations

- **Acknowledgement within 72 hours** of receipt. If you don't hear back, ping us — mail can drop.
- **Triage and severity assessment within 7 days.** We'll tell you what we think, what fix path looks like, and the rough timeline.
- **Coordinated disclosure on the current supported line.** We'll work with you on the timing and on any embargo windows reasonable for the impact level. We default to "fix first, then publish" unless the vulnerability is being actively exploited or a fix is unreasonably delayed.
- **Credit in the advisory** unless you ask us not to. Acknowledged researchers are listed by the name they choose; anonymity is fine.

## Supported versions

| Version line | Status |
|--------------|--------|
| Latest tagged release | **Supported.** Security fixes land as patch releases. |
| Untagged forks / older snapshots | Not supported. Update to the latest release to get fixes. |

## Out of scope

- **Configuration mistakes** (e.g. an operator running `xe sr-create` on an unintended pool). Configuration safety is the operator's responsibility; see [docs/troubleshooting.md](docs/troubleshooting.md) for common cases.
- **Upstream xen-api / xapi-storage-script / qemu-dp / ZFS-on-Linux vulnerabilities.** Report those to the upstream projects directly; we'll happily route to the right place if you're not sure where.
- **Findings only relevant to a debug build, fork, or non-supported version.**

## Commercial-evaluation security responses

Operators running `zfs-live` under the 270-day commercial evaluation clause described in [LICENSE.md](LICENSE.md) get the same security-response contract documented here, with no exclusion for the evaluation period.

## Bug-bounty programme

There isn't one. If that changes, this section will say so.

---

For non-security bug reports and feature requests, file an Issue on [`git.bulkhead.dk/storage/zfs-live`](https://git.bulkhead.dk/storage/zfs-live) per [CONTRIBUTING.md](CONTRIBUTING.md).
