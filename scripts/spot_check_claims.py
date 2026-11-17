#!/usr/bin/env python3
"""Precision spot-check: sample published claims and re-verify them against the *live* source.

For each sampled claim, every cited evidence item is re-fetched (Brreg JSON or the company web page):
  * JSON evidence: the value at ``json_pointer`` in the live response is re-rendered and compared with the
    stored ``claim_span``; the entity/accounts organisation number in the live response must be the input.
  * Web evidence: the stored ``claim_span`` text (minus ellipses) must appear in the live page text.
The claim value, the stored span and the live value are printed side by side for manual review.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs4 import BeautifulSoup  # noqa: E402

from norway_company_agent.brreg_client import BrregClient  # noqa: E402
from norway_company_agent.signalpost import _span  # noqa: E402
from norway_company_agent.site_verify import SiteStats, fetch_page, visible_text  # noqa: E402

SKIP_FIELDS = {"company_summary"}


def resolve(body, pointer: str):
    if pointer in ("", "/"):
        return body
    value = body
    for raw in pointer.lstrip("/").split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(key)]
        else:
            value = value[key]
    return value


def check_json(client: BrregClient, ev: dict, org: str) -> tuple[bool, str]:
    live = client.get_json(ev["source_url"])
    if not live.ok:
        return False, f"live fetch failed: {live.error}"
    pointer = ev.get("json_pointer")
    if pointer is None:
        return True, "response-level evidence (no pointer)"
    try:
        value = resolve(live.body, pointer)
    except (KeyError, IndexError, ValueError, TypeError):
        return False, f"pointer {pointer} absent in live response"
    attribution = None
    if isinstance(live.body, dict) and "organisasjonsnummer" in live.body and "enheter/" in ev["source_url"] and "underenheter" not in ev["source_url"]:
        attribution = live.body.get("organisasjonsnummer") == org
    if isinstance(live.body, list) and pointer.split("/")[1].isdigit():
        record = live.body[int(pointer.split("/")[1])]
        attribution = (record.get("virksomhet") or {}).get("organisasjonsnummer") == org
    span = ev.get("claim_span")
    key = pointer.rstrip("/").split("/")[-1]
    if span and span.startswith('"rolle"'):
        rendered = None  # role spans are composites; compare the name/role inside the live role object
        person = value.get("person") or {}
        name = " ".join(p for p in ((person.get("navn") or {}).get(k) for k in ("fornavn", "mellomnavn", "etternavn")) if p) or " ".join((value.get("enhet") or {}).get("navn") or [])
        ok = json.loads(span.split(": ", 1)[1]).get("name") == name
        return ok and attribution is not False, f"live role holder: {name}"
    if span and (span.startswith('"underenhet"') or span.startswith('"child"')):
        stored = json.loads(span.split(": ", 1)[1])
        ok = stored.get("organisasjonsnummer") == value.get("organisasjonsnummer") and stored.get("navn") == value.get("navn")
        return ok, f"live: {value.get('organisasjonsnummer')} {value.get('navn')}"
    rendered = _span(key if key else "aar", value)
    ok = (rendered == span) if span else True
    return ok and attribution is not False, f"live {rendered[:160]}" + ("" if attribution is None else f" | attributed_to_input={attribution}")


def check_web(ev: dict) -> tuple[bool, str]:
    span = (ev.get("claim_span") or "").strip("…").strip()
    page = fetch_page(ev["source_url"], timeout=15, max_bytes=2_000_000, stats=SiteStats())
    if page.html is None:
        return False, f"live fetch failed: {page.error}"
    if not span:
        return True, "page reachable (no span)"
    text = visible_text(BeautifulSoup(page.html, "lxml"))
    title = ""
    soup = BeautifulSoup(page.html, "lxml")
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
    meta = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
    haystack = re.sub(r"\s+", " ", " ".join([text, title, str(meta.get("content") or "") if meta else ""]))
    probe = re.sub(r"\s+", " ", span.removeprefix("link text: "))[:120]
    return probe in haystack, f"span {'found' if probe in haystack else 'NOT found'} in live page"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    envelopes = [json.loads(line) for line in Path(args.envelopes).read_text(encoding="utf-8").splitlines() if line.strip()]
    pool = [(e, c) for e in envelopes for c in e["claims"] if c["availability"] == "available" and c["field"] not in SKIP_FIELDS]
    rng = random.Random(args.seed)
    # Stratify: always include every website/hiring/social claim, then a random sample of the rest.
    web = [item for item in pool if item[1]["section"] in ("website", "hiring", "public_activity", "profile") and item[1]["field"] != "registry_website" and item[1]["field"] not in ("statutory_purpose", "registered_activity")]
    rest = [item for item in pool if item not in web]
    sample = web + rng.sample(rest, max(0, args.sample - len(web)))
    client = BrregClient(max_in_flight=2)
    results = []
    for envelope, claim in sample:
        org = envelope["organisation_number"]
        evidence = {ev["id"]: ev for ev in envelope["evidence"]}
        checks = []
        for ev_id in claim["evidence_ids"]:
            ev = evidence[ev_id]
            ok, detail = check_json(client, ev, org) if "data.brreg.no" in ev["source_url"] else check_web(ev)
            checks.append({"evidence_id": ev_id, "source_url": ev["source_url"], "json_pointer": ev.get("json_pointer"), "stored_span": ev.get("claim_span"), "ok": ok, "detail": detail})
        results.append({"organisation_number": org, "field": claim["field"], "section": claim["section"], "value": claim["value"], "confidence": claim["confidence"],
                        "all_evidence_reconfirmed": all(c["ok"] for c in checks), "checks": checks})
    summary = {"sampled_claims": len(results), "reconfirmed": sum(r["all_evidence_reconfirmed"] for r in results),
               "by_section": {}, "seed": args.seed, "envelopes": args.envelopes}
    for r in results:
        bucket = summary["by_section"].setdefault(r["section"], {"sampled": 0, "reconfirmed": 0})
        bucket["sampled"] += 1
        bucket["reconfirmed"] += int(r["all_evidence_reconfirmed"])
    Path(args.output).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
