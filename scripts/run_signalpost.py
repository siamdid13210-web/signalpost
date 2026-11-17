#!/usr/bin/env python3
"""Kinpaku Signalpost Agent — single evaluator command.

    uv run python scripts/run_signalpost.py --organisations batch.jsonl --output out/envelopes.jsonl

Accepts JSONL / JSON / plain-text organisation-number lists (the kit's batch formats) and/or numbers as
positional arguments. Writes exactly one terminal envelope per unique input to the JSONL output (input
order), one ``<orgnr>.json`` per company in ``--envelope-dir``, a machine-readable run report, and an
immutable snapshot under ``--state-dir`` used to compute ``changes`` on the next run.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.brreg_client import BrregClient, utc_now  # noqa: E402
from norway_company_agent.signalpost import AGENT_NAME, AGENT_VERSION, RunContext, envelope_digest, failed_envelope, research_company  # noqa: E402

CONTRACT_KEYS = ("organisation_number", "run", "claims", "evidence", "changes", "errors", "operations")
ALLOWED_AVAILABILITY = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}


def read_inputs(paths: list[str], positional: list[str]) -> tuple[list[str], list[str]]:
    raw: list[Any] = []
    for path in paths:
        source = Path(path)
        text = source.read_text(encoding="utf-8-sig")
        if source.suffix == ".json":
            body = json.loads(text)
            raw.extend(body if isinstance(body, list) else body.get("organisation_numbers", []))
        elif source.suffix == ".jsonl":
            raw.extend(json.loads(line) for line in text.splitlines() if line.strip())
        else:
            raw.extend(line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    raw.extend(positional)
    orgs: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for value in raw:
        org = value.get("organisation_number") if isinstance(value, dict) else value
        digits = "".join(ch for ch in str(org or "") if ch.isdigit()) or str(org or "").strip()
        if digits in seen:
            duplicates.append(digits)
            continue
        seen.add(digits)
        orgs.append(digits)
    return orgs, duplicates


def load_bulk(path: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
    source = Path(path)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    opener = gzip.open if source.suffix == ".gz" or source.read_bytes()[:2] == b"\x1f\x8b" else open
    rows: dict[str, dict[str, Any]] = {}
    retrieved = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with opener(source, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            org = row.get("organisasjonsnummer")
            if org in wanted:
                rows[org] = {**row, "_snapshot_sha256": sha, "_retrieved_at": retrieved}
                if len(rows) == len(wanted):
                    break
    return rows


def load_previous(state_dir: Path, explicit: str | None, run_id: str) -> tuple[dict[str, dict[str, Any]], str | None]:
    path: Path | None = None
    if explicit:
        path = Path(explicit)
    else:
        index = state_dir / "index.json"
        if index.exists():
            runs = [item for item in json.loads(index.read_text(encoding="utf-8")).get("runs", []) if item.get("run_id") != run_id]
            if runs:
                path = state_dir / runs[-1]["path"]
    if not path or not path.exists():
        return {}, None
    previous: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            if item.get("run", {}).get("terminal_status") != "failed":
                previous[item["organisation_number"]] = item
    return previous, str(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def validate(envelopes: list[dict[str, Any]], orgs: list[str]) -> dict[str, Any]:
    problems = []
    for envelope in envelopes:
        missing = [key for key in CONTRACT_KEYS if key not in envelope]
        if missing:
            problems.append({"organisation_number": envelope.get("organisation_number"), "missing_keys": missing})
        ids = {ev["id"] for ev in envelope.get("evidence", [])}
        for claim in envelope.get("claims", []):
            if claim.get("availability") not in ALLOWED_AVAILABILITY:
                problems.append({"organisation_number": envelope["organisation_number"], "field": claim.get("field"), "problem": "bad availability"})
            if not set(claim.get("evidence_ids", [])) <= ids:
                problems.append({"organisation_number": envelope["organisation_number"], "field": claim.get("field"), "problem": "dangling evidence id"})
            if claim.get("availability") == "available" and not claim.get("evidence_ids"):
                problems.append({"organisation_number": envelope["organisation_number"], "field": claim.get("field"), "problem": "available claim without evidence"})
        for ev in envelope.get("evidence", []):
            if not (ev.get("source_url") and ev.get("source_class") and ev.get("retrieved_at")):
                problems.append({"organisation_number": envelope["organisation_number"], "evidence": ev.get("id"), "problem": "incomplete evidence"})
    emitted = [envelope["organisation_number"] for envelope in envelopes]
    checks = {
        "one_envelope_per_input": emitted == orgs,
        "unique_organisation_numbers": len(emitted) == len(set(emitted)),
        "all_terminal": all(envelope["run"]["terminal_status"] in {"completed", "partial", "failed"} for envelope in envelopes),
        "contract_shape_and_evidence_links": not problems,
    }
    return {"passed": all(checks.values()), "checks": checks, "problems": problems[:50]}


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{AGENT_NAME} — one terminal envelope per organisation number")
    parser.add_argument("orgnrs", nargs="*", help="Organisation numbers (optional, in addition to --organisations)")
    parser.add_argument("--organisations", action="append", default=[], help="JSONL/JSON/text list of organisation numbers (repeatable)")
    parser.add_argument("--output", default="out/envelopes.jsonl", help="Terminal envelopes JSONL, input order")
    parser.add_argument("--envelope-dir", default=None, help="Directory for one <orgnr>.json per company (default: <output stem>/ next to output)")
    parser.add_argument("--report", default=None, help="Run report JSON (default: <output stem>.report.json)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--state-dir", default="state", help="Immutable snapshot store used for change detection")
    parser.add_argument("--previous", default=None, help="Explicit previous envelopes JSONL to diff against (default: latest snapshot in --state-dir)")
    parser.add_argument("--bulk", default=None, help="Optional Brreg bulk CSV(.gz) used only as fallback if the live entity API fails")
    parser.add_argument("--expected-count", type=int, default=None, help="Fail loudly if the input count differs")
    parser.add_argument("--workers", type=int, default=8, help="Companies researched in parallel")
    parser.add_argument("--brreg-concurrency", type=int, default=4, help="Max simultaneous requests to data.brreg.no")
    parser.add_argument("--time-budget-minutes", type=float, default=50.0, help="After this, optional modules (website, filing years) are skipped")
    parser.add_argument("--hard-deadline-minutes", type=float, default=None, help="Emit failed envelopes for unfinished companies and stop (default: budget + 10)")
    parser.add_argument("--no-website", action="store_true")
    parser.add_argument("--no-filing-years", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse envelopes from an interrupted run with the same --output and --run-id")
    args = parser.parse_args()

    wall_start = time.monotonic()
    started_at = utc_now()
    run_id = args.run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    orgs, duplicates = read_inputs(args.organisations, args.orgnrs)
    if not orgs:
        raise SystemExit("No organisation numbers supplied")
    if args.expected_count is not None and len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)} unique")

    output = Path(args.output)
    envelope_dir = Path(args.envelope_dir) if args.envelope_dir else output.with_suffix("")
    report_path = Path(args.report) if args.report else output.with_suffix(".report.json")
    partial_path = output.with_suffix(".partial.jsonl")
    state_dir = Path(args.state_dir)
    previous, previous_path = load_previous(state_dir, args.previous, run_id)
    bulk_rows = load_bulk(args.bulk, set(orgs)) if args.bulk else {}

    client = BrregClient(max_in_flight=args.brreg_concurrency)
    budget_s = args.time_budget_minutes * 60
    hard_s = (args.hard_deadline_minutes * 60) if args.hard_deadline_minutes else budget_s + 600
    ctx = RunContext(run_id=run_id, client=client, deadline=wall_start + budget_s, previous=previous, bulk_rows=bulk_rows,
                     with_website=not args.no_website, with_filing_years=not args.no_filing_years)

    done: dict[str, dict[str, Any]] = {}
    if args.resume and partial_path.exists():
        for line in partial_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("run", {}).get("run_id") == run_id and item["organisation_number"] in orgs and item["run"]["terminal_status"] != "failed":
                    done[item["organisation_number"]] = item
    resumed = len(done)
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_lock = threading.Lock()
    partial_handle = partial_path.open("a" if args.resume else "w", encoding="utf-8")

    def job(org: str) -> dict[str, Any]:
        org_started = utc_now()
        try:
            return research_company(org, ctx)
        except Exception as exc:  # never lose an input: convert to a terminal failed envelope
            return failed_envelope(org, run_id, f"unhandled {type(exc).__name__}: {str(exc)[:200]}", org_started)

    pending = [org for org in orgs if org not in done]
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(job, org): org for org in pending}
    remaining = set(futures)
    progress_every = max(1, len(pending) // 20)
    while remaining:
        timeout = max(0.0, hard_s - (time.monotonic() - wall_start))
        finished, remaining = wait(remaining, timeout=timeout, return_when=FIRST_COMPLETED)
        if not finished:
            break
        for future in finished:
            envelope = future.result()
            done[envelope["organisation_number"]] = envelope
            with partial_lock:
                partial_handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
                partial_handle.flush()
            if len(done) % progress_every == 0:
                print(f"[{len(done)}/{len(orgs)}] {time.monotonic() - wall_start:.0f}s", file=sys.stderr, flush=True)
    hard_stopped = bool(remaining)
    for future in remaining:
        org = futures[future]
        done[org] = failed_envelope(org, run_id, "hard deadline reached before this company finished (budget_exhausted)", started_at)
    partial_handle.close()
    pool.shutdown(wait=not hard_stopped, cancel_futures=True)

    completed_at = utc_now()
    envelopes = [done[org] for org in orgs]
    write_jsonl(output, envelopes)
    envelope_dir.mkdir(parents=True, exist_ok=True)
    for envelope in envelopes:
        (envelope_dir / f"{envelope['organisation_number']}.json").write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Immutable snapshot (never overwrite a previous run's snapshot).
    snapshot_rel = Path("snapshots") / run_id / "envelopes.jsonl"
    snapshot_path = state_dir / snapshot_rel
    if snapshot_path.exists():
        snapshot_rel = Path("snapshots") / f"{run_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}" / "envelopes.jsonl"
        snapshot_path = state_dir / snapshot_rel
    write_jsonl(snapshot_path, envelopes)
    index_path = state_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"runs": []}
    index["runs"].append({"run_id": run_id, "completed_at": completed_at, "path": str(snapshot_rel), "count": len(envelopes),
                          "claims_digest": hashlib.sha256("".join(envelope_digest(e) for e in envelopes).encode()).hexdigest()})
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    runtimes = [e["operations"]["runtime_ms"] for e in envelopes if e["operations"]["runtime_ms"]]
    statuses = Counter(e["run"]["terminal_status"] for e in envelopes)
    section_av: dict[str, Counter] = {}
    for envelope in envelopes:
        for name, section in envelope.get("sections", {}).items():
            section_av.setdefault(name, Counter())[section["availability"]] += 1
    field_av: dict[str, Counter] = {}
    for envelope in envelopes:
        for claim in envelope["claims"]:
            field_av.setdefault(claim["field"], Counter())[claim["availability"]] += 1
    latencies = sorted(client.stats.latencies_ms)
    validation = validate(envelopes, orgs)
    report = {
        "agent": {"name": AGENT_NAME, "version": AGENT_VERSION},
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "wall_runtime_s": round(time.monotonic() - wall_start, 1),
        "inputs": {"unique": len(orgs), "duplicates_ignored": duplicates},
        "emitted_envelopes": len(envelopes),
        "resumed_envelopes": resumed,
        "hard_deadline_hit": hard_stopped,
        "terminal_status_counts": dict(statuses),
        "per_company_runtime_ms": {
            "mean": round(statistics.mean(runtimes)) if runtimes else None,
            "p50": sorted(runtimes)[len(runtimes) // 2] if runtimes else None,
            "p95": sorted(runtimes)[min(len(runtimes) - 1, int(len(runtimes) * 0.95))] if runtimes else None,
            "max": max(runtimes) if runtimes else None,
        },
        "operations": {
            "network_requests_total": sum(e["operations"]["requests"] for e in envelopes),
            "brreg_requests": client.stats.network_requests,
            "brreg_cache_hits": client.stats.cache_hits,
            "brreg_p50_ms": latencies[len(latencies) // 2] if latencies else None,
            "brreg_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
            "third_party_cost_usd": 0,
            "paid_apis": [],
            "models": [],
        },
        "claims_total": sum(len(e["claims"]) for e in envelopes),
        "claims_available": sum(1 for e in envelopes for c in e["claims"] if c["availability"] == "available"),
        "changes_total": sum(len(e["changes"]) for e in envelopes),
        "previous_snapshot": previous_path,
        "snapshot": str(snapshot_path),
        "section_availability": {name: dict(counter) for name, counter in sorted(section_av.items())},
        "field_availability": {name: dict(counter) for name, counter in sorted(field_av.items())},
        "validation": validation,
        "outputs": {"envelopes_jsonl": str(output), "envelope_dir": str(envelope_dir)},
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    headline = {k: report[k] for k in ("run_id", "emitted_envelopes", "terminal_status_counts", "wall_runtime_s", "per_company_runtime_ms", "claims_total", "claims_available", "changes_total")}
    headline["validation_passed"] = validation["passed"]
    print(json.dumps(headline, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    if hard_stopped:
        os._exit(0 if validation["passed"] else 1)
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
