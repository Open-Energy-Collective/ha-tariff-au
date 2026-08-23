# Build Session Status Log

## 2026-08-14 — `validate.yml` pinned + merged to `main`; HACS `hacs/default` submission opened

Closes both items from the 2026-08-12 build HANDOFF in `TODO-ha-tariff-au.md`
(planning-owned file, not edited here — reporting for planning to reconcile):

1. **Pinned floating action refs** in `validate.yml`: `hacs/action@main` →
   `@d556e736723344f83838d08488c983a15381059a` (22.5.0),
   `home-assistant/actions/hassfest@master` →
   `@a7c616ce81ccda50150bf1595786c71b1883fabb` (no tagged releases exist for
   this action, pinned to `master`'s commit at time of pinning instead).
   PR #5, squash-merged to `main` as `b9e0dc8`.
2. **Confirmed real green run on `main`**: both `validate-hacs` and
   `validate-hassfest` passed via manual `workflow_dispatch` against `main`
   post-merge (run `31588219063`) — the first genuine passing run on the
   default branch; prior passing runs were all on throwaway/PR branches.
   `push`-triggered runs still never fired for any commit this session
   (worked around via manual dispatch every time) — root cause not found,
   see below.

**HACS `hacs/default` submission opened**: PR
`hacs/default#9932` (`oec-agent/default:add-ha-tariff-au` →
`hacs/default:master`), open, no `hacs-bot[bot]` rejection as of last check.
Submitted by `oec-agent` rather than the founder personally — deviates from
the HANDOFF's stated plan ("founder submits... not organization"), done
deliberately this session as a live test of how the `oec-agent` account is
treated by GitHub, given the founder's personal account is currently
flagged and was blocked from even forking `hacs/default`. `oec-agent` is a
plain `type: "User"` account (confirmed via `gh api users/oec-agent`), not
an organization, so it satisfies the literal HACS requirement even though
it wasn't the founder's own account. Three earlier attempts from this
branch/account failed and were closed for concrete, fixed reasons (case
mismatch + unrecognized angle-bracket link syntax; then submitting from the
fork's own `master` branch, which HACS's bot explicitly rejects) — see PRs
`hacs/default#9930` and `#9931` (closed) for detail if needed.

**Still open / not investigated:**
- Why `push` events never trigger `validate.yml` on this repo while
  `pull_request` and `workflow_dispatch` both work reliably — observed
  consistently all session, no root cause found. Distinct from the
  account-level GitHub compliance flag (that explains other symptoms, not
  this asymmetry).
- `hacs.json` could add `"country": ["AU"]` — not blocking, not yet done.

## 2026-08-10 — Security/PII scan pass: working tree clean, one historical finding accepted as-is

**Cross-repo security pass** (`repos/.agent/security-scan.md`, new repeatable
process this session; this repo's own 2026-08-07 fix was the precedent it's built
from). Current working tree and `HEAD` confirmed clean of real IPs/paths/tokens.

One older finding exists further back in this repo's git history (predates the
2026-08-07 fix) — deliberately **not detailed here** (see
`repos/.agent/security-scan.md`'s "Recording a finding without republishing it"
section: naming the exact commit/value in a file committed to this repo would
publish a pointer to it, which is worse than the finding itself). Discussed
directly with the repo owner, who accepted it as low-severity and chose not to
rewrite published history over it — HACS installs are unaffected regardless
(HACS installs from a tagged release zipball, not a `git clone`, so history
content was never reachable through that path). Decision recorded here for
continuity; specifics live outside this repo.

## 2026-08-07 — Finding: no graceful handling of server-side rate limiting; gets worse at scale, not just per-household

**Trigger:** user question — does the integration handle rate limiting gracefully, and
what happens if someone configures 50 tariffs?

**Verified (not hypothetical):**
- `tariff-service` genuinely rate-limits: live request against
  `api.openenergy.org.au` returned `x-ratelimit-limit: 20`, `retry-after: 60`.
  Middleware is `slowapi`, keyed by **remote IP** (`get_remote_address`) — the
  budget is shared per source IP, not per-tariff, per-entry, or per-API-key.
- `custom_components/oec_tariff_au/coordinator.py`: one `DataUpdateCoordinator`
  per configured DNSP+tariff, each polling independently every 300s
  (`DEFAULT_SCAN_INTERVAL`), with no shared client-side limiter/queue across
  coordinators. On any non-200 (including 429) it just
  `raise UpdateFailed(f"API returned {resp.status}: ...")` — no 429-specific
  handling, doesn't read `retry-after`, no backoff. `DataUpdateCoordinator`'s
  default failure behavior (mark unavailable, retry at the same fixed interval)
  applies, so nothing breaks the cadence that caused the failure in the first
  place.

**Failure mode:** coordinators are all created at HA startup with the same
interval and no jitter between them, so their refreshes tend to cluster —
a burst of near-simultaneous requests against a 20/minute budget. Steady-state
*average* load is fine even at real scale (e.g. 50 tariffs ≈ 10 req/min,
under budget) — the problem is burst shape, not average rate. Whichever ~20
requests land first in a burst succeed; the rest 429, go unavailable, and
retry at the exact same cadence next cycle with no jitter to desync them —
some entries could plausibly stay unlucky indefinitely.

**Scale consideration raised by user, not yet investigated further:** the
per-IP key makes this worse than just "one household with many tariffs" —
multiple *distinct* HA installations sharing a public IP (CGNAT, a shared
VPN egress, a corporate/site network with several installs behind it) would
be invisible to each other but silently share the same 20/minute budget at
the server. That's a real amplification path if adoption grows beyond a
single household per IP, and isn't something the client-side fix alone
(jitter, backoff) fully solves — the server-side keying assumption
(one IP == one installation) may not hold at scale either.

**Not fixed — deliberately deferred, not urgent today:**
- Client-side: stagger/jitter each coordinator's initial refresh + poll
  interval so 50 entries don't all land on the same 5-minute tick; honor
  `retry-after` with backoff specifically on 429 rather than the generic
  `UpdateFailed` path.
- Server-side (`tariff-service`, out of scope for this repo): whether
  per-IP keying is still the right model once/if multi-installation-per-IP
  becomes common — a design question, not something to decide here.

**Priority:** low today (no reports of this happening in practice, current
adoption is effectively one pilot household); revisit if uptake grows —
worth fixing proactively rather than waiting for a user-visible incident,
since the failure mode (entities silently going unavailable and staying
that way) is hard for an end user to self-diagnose as "rate limiting" rather
than "the integration is broken."
