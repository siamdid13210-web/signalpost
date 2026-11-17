# Kinpaku Signalpost Agent — submission notes

**Agent name:** Kinpaku Signalpost Agent (v1.0.0)
**Challenge:** Builderr Signalpost company research, Round 1
**Approach:** precision first; official Brønnøysund registers are the backbone; the company's own
website is used only after its ownership is verified by exact organisation number or exact legal name.

## Single evaluator command

```bash
uv sync --frozen
uv run python scripts/run_signalpost.py --organisations <batch.jsonl> --output out/envelopes.jsonl --run-id <run-id>
```

* `<batch.jsonl>`: the kit's batch format (`{"organisation_number": "..."}` per line). `.json`
  (list or `{"organisation_numbers": [...]}`) and plain-text one-number-per-line files also work.
  You can also pass numbers as positional arguments.
* Output: `out/envelopes.jsonl` has exactly one terminal envelope per unique input, in input order.
  `out/envelopes/<orgnr>.json` has one pretty-printed envelope per company. `out/envelopes.report.json`
  is the machine-readable run report (runtime, request counts, cost, coverage and validation).
* Exit code 0 means every input got a terminal envelope and every evidence link validated.
* Optional flags:
  * `--expected-count N` fails fast if the input count is wrong.
  * `--time-budget-minutes` defaults to 50. After this point the optional website and filing-year
    modules are skipped, and those claims are marked `not_available` / `checked:false`.
  * `--hard-deadline-minutes` defaults to the budget plus 10. Unfinished companies then get a `failed`
    envelope, so no input is ever silently dropped.
  * `--no-filing-years` gives the fastest run (see runtime below).
  * `--resume` continues an interrupted run with the same `--run-id`.
  * `--bulk brreg-enheter.csv.gz` is an optional fallback used only if the live entity API fails.
    It is **not** required.

### Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Dependencies are pinned in `uv.lock`, and no
new dependencies were added to the kit. No downloads are needed: the agent reads the live public APIs
directly.

```bash
pip install uv     # if uv is not installed
uv sync --frozen
uv run --with pytest pytest -q    # 120 tests, offline
```

### Refresh / changes

Every run writes an immutable snapshot to `state/snapshots/<run-id>/envelopes.jsonl` and appends to
`state/index.json`. The next run diffs each company's claims against the latest prior snapshot. You can
also name a snapshot explicitly with `--previous <envelopes.jsonl>`. Material differences appear in each
envelope's `changes` (`added`, `removed`, `value_changed`, `availability_changed`) with old and new
values and the new evidence ids. Prior snapshots are never overwritten. A rerun on unchanged sources
produces zero changes (idempotent). Use `--state-dir` to keep separate histories.

## Runtime

* **Measured** on the live 100-company smoke test: 213 s wall time (about 3.6 min), 562 requests, $0.
  See SMOKE_TEST_REPORT.md.
* **Estimated** for 1,000 companies at the defaults: about 35 min, bound by the polite filing-years
  throttle (1,000 × 2.1 s).
* With `--no-filing-years`, runtime is bound by Brreg latency and website fetches instead. The
  `--time-budget-minutes` / `--hard-deadline-minutes` guards make sure every input still gets an
  envelope.

## Models / APIs / cost

* **Models:** none. No LLM and no ML model is called. The summary is deterministic and built only from
  the cited claims.
* **APIs (all public, no key, NLOD-licensed open data):**
  * Enhetsregisteret: `/enhetsregisteret/api/enheter/{org}`, `/enheter/{org}/roller`,
    `/underenheter?overordnetEnhet={org}`, and `/konsernstruktur/{org}` (fetched only when the
    registry flags `erIKonsern=true`).
  * Regnskapsregisteret: `/regnskapsregisteret/regnskap/{org}` (all returned periods) and
    `/regnskap/aarsregnskap/kopi/{org}/aar` (filing years, throttled to ≤ 1 request per 2.1 s).
* **Company websites:** only the registry-listed `hjemmeside`. If none is listed, the agent tries the
  domain of the registry e-mail address, excluding free and ISP mail providers and sole proprietorships.
  It reads the homepage plus at most 3 same-domain identity pages and 1 careers page, with a 10 s
  timeout and no JavaScript rendering. robots.txt is honoured and cached per origin, including for every
  redirect target, which is checked *before* it is requested. A robots.txt that cannot be read (timeout,
  network error, 5xx) is treated as **not** allowed, and the site is reported as `blocked`. Only a 4xx
  (no robots file) counts as allowed.
* **Not used:** search engines, LinkedIn, Meta, Indeed, Google, or any source whose terms restrict
  automated access.
* **Third-party cost:** $0 per run. The expected cost of an official 1,000-company batch is **$0**.

## Politeness

* At most 4 concurrent requests to data.brreg.no.
* Exponential backoff with jitter on 429, 5xx and network errors, honouring `Retry-After`.
* A per-run URL cache, so no request is repeated within a run.
* Brreg's deterministic "unsupported statement layout" 500 (banks and insurers) is classified, not retried.

## Output contract

The envelope keys are `organisation_number, run, claims, evidence, changes, errors, operations` (from
OUTPUT_CONTRACT.md), plus `agent` and `sections`, which give a per-section availability overview.

* `run.terminal_status` takes one of three values:
  * `completed`: all modules reached a definitive state.
  * `partial`: some module failed after retries; see `errors`.
  * `failed`: invalid input, entity lookup failed, or the hard deadline was hit.
* Each claim has `field, section, value, availability, confidence, evidence_ids` and an optional `note`.
  `availability` is one of `available | not_available | blocked | not_applicable | ambiguous | failed`.
* Each evidence item has `id, source_url, source_class, retrieved_at, content_sha256` (SHA-256 of the
  exact response bytes) and `http_status`. JSON sources also carry `json_pointer` and `claim_span`, the
  exact key and value at that pointer. Web sources carry `claim_span` as the exact text excerpt
  containing the org number or legal name.
* `source_class` takes one of four values:
  * `official_registry`
  * `official_annual_accounts`
  * `company_owned` (verified website pages)
  * `official_registry_bulk` (fallback only)

## Precision rules implemented

1. **Accounts.** Financial values are copied from Regnskapsregisteret, never estimated:
   * Periods are sorted, and the latest *company* (`SELSKAP`) period is used for headline metrics.
     Group (`KONSERN`) accounts are reported separately as `group_consolidated_financials`. The kit's
     baseline took the first 3 records in API order, which can mix group and company figures.
   * A metric missing from the response is not published at all. It is never set to zero.
   * Records whose embedded organisation number differs from the input are dropped and logged.
   * Amounts are parsed as exact decimals.
2. **Employees.** A missing employee count is `not_available`, never 0.
3. **Leadership.**
   * Only active (non-deregistered, non-resigned) roles returned by `/roller` are published.
     Birth dates are discarded.
   * For AS/ASA, a missing CEO or chair is reported as `not_available` ("source checked; no such role").
   * Sole-proprietor contact details are not published.
4. **Workplaces.** The registered sub-unit count is exact (`page.totalElements`), and the list holds up
   to 200 rows. Sub-units whose parent is a different entity are dropped.
5. **Website.** `registry_website` is what the registry lists (an official fact). `official_website`
   is a separate ownership claim:
   * `available` when the exact org number (0.97) or exact legal name (0.90) is visible on the site.
     Name matching tolerates only whitespace, `&`/`og`, and AS vs A/S spelling.
   * `ambiguous` (with evidence kept) when it cannot be verified, when the site shows a *different*
     organisation number (a parent, brand or franchise site), or when the page is parked or for sale.
   * `failed` or `blocked` when the site is unreachable or disallowed by robots.txt.
6. **Hiring / public activity.** These come only from a verified official website: a careers page on
   the same registered domain, and social profiles linked from the site whose handle matches the legal
   name. Otherwise the field is `not_available`, with the reason given.
7. **Summary.** The deterministic summary cites the evidence of every claim it uses.

## Files added or changed vs the starter kit

| File | Purpose |
|---|---|
| `scripts/run_signalpost.py` | the single evaluator command |
| `src/norway_company_agent/signalpost.py` | per-company research, claims, evidence, changes |
| `src/norway_company_agent/brreg_client.py` | polite cached Brreg client |
| `src/norway_company_agent/site_verify.py` | website ownership verification, careers page, e-mail-domain candidate |
| `src/norway_company_agent/website.py` | (changed) allows fake-IP DNS only behind an explicit egress proxy; SSRF guard otherwise unchanged |
| `scripts/spot_check_claims.py` | re-verifies sampled claims against live sources |
| `tests/test_signalpost.py` | 15 regression tests for the precision and robots rules |
| `smoke/` | the 100-company smoke batch and its run outputs |
| `SMOKE_TEST_REPORT.md` | smoke-test results |

The kit's original `scripts/run_competition_batch.py` and all of its tests are kept unchanged and still
pass.
