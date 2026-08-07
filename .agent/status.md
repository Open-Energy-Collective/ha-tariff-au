# Build Session Status Log

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
