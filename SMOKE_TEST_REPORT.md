# Smoke test report — Kinpaku Signalpost Agent

Live run on 2026-11-17 against the public Brønnøysund APIs (and registry-listed company websites).
Every number below comes from the committed artifacts in `smoke/`. Nothing here is estimated.

## Batch

* **Selection:** the kit's own selector, unchanged, with its default seed:
  `uv run python select_entry_batch.py --universe signalpost-universe.jsonl.gz --count 100 --output smoke/smoke-companies.jsonl`
  (seed 20260823).
* **Universe file:** `https://builderr.ai/signalpost-company-universe-2025.jsonl.gz`, 411,160 rows,
  which matches `data/universe-metadata.json`.
* **Mix:** 91 AS, 3 BRL, 2 ESEK, 2 STI, 1 SA, 1 ANS. 7 of the 100 have a registry-listed website.

## Command

```bash
uv run python scripts/run_signalpost.py --organisations smoke/smoke-companies.jsonl \
  --output smoke/run-A/envelopes.jsonl --state-dir smoke/state --run-id smoke-A --expected-count 100
```

Run B used the same command with `--output smoke/run-B/envelopes.jsonl --run-id smoke-B`. It diffs
against run A's snapshot.

## Headline results

| Metric | Run A | Run B (rerun) |
|---|---|---|
| Inputs | 100 | 100 |
| Terminal envelopes emitted | **100 / 100** | **100 / 100** |
| `terminal_status` | 100 `completed` | 100 `completed` |
| Contract + evidence-link validation | passed | passed |
| Wall-clock runtime | 213.1 s (≈ 3.6 min) | 213.1 s |
| Per-company runtime (mean / p50 / p95 / max) | 16.4 s / 14.7 s / 31.6 s / 35.7 s | 16.4 s / 14.7 s / 31.8 s / 35.8 s |
| Network requests (all sources) | 562 (509 to data.brreg.no) | same |
| Brreg latency p50 / p95 | 887 ms / 1,021 ms | — |
| Claims emitted / `available` | 5,147 / 4,827 | 5,147 / 4,827 |
| `changes` vs previous snapshot | 0 (first run) | **0**, idempotent; same claims digest as A |
| Third-party cost | **$0** (no paid APIs, no models, no keys) | $0 |

Per-company times overlap because 8 companies run in parallel. Wall time is dominated by the deliberate
throttle on the annual-account filing-years endpoint (at most 1 request per 2.1 s, about 30/min).

**Faster mode:** a separate run with `--no-filing-years` (separate state dir, not committed) took 139 s.
It was slowed by website timeouts in this sandbox; see failure modes below. At the default setting,
1,000 companies is *estimated* (not measured) at about 35 min: 1,000 × 2.1 s for the filing-years throttle. The default 50-minute time
budget then skips only the optional modules, and only for the companies that remain.

## Coverage (run A, per company)

| Section / field | Result |
|---|---|
| Identity (name, form, dates, NACE, addresses, status flags, capital, purpose) | 100 / 100 available |
| Leadership: `registered_roles` | 100 / 100 (source checked) |
| Leadership: board chair | 99 available, 1 explicitly `not_available` |
| Leadership: CEO (daglig leder) | 64 available, 29 `not_available` for AS/ASA (small AS are not required to have one) |
| Workplaces: registered sub-unit count | 100 (exact count; 74 have ≥ 1 sub-unit listed) |
| Financials: latest company period | 100 / 100 |
| Financials: revenue | 73 |
| Financials: net result / total assets | 100 / 100 |
| Financials: history (all returned periods) | 100 |
| Financials: filing years and official copy links | 100 |
| Group (only fetched when `erIKonsern=true`) | 9 available (6 with ultimate parent), 91 `not_applicable` |
| Employees | 10 available, 90 `not_available` (the registry holds no count; never reported as 0) |
| Official website | 2 verified `available`, 5 `blocked` (see failure modes), 93 `not_available` |
| Hiring (careers page) | 0 / 100. No verified site in this batch exposed a careers link. |

The email-domain candidate path checked 13 non-generic company email domains. None was published as
`official_website`, because none could be verified under the strict rule (org number on the site, or
the exact legal name on the homepage). The rejected candidates include an accountant's domain
(devoldsregnskap.no), a board-services domain (styrerommet.net), and parent/association domains
(bbl.no, formue.no, atg.no). This is the intended precision behaviour.

## Precision spot-check (live re-verification)

Tool: `scripts/spot_check_claims.py --envelopes smoke/run-A/envelopes.jsonl --sample 30` (seed
20261117). Full output is in `smoke/spot-check.json`.

* **30 sampled claims:** every website-section claim, plus a random sample of the rest. Composite
  claims are made of their sub-values: financials 11, identity 9, leadership 3, profile 3, website 2,
  group 2.
* **How each claim was checked:**
  * Every cited evidence item was re-fetched live.
  * For Brreg JSON, the value at the cited `json_pointer` was re-rendered and compared with the stored
    `claim_span`, and the record's organisation number had to equal the input (correct company).
  * For website evidence, the stored text span had to appear in the live page.
* **Result: 30 / 30 correct and attributed to the right company.** I also reviewed the printed
  claim-value / stored-span / live-value triples by hand. For example:
  * revenue history 859,007 → 2,813,366 → 3,092,876 matches pointers `/0`, `/1`, `/2` of the live response;
  * board chair "Hanna Hellum Tangen" matches `/rollegrupper/1/roller/0`;
  * the direct parent's org number matches the group tree;
  * "Carlin Invest AS" appears on carlin.no;
  * "Aas Elektronikk AS" appears on aelektronikk.no.

Two problems found by an earlier spot-check were **fixed before the final runs above**:

1. **robots.txt was failing open.** A robots.txt that timed out was treated as "allowed", and robots
   rules were not checked on redirect targets. This let the agent fetch arkjv.no → www.arkjv.no (a
   SmugMug site whose robots.txt disallows unlisted crawlers) and publish it as a verified website.
   Now an unreadable robots.txt means blocked, and every redirect target is robots-checked before it is
   requested. Regression test added.
2. **Boilerplate site descriptions** ("Powered by SmugMug") were being published as
   `website_self_description`. They are now filtered.

## Change detection / refresh

* **Rerun (run B vs run A):** 0 changes and identical claims digests, so the refresh is idempotent.
  The prior snapshot `smoke/state/snapshots/smoke-A/` was preserved, and B was written alongside it.
* **Seeded-change demo (`smoke/change-demo/`):**
  * Setup: two companies were re-run against a deliberately altered copy of their run-A envelopes. The
    copy had the legal name changed, `net_result` shifted by 1,000, and `business_address` removed.
  * Result: all 6 seeded differences were detected (`value_changed` ×4, `added` ×2), each with the new
    evidence ids. There were no other changes.
* **Kit replay:** the kit's offline replay (`scripts/run_refresh_replay.py`) still passes: 2/2 expected
  changes, precision 1.0, recall 1.0, idempotent.

## Failure modes seen

1. **Website egress in this sandbox.** Partway through the session, Python HTTPS requests to most
   non-Brreg hosts started timing out through the sandbox's egress proxy. curl kept working and
   data.brreg.no was unaffected. In the final runs this left 5 registry-listed sites as `blocked`,
   because their robots.txt could not be read. Under the conservative policy they were not fetched, and
   nothing was claimed. In an earlier run of the same batch before the proxy change (and before the
   robots fix), these sites were reachable:
   * 7-eleven.no (franchise store) → `ambiguous`, correctly not claimed;
   * studiox.no → appelx.no showed a *different* organisation number → `ambiguous` (conflicting identity);
   * aiota.no → unverified;
   * betakst.no and tnbbl.no → connection reset (also with curl).

   These results mean website and hiring coverage in this report is a lower bound for an unrestricted
   network.
2. **JavaScript-only sites** (e.g. Elopak, seen in development) expose no text statically, so ownership
   cannot be verified and the site is reported `ambiguous`. No browser rendering is used.
3. **Brreg accounts API returns HTTP 500 for bank/insurance statement layouts** (seen for DNB Bank ASA in
   development). This is classified as `not_available` with the reason, and not retried.
4. **Low registry website coverage** (7/100 listed) caps the website and hiring sections. The agent
   deliberately does not guess websites from search engines.
5. **No transient Brreg errors** occurred in the final runs (0 `partial`, 0 `failed`). The retry and
   backoff path is covered by unit tests only.
