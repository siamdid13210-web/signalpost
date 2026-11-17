"""Kinpaku Signalpost Agent — precision-first company research for Norwegian organisation numbers.

One call to :func:`research_company` returns exactly one terminal envelope (OUTPUT_CONTRACT.md):
``organisation_number, run, claims, evidence, changes, errors, operations``.

Source backbone (no keys, no paid APIs):
  * Enhetsregisteret  /enheter/{org}, /enheter/{org}/roller, /underenheter?overordnetEnhet={org},
    /konsernstruktur/{org} (only when the entity is flagged as part of a group)
  * Regnskapsregisteret /regnskap/{org} (all returned periods) and /regnskap/aarsregnskap/kopi/{org}/aar
  * the registry-listed website, verified by exact organisation number / exact legal name.

Rules: every claim carries claim-level evidence (URL, source class, retrieval time, SHA-256 of the exact
response bytes, JSON pointer and the exact span). Values are copied, never estimated; a missing value is
an explicit availability state, never zero.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .brreg_client import BrregClient, RequestStats, Response, utc_now
from .site_verify import email_domain_candidate, verify_registry_website

AGENT_NAME = "Kinpaku Signalpost Agent"
AGENT_VERSION = "1.0.0"

ENTITY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
SUBUNITS_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size={size}&page={page}"
GROUP_URL = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
FILING_YEARS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
FILING_COPY_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
BULK_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"

SC_REGISTRY = "official_registry"
SC_ACCOUNTS = "official_annual_accounts"
SC_COMPANY = "company_owned"
SC_DERIVED = "derived_from_cited_evidence"

CONF_OFFICIAL = 0.99
CONF_DERIVED = 0.97

FINANCIAL_METRICS: dict[str, tuple[str, ...]] = {
    "revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
    "sales_revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "salgsinntekter"),
    "operating_expenses": ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"),
    "payroll_expenses": ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "loennskostnad"),
    "operating_result": ("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
    "net_financial_items": ("resultatregnskapResultat", "finansresultat", "nettoFinans"),
    "profit_before_tax": ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
    "net_result": ("resultatregnskapResultat", "aarsresultat"),
    "total_assets": ("eiendeler", "sumEiendeler"),
    "fixed_assets": ("eiendeler", "anleggsmidler", "sumAnleggsmidler"),
    "current_assets": ("eiendeler", "omloepsmidler", "sumOmloepsmidler"),
    "cash_and_bank_deposits": ("eiendeler", "sumBankinnskuddOgKontanter"),
    "equity": ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
    "total_debt": ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
    "long_term_debt": ("egenkapitalGjeld", "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld"),
    "short_term_debt": ("egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld"),
}
HISTORY_METRICS = ("revenue", "operating_result", "profit_before_tax", "net_result", "total_assets", "equity", "total_debt")

ROLE_FIELDS = {
    "DAGL": "chief_executive",
    "LEDE": "board_chair",
    "NEST": "board_deputy_chair",
    "MEDL": "board_members",
    "VARA": "board_deputy_members",
    "OBS": "board_observers",
    "REVI": "auditor",
    "REGN": "accountant",
    "INNH": "proprietor",
    "DTPR": "general_partners",
    "DTSO": "general_partners",
    "KOMP": "general_partners",
    "KONT": "contact_person",
    "FFØR": "business_manager",
    "REPR": "norwegian_representative",
}
SINGLE_ROLE_FIELDS = {"chief_executive", "board_chair", "board_deputy_chair", "auditor", "accountant", "proprietor", "contact_person", "business_manager", "norwegian_representative"}
# Sections whose absence of a role is meaningful when the roles source was checked.
EXPECTED_ROLES_BY_FORM = {"AS": ("chief_executive", "board_chair"), "ASA": ("chief_executive", "board_chair")}


# --------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------

def _get(value: Any, *path: Any) -> Any:
    for key in path:
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
        else:
            return None
    return value


def _pointer(*path: Any) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _span(key: Any, value: Any, limit: int = 400) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f'"{key}": {text}'


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def format_address(address: Any) -> str | None:
    if not isinstance(address, dict):
        return None
    lines = [str(line).strip() for line in address.get("adresse") or [] if str(line or "").strip()]
    tail = " ".join(part for part in (address.get("postnummer"), address.get("poststed")) if part)
    parts = lines + ([tail] if tail else [])
    if address.get("land") and address.get("landkode") not in (None, "NO"):
        parts.append(str(address["land"]))
    return ", ".join(parts) or None


def person_name(person: dict[str, Any]) -> str | None:
    name = person.get("navn") or {}
    return " ".join(part for part in (name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")) if part) or None


def valid_orgnr(org: str) -> bool:
    if not re.fullmatch(r"\d{9}", org or ""):
        return False
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(d) * w for d, w in zip(org[:8], weights)) % 11
    check = 0 if remainder == 0 else 11 - remainder
    return check != 10 and check == int(org[8])


# --------------------------------------------------------------------------------------------------
# Envelope builder
# --------------------------------------------------------------------------------------------------

@dataclass
class Envelope:
    org: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    _evidence_index: dict[str, str] = field(default_factory=dict)

    def cite(self, *, source_url: str, source_class: str, retrieved_at: str | None, content_sha256: str | None,
             claim_span: str | None = None, json_pointer: str | None = None, http_status: int | None = None,
             note: str | None = None) -> str:
        key = _canon([source_url, json_pointer, claim_span, content_sha256])
        if key in self._evidence_index:
            return self._evidence_index[key]
        evidence_id = f"ev-{len(self.evidence) + 1}"
        record: dict[str, Any] = {
            "id": evidence_id,
            "source_url": source_url,
            "source_class": source_class,
            "retrieved_at": retrieved_at,
            "content_sha256": content_sha256,
        }
        if http_status is not None:
            record["http_status"] = http_status
        if json_pointer is not None:
            record["json_pointer"] = json_pointer
        if claim_span is not None:
            record["claim_span"] = claim_span
        if note:
            record["note"] = note
        self.evidence.append(record)
        self._evidence_index[key] = evidence_id
        return evidence_id

    def cite_json(self, response: Response, source_class: str, path: tuple[Any, ...], *, value: Any = None) -> str:
        value = _get(response.body, *path) if value is None else value
        return self.cite(
            source_url=response.url,
            source_class=source_class,
            retrieved_at=response.retrieved_at,
            content_sha256=response.raw_sha256,
            json_pointer=_pointer(*path),
            claim_span=_span(path[-1] if path else "", value),
            http_status=response.status,
        )

    def cite_response(self, response: Response, source_class: str, note: str | None = None) -> str:
        return self.cite(source_url=response.url, source_class=source_class, retrieved_at=response.retrieved_at,
                         content_sha256=response.raw_sha256, http_status=response.status or None, note=note)

    def claim(self, section: str, field_name: str, value: Any, availability: str, confidence: float | None,
              evidence_ids: list[str], note: str | None = None, **extra: Any) -> None:
        claim = {
            "field": field_name,
            "section": section,
            "value": value,
            "availability": availability,
            "confidence": confidence,
            "evidence_ids": sorted(set(evidence_ids), key=lambda ev: int(ev.split("-")[1])),
        }
        if note:
            claim["note"] = note
        claim.update(extra)
        self.claims.append(claim)

    def error(self, module: str, response: Response | None, message: str, *, retryable: bool | None = None) -> None:
        self.errors.append({
            "module": module,
            "source_url": response.url if response else None,
            "http_status": (response.status or None) if response else None,
            "message": message,
            "retryable": bool(response.retryable) if retryable is None and response else bool(retryable),
            "at": utc_now(),
        })

    def section(self, name: str, availability: str, note: str | None = None, **extra: Any) -> None:
        self.sections[name] = {"availability": availability, **({"note": note} if note else {}), **extra}


def _availability_for_failure(response: Response) -> str:
    if response.status in (404, 410):
        return "not_available"
    if response.status in (401, 403):
        return "blocked"
    return "failed"


# --------------------------------------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------------------------------------

def build_identity(env: Envelope, entity: Response, source_class: str = SC_REGISTRY) -> dict[str, Any]:
    body = entity.body or {}
    o = SC_REGISTRY if source_class == SC_REGISTRY else source_class

    def simple(field_name: str, path: tuple[Any, ...], value: Any = None, section: str = "identity", transform: Callable[[Any], Any] | None = None) -> None:
        raw = _get(body, *path) if value is None else value
        if raw in (None, "", []):
            return
        env.claim(section, field_name, transform(raw) if transform else raw, "available", CONF_OFFICIAL, [env.cite_json(entity, o, path, value=_get(body, *path))])

    simple("organisation_number", ("organisasjonsnummer",))
    simple("legal_name", ("navn",))
    form = body.get("organisasjonsform") or {}
    if form.get("kode"):
        env.claim("identity", "organisation_form", {"code": form.get("kode"), "description": form.get("beskrivelse")}, "available", CONF_OFFICIAL,
                  [env.cite_json(entity, o, ("organisasjonsform", "kode")), env.cite_json(entity, o, ("organisasjonsform", "beskrivelse"))])
    simple("registration_date", ("registreringsdatoEnhetsregisteret",))
    simple("foundation_date", ("stiftelsesdato",))
    simple("articles_of_association_date", ("vedtektsdato",))
    if body.get("registrertIForetaksregisteret") is not None:
        ev = [env.cite_json(entity, o, ("registrertIForetaksregisteret",))]
        if body.get("registreringsdatoForetaksregisteret"):
            ev.append(env.cite_json(entity, o, ("registreringsdatoForetaksregisteret",)))
        env.claim("identity", "business_register", {"registered": bool(body["registrertIForetaksregisteret"]), "registration_date": body.get("registreringsdatoForetaksregisteret")}, "available", CONF_OFFICIAL, ev)
    if body.get("registrertIMvaregisteret") is not None:
        ev = [env.cite_json(entity, o, ("registrertIMvaregisteret",))]
        if body.get("registreringsdatoMerverdiavgiftsregisteret"):
            ev.append(env.cite_json(entity, o, ("registreringsdatoMerverdiavgiftsregisteret",)))
        env.claim("identity", "vat_register", {"registered": bool(body["registrertIMvaregisteret"]), "registration_date": body.get("registreringsdatoMerverdiavgiftsregisteret")}, "available", CONF_OFFICIAL, ev)
    for index, key in enumerate(("naeringskode1", "naeringskode2", "naeringskode3")):
        code = body.get(key) or {}
        if code.get("kode"):
            env.claim("identity", "industry_primary" if index == 0 else "industry_secondary", {"code": code.get("kode"), "description": code.get("beskrivelse"), "rank": index + 1},
                      "available", CONF_OFFICIAL, [env.cite_json(entity, o, (key,))])
    sector = body.get("institusjonellSektorkode") or {}
    if sector.get("kode"):
        env.claim("identity", "institutional_sector", {"code": sector.get("kode"), "description": sector.get("beskrivelse")}, "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("institusjonellSektorkode",))])

    # Employees: only a registered count is a fact. Absent => explicit not_available (never zero).
    employees = body.get("antallAnsatte")
    has_registered = body.get("harRegistrertAntallAnsatte")
    if isinstance(employees, int) and has_registered is not False:
        ev = [env.cite_json(entity, o, ("antallAnsatte",))]
        if body.get("registreringsdatoAntallAnsatteEnhetsregisteret"):
            ev.append(env.cite_json(entity, o, ("registreringsdatoAntallAnsatteEnhetsregisteret",)))
        env.claim("identity", "employees", {"count": employees, "registered_at": body.get("registreringsdatoAntallAnsatteEnhetsregisteret"), "basis": "registered employee count (Enhetsregisteret/Aa-registeret)"}, "available", CONF_OFFICIAL, ev)
    else:
        ev = [env.cite_json(entity, o, ("harRegistrertAntallAnsatte",))] if has_registered is not None else [env.cite_response(entity, o)]
        env.claim("identity", "employees", None, "not_available", None, ev, note="The registry returns no registered employee count for this entity (not the same as zero).")

    for field_name, key in (("business_address", "forretningsadresse"), ("postal_address", "postadresse")):
        address = body.get(key)
        if isinstance(address, dict) and address:
            env.claim("identity", field_name, {"formatted": format_address(address), **{k: address.get(k) for k in ("adresse", "postnummer", "poststed", "kommune", "kommunenummer", "land", "landkode")}},
                      "available", CONF_OFFICIAL, [env.cite_json(entity, o, (key,))])
    business = body.get("forretningsadresse") or body.get("beliggenhetsadresse") or {}
    if business.get("kommune"):
        key = "forretningsadresse" if body.get("forretningsadresse") else "beliggenhetsadresse"
        env.claim("identity", "municipality", {"name": business.get("kommune"), "number": business.get("kommunenummer")}, "available", CONF_OFFICIAL,
                  [env.cite_json(entity, o, (key, "kommune")), env.cite_json(entity, o, (key, "kommunenummer"))])

    # Status flags — each flag cited individually; the rolled-up status is derived only from them.
    flags = {
        "bankrupt": ("konkurs", "konkursdato"),
        "under_liquidation": ("underAvvikling", "underAvviklingDato"),
        "under_forced_liquidation_or_dissolution": ("underTvangsavviklingEllerTvangsopplosning", "tvangsavvikletPgaManglendeSlettingDato"),
    }
    status_ev: list[str] = []
    status_value: dict[str, Any] = {}
    for name, (flag_key, date_key) in flags.items():
        if flag_key in body:
            ev_id = env.cite_json(entity, o, (flag_key,))
            status_ev.append(ev_id)
            status_value[name] = bool(body[flag_key])
            date_value = body.get(date_key)
            ev = [ev_id] + ([env.cite_json(entity, o, (date_key,))] if date_value else [])
            env.claim("status", name, {"value": bool(body[flag_key]), "date": date_value}, "available", CONF_OFFICIAL, ev)
    if body.get("slettedato"):
        status_ev.append(env.cite_json(entity, o, ("slettedato",)))
        status_value["deleted_date"] = body["slettedato"]
    if status_value:
        label = "deleted" if status_value.get("deleted_date") else "bankrupt" if status_value.get("bankrupt") else "under_forced_liquidation_or_dissolution" if status_value.get("under_forced_liquidation_or_dissolution") else "under_liquidation" if status_value.get("under_liquidation") else "active"
        env.claim("status", "registry_status", {"status": label, **status_value}, "available", CONF_DERIVED, status_ev,
                  note="Derived only from the cited registry flags.")

    capital = body.get("kapital")
    if isinstance(capital, dict) and capital.get("belop") is not None:
        env.claim("identity", "share_capital", {"amount": capital.get("belop"), "currency": capital.get("valuta"), "type": capital.get("type"), "shares": capital.get("antallAksjer"), "fully_paid": capital.get("fulltInnbetalt"), "registered_at": capital.get("innfortDato")},
                  "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("kapital",))])
    for field_name, key in (("statutory_purpose", "vedtektsfestetFormaal"), ("registered_activity", "aktivitet")):
        lines = body.get(key)
        if isinstance(lines, list) and any(lines):
            env.claim("profile", field_name, " ".join(str(line).strip() for line in lines if line), "available", CONF_OFFICIAL, [env.cite_json(entity, o, (key,))])
    history = body.get("historiskeNavn")
    if isinstance(history, list) and history:
        env.claim("identity", "former_names", [{"name": item.get("navn"), "from": item.get("fraDato"), "to": item.get("tilDato")} for item in history], "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("historiskeNavn",))])
    if body.get("erIKonsern") is not None:
        env.claim("group", "part_of_group", bool(body["erIKonsern"]), "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("erIKonsern",))])
    if body.get("overordnetEnhet"):
        env.claim("group", "parent_unit_in_registry", body["overordnetEnhet"], "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("overordnetEnhet",))])
    if body.get("sisteInnsendteAarsregnskap"):
        env.claim("financials", "latest_submitted_annual_accounts_year", str(body["sisteInnsendteAarsregnskap"]), "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("sisteInnsendteAarsregnskap",))])
    if body.get("hjemmeside"):
        env.claim("website", "registry_website", body["hjemmeside"], "available", CONF_OFFICIAL, [env.cite_json(entity, o, ("hjemmeside",))],
                  note="Website as listed in Enhetsregisteret; ownership verification is reported separately in official_website.")
    form_code = form.get("kode")
    if form_code != "ENK":  # sole-proprietor contact details are personal data; keep them out.
        simple("contact_email", ("epostadresse",), section="contact")
        simple("contact_phone", ("telefon",), section="contact")
        simple("contact_mobile", ("mobil",), section="contact")
    env.section("identity", "available")
    return body


def build_roles(env: Envelope, response: Response, legal_form: str | None) -> list[dict[str, Any]]:
    if not response.ok:
        availability = _availability_for_failure(response)
        ev = [env.cite_response(response, SC_REGISTRY)]
        env.claim("leadership", "roles", None, availability, None, ev, note="Roles endpoint returned no data." if availability == "not_available" else response.error)
        env.section("leadership", availability)
        if availability == "failed":
            env.error("roles", response, response.error or "roles fetch failed")
        return []
    collected: dict[str, list[tuple[dict[str, Any], str]]] = {}
    all_roles: list[dict[str, Any]] = []
    all_ev: list[str] = []
    for gi, group in enumerate(response.body.get("rollegrupper") or []):
        for ri, role in enumerate(group.get("roller") or []):
            if role.get("avregistrert"):
                continue
            person = role.get("person") or {}
            unit = role.get("enhet") or {}
            if person:
                holder = {"type": "person", "name": person_name(person)}
                if person.get("erDoed"):
                    holder["deceased"] = True
            else:
                names = unit.get("navn") or []
                holder = {"type": "organisation", "name": " ".join(names) if isinstance(names, list) else names, "organisation_number": unit.get("organisasjonsnummer")}
                if unit.get("erSlettet"):
                    holder["deleted"] = True
            code = _get(role, "type", "kode")
            entry = {**holder, "role_code": code, "role": _get(role, "type", "beskrivelse"), "group": _get(group, "type", "beskrivelse"), "group_last_changed": group.get("sistEndret")}
            if role.get("fratraadt"):
                entry["resigned"] = True
            if _get(role, "valgtAv", "beskrivelse"):
                entry["elected_by"] = _get(role, "valgtAv", "beskrivelse")
            path = ("rollegrupper", gi, "roller", ri)
            span_value = {"role": entry["role"], "name": entry["name"]}
            ev_id = env.cite(source_url=response.url, source_class=SC_REGISTRY, retrieved_at=response.retrieved_at, content_sha256=response.raw_sha256,
                             json_pointer=_pointer(*path), claim_span=_span("rolle", span_value), http_status=response.status)
            all_roles.append(entry)
            all_ev.append(ev_id)
            if entry.get("resigned"):
                continue
            target = ROLE_FIELDS.get(code or "")
            if target:
                collected.setdefault(target, []).append((entry, ev_id))
    for target, items in collected.items():
        values = [{k: v for k, v in item.items() if k not in ("group", "group_last_changed")} for item, _ in items]
        value: Any = values[0] if target in SINGLE_ROLE_FIELDS and len(values) == 1 else values
        env.claim("leadership", target, value, "available", CONF_OFFICIAL, [ev for _, ev in items])
    for expected in EXPECTED_ROLES_BY_FORM.get(legal_form or "", ()):
        if expected not in collected:
            env.claim("leadership", expected, None, "not_available", None, [env.cite_response(response, SC_REGISTRY)], note="Roles source checked; no such active role is registered.")
    if all_roles:
        env.claim("leadership", "registered_roles", all_roles, "available", CONF_OFFICIAL, all_ev)
        env.section("leadership", "available", role_count=len(all_roles))
    else:
        env.claim("leadership", "registered_roles", [], "available", CONF_OFFICIAL, [env.cite_response(response, SC_REGISTRY)], note="Roles source checked; zero active roles returned.")
        env.section("leadership", "available", role_count=0)
    return all_roles


def build_workplaces(env: Envelope, pages: list[Response], total: int | None, *, cap: int) -> list[dict[str, Any]]:
    if not pages or not pages[0].ok:
        response = pages[0] if pages else None
        if response is None:
            return []
        availability = _availability_for_failure(response)
        env.claim("workplaces", "registered_workplaces", None, availability, None, [env.cite_response(response, SC_REGISTRY)], note=response.error)
        env.section("workplaces", availability)
        if availability == "failed":
            env.error("workplaces", response, response.error or "subunit fetch failed")
        return []
    first = pages[0]
    count_ev = env.cite_json(first, SC_REGISTRY, ("page", "totalElements")) if _get(first.body, "page", "totalElements") is not None else env.cite_response(first, SC_REGISTRY)
    count = total if total is not None else 0
    env.claim("workplaces", "registered_workplace_count", count, "available", CONF_OFFICIAL, [count_ev],
              note="Registered sub-units (underenheter) in Enhetsregisteret." + (" Zero means the registry was checked and returned none." if count == 0 else ""))
    rows: list[dict[str, Any]] = []
    evs: list[str] = []
    for response in pages:
        for index, unit in enumerate(_get(response.body, "_embedded", "underenheter") or []):
            if len(rows) >= cap:
                break
            address = unit.get("beliggenhetsadresse") or unit.get("postadresse") or {}
            key = "beliggenhetsadresse" if unit.get("beliggenhetsadresse") else "postadresse"
            row = {
                "organisation_number": unit.get("organisasjonsnummer"),
                "name": unit.get("navn"),
                "address": format_address(address),
                "municipality": address.get("kommune"),
                "address_type": "location" if key == "beliggenhetsadresse" else "postal",
                "industry": {"code": _get(unit, "naeringskode1", "kode"), "description": _get(unit, "naeringskode1", "beskrivelse")} if unit.get("naeringskode1") else None,
                "employees": unit.get("antallAnsatte") if unit.get("harRegistrertAntallAnsatte") is not False else None,
                "start_date": unit.get("oppstartsdato"),
                "closed_date": unit.get("nedleggelsesdato"),
            }
            if unit.get("overordnetEnhet") and unit.get("overordnetEnhet") != env.org:
                continue  # never attach another entity's workplace
            rows.append(row)
            evs.append(env.cite(source_url=response.url, source_class=SC_REGISTRY, retrieved_at=response.retrieved_at, content_sha256=response.raw_sha256,
                                json_pointer=_pointer("_embedded", "underenheter", index), claim_span=_span("underenhet", {"organisasjonsnummer": row["organisation_number"], "navn": row["name"], "adresse": row["address"]}), http_status=response.status))
    if rows:
        extra = {"listed": len(rows), "total": count}
        env.claim("workplaces", "registered_workplaces", rows, "available", CONF_OFFICIAL, evs,
                  note=None if len(rows) >= count else f"List truncated to {len(rows)} of {count}; count is exact.", **extra)
    env.section("workplaces", "available", count=count)
    return rows


def _period(record: dict[str, Any]) -> tuple[str, str]:
    return (str(_get(record, "regnskapsperiode", "fraDato") or ""), str(_get(record, "regnskapsperiode", "tilDato") or ""))


def build_financials(env: Envelope, response: Response) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if not response.ok:
        if response.status == 500 and response.retryable is False:
            env.claim("financials", "annual_accounts", None, "not_available", None, [env.cite_response(response, SC_ACCOUNTS, note=response.error)],
                      note="Regnskapsregisteret's open API does not serve this statement layout (e.g. bank/insurance); the official filing copy is the route.")
            env.section("financials", "not_available", note="unsupported statement layout in open API")
        else:
            availability = _availability_for_failure(response)
            env.claim("financials", "annual_accounts", None, availability, None, [env.cite_response(response, SC_ACCOUNTS)],
                      note="Regnskapsregisteret returned no annual accounts for this organisation number (not the same as zero)." if availability == "not_available" else response.error)
            env.section("financials", availability)
            if availability == "failed":
                env.error("financials", response, response.error or "accounts fetch failed")
        return summary
    records = response.body if isinstance(response.body, list) else []
    indexed = [(i, r) for i, r in enumerate(records) if isinstance(r, dict)]
    # Wrong-company guard: only accept records whose embedded organisation number is this entity.
    mismatched = [i for i, r in indexed if _get(r, "virksomhet", "organisasjonsnummer") not in (None, env.org)]
    indexed = [(i, r) for i, r in indexed if i not in mismatched]
    if mismatched:
        env.error("financials", response, f"Dropped {len(mismatched)} account record(s) labelled with a different organisation number", retryable=False)
    if not indexed:
        env.claim("financials", "annual_accounts", None, "not_available", None, [env.cite_response(response, SC_ACCOUNTS)], note="Regnskapsregisteret returned an empty list.")
        env.section("financials", "not_available")
        return summary
    company = sorted([(i, r) for i, r in indexed if r.get("regnskapstype") == "SELSKAP"], key=lambda item: _period(item[1]))
    group = sorted([(i, r) for i, r in indexed if r.get("regnskapstype") == "KONSERN"], key=lambda item: _period(item[1]))
    other = [(i, r) for i, r in indexed if r.get("regnskapstype") not in ("SELSKAP", "KONSERN")]
    if not company and other:
        company = sorted(other, key=lambda item: _period(item[1]))

    def period_value(index: int, record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        value = {"from": _get(record, "regnskapsperiode", "fraDato"), "to": _get(record, "regnskapsperiode", "tilDato")}
        return value, env.cite_json(response, SC_ACCOUNTS, (index, "regnskapsperiode"))

    if company:
        index, latest = company[-1]
        period, period_ev = period_value(index, latest)
        basis = {"period": period, "currency": latest.get("valuta"), "account_type": "company" if latest.get("regnskapstype") == "SELSKAP" else str(latest.get("regnskapstype")).lower(),
                 "source_record_id": latest.get("id")}
        summary["latest_period"] = period
        summary["currency"] = latest.get("valuta")
        currency_ev = env.cite_json(response, SC_ACCOUNTS, (index, "valuta")) if latest.get("valuta") else None
        env.claim("financials", "latest_accounts_period", {**period, "account_type": basis["account_type"]}, "available", CONF_OFFICIAL, [period_ev])
        for metric, path in FINANCIAL_METRICS.items():
            amount = _get(latest, *path)
            if amount is None:
                continue
            ev = [env.cite_json(response, SC_ACCOUNTS, (index, *path)), period_ev] + ([currency_ev] if currency_ev else [])
            env.claim("financials", metric, {"amount": amount, **basis}, "available", CONF_OFFICIAL, ev)
            summary[metric] = amount
        audit = latest.get("revisjon") or {}
        if audit:
            env.claim("financials", "accounts_audit", {"not_audited": audit.get("ikkeRevidertAarsregnskap"), "audit_opted_out": audit.get("fravalgRevisjon"), "period": period},
                      "available", CONF_OFFICIAL, [env.cite_json(response, SC_ACCOUNTS, (index, "revisjon")), period_ev])
        if latest.get("avviklingsregnskap") is not None:
            env.claim("financials", "liquidation_accounts", {"value": bool(latest["avviklingsregnskap"]), "period": period}, "available", CONF_OFFICIAL,
                      [env.cite_json(response, SC_ACCOUNTS, (index, "avviklingsregnskap"))])
        history = []
        history_ev = []
        for index_h, record in company:
            row = {"period": {"from": _get(record, "regnskapsperiode", "fraDato"), "to": _get(record, "regnskapsperiode", "tilDato")}, "currency": record.get("valuta")}
            for metric in HISTORY_METRICS:
                amount = _get(record, *FINANCIAL_METRICS[metric])
                if amount is not None:
                    row[metric] = amount
            history.append(row)
            history_ev.append(env.cite_json(response, SC_ACCOUNTS, (index_h, "regnskapsperiode")))
            for metric in ("revenue", "net_result"):
                if metric in row:
                    history_ev.append(env.cite_json(response, SC_ACCOUNTS, (index_h, *FINANCIAL_METRICS[metric])))
        env.claim("financials", "financial_history", history, "available", CONF_OFFICIAL, history_ev,
                  note=f"All {len(history)} company-level period(s) returned by Regnskapsregisteret, oldest first.")
        summary["history"] = history
    if group:
        index, latest = group[-1]
        period, period_ev = period_value(index, latest)
        value: dict[str, Any] = {"period": period, "currency": latest.get("valuta"), "account_type": "group_consolidated", "source_record_id": latest.get("id")}
        ev = [period_ev]
        for metric in HISTORY_METRICS:
            amount = _get(latest, *FINANCIAL_METRICS[metric])
            if amount is not None:
                value[metric] = amount
                ev.append(env.cite_json(response, SC_ACCOUNTS, (index, *FINANCIAL_METRICS[metric])))
        env.claim("financials", "group_consolidated_financials", value, "available", CONF_OFFICIAL, ev)
        summary["group"] = value
    env.section("financials", "available", periods=len(company), group_periods=len(group))
    return summary


def build_filing_years(env: Envelope, response: Response | None, skipped_reason: str | None) -> None:
    if response is None:
        env.claim("financials", "annual_account_filing_years", None, "not_available", None, [], note=f"Not checked: {skipped_reason}", checked=False)
        return
    if not response.ok:
        availability = _availability_for_failure(response)
        env.claim("financials", "annual_account_filing_years", None, availability, None, [env.cite_response(response, SC_ACCOUNTS)], note=response.error)
        if availability == "failed":
            env.error("filing_years", response, response.error or "filing-year fetch failed")
        return
    years = sorted({str(y) for y in response.body if str(y).isdigit()}) if isinstance(response.body, list) else []
    if not years:
        env.claim("financials", "annual_account_filing_years", [], "available", CONF_OFFICIAL, [env.cite_response(response, SC_ACCOUNTS)], note="Checked; no filed annual-account copies listed.")
        return
    value = [{"year": year, "official_copy_url": FILING_COPY_URL.format(org=env.org, year=year)} for year in years]
    env.claim("financials", "annual_account_filing_years", value, "available", CONF_OFFICIAL,
              [env.cite(source_url=response.url, source_class=SC_ACCOUNTS, retrieved_at=response.retrieved_at, content_sha256=response.raw_sha256, json_pointer="/", claim_span=_span("aar", years), http_status=response.status)])


def _find_node(node: dict[str, Any], org: str, parent: dict[str, Any] | None = None, path: tuple[Any, ...] = ()) -> tuple[dict[str, Any] | None, dict[str, Any] | None, tuple[Any, ...]]:
    if node.get("organisasjonsnummer") == org:
        return node, parent, path
    for index, child in enumerate(node.get("children") or []):
        found, found_parent, found_path = _find_node(child, org, node, (*path, "children", index))
        if found is not None:
            return found, found_parent, found_path
    return None, None, ()


def _count(node: dict[str, Any]) -> int:
    return sum(1 + _count(child) for child in node.get("children") or [])


def build_group(env: Envelope, response: Response | None, part_of_group: bool | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if response is None:
        if part_of_group is False:
            env.section("group", "not_applicable", note="Registry flags the entity as not part of a group (erIKonsern=false).")
        return out
    if not response.ok:
        availability = _availability_for_failure(response)
        env.claim("group", "group_structure", None, availability, None, [env.cite_response(response, SC_REGISTRY)], note=response.error)
        env.section("group", availability)
        if availability == "failed":
            env.error("group", response, response.error or "group fetch failed")
        return out
    root = response.body if isinstance(response.body, dict) else {}
    node, parent, path = _find_node(root, env.org)
    if node is None:
        env.claim("group", "group_structure", None, "ambiguous", None, [env.cite_response(response, SC_REGISTRY)], note="Entity not found inside the returned group tree.")
        env.section("group", "ambiguous")
        return out
    if root.get("organisasjonsnummer") and root.get("organisasjonsnummer") != env.org:
        value = {"organisation_number": root.get("organisasjonsnummer"), "name": root.get("navn")}
        env.claim("group", "ultimate_parent", value, "available", CONF_OFFICIAL, [env.cite_json(response, SC_REGISTRY, ("organisasjonsnummer",)), env.cite_json(response, SC_REGISTRY, ("navn",))])
        out["ultimate_parent"] = value
    if parent is not None:
        value = {"organisation_number": node.get("parentOrganisasjonsnummer") or parent.get("organisasjonsnummer"), "name": node.get("parentNavn") or parent.get("navn"),
                 "relationship": _get(node, "knytningsform", "beskrivelse"), "ownership_basis": node.get("grunnlag"), "as_of": node.get("dato")}
        env.claim("group", "direct_parent", value, "available", CONF_OFFICIAL, [env.cite_json(response, SC_REGISTRY, (*path, "parentOrganisasjonsnummer")) if node.get("parentOrganisasjonsnummer") else env.cite_json(response, SC_REGISTRY, path, value={"organisasjonsnummer": node.get("organisasjonsnummer")})])
        out["direct_parent"] = value
    children = node.get("children") or []
    subs = []
    evs = []
    for index, child in enumerate(children):
        subs.append({"organisation_number": child.get("organisasjonsnummer"), "name": child.get("navn"), "relationship": _get(child, "knytningsform", "beskrivelse"), "ownership_basis": child.get("grunnlag"), "as_of": child.get("dato")})
        evs.append(env.cite(source_url=response.url, source_class=SC_REGISTRY, retrieved_at=response.retrieved_at, content_sha256=response.raw_sha256,
                            json_pointer=_pointer(*path, "children", index), claim_span=_span("child", {"organisasjonsnummer": child.get("organisasjonsnummer"), "navn": child.get("navn"), "grunnlag": child.get("grunnlag")}), http_status=response.status))
    env.claim("group", "direct_subsidiaries", subs, "available", CONF_OFFICIAL, evs or [env.cite_response(response, SC_REGISTRY)], note=None if subs else "Group tree checked; no direct subsidiaries listed.")
    env.claim("group", "group_entity_count", _count(root) + 1, "available", CONF_DERIVED, [env.cite_response(response, SC_REGISTRY)], note="Number of entities in the registry group tree (root + descendants).")
    out["subsidiaries"] = len(subs)
    out["group_size"] = _count(root) + 1
    env.section("group", "available")
    return out


def build_website(env: Envelope, outcome: dict[str, Any] | None, registry_ev: str | None, skipped_reason: str | None, via: str = "registry_website") -> dict[str, Any]:
    if outcome is not None and via == "registry_email_domain" and outcome.get("status") != "verified":
        # The registry never named this domain as the website: report the check, publish nothing.
        domain = outcome.get("requested_url")
        env.claim("website", "official_website", None, "not_available", None, [registry_ev] if registry_ev else [],
                  note=f"No website listed in Enhetsregisteret. Candidate domain from the registry e-mail address ({domain}) was checked but ownership could not be verified ({outcome.get('status')}).",
                  candidate_checked=domain)
        env.claim("hiring", "careers_page", None, "not_available", None, [], note="No verified official website to look for a careers page on.")
        env.section("website", "not_available", note=f"email-domain candidate {outcome.get('status')}")
        env.section("hiring", "not_available")
        return {"status": outcome.get("status")}
    if outcome is None:
        env.claim("website", "official_website", None, "not_available", None, [registry_ev] if registry_ev else [], note=f"Not checked: {skipped_reason}", checked=False)
        env.claim("hiring", "careers_page", None, "not_available", None, [], note=f"Not checked: {skipped_reason}", checked=False)
        env.section("website", "not_available", note=skipped_reason)
        env.section("hiring", "not_available", note=skipped_reason)
        return {}
    status = outcome.get("status")
    if status == "not_listed":
        env.claim("website", "official_website", None, "not_available", None, [registry_ev] if registry_ev else [], note="No website is listed in Enhetsregisteret; no other source is used to guess one.")
        env.claim("hiring", "careers_page", None, "not_available", None, [], note="No verified official website to look for a careers page on.")
        env.section("website", "not_available")
        env.section("hiring", "not_available")
        return {}
    home = outcome.get("homepage_fetch") or {}
    ev = [registry_ev] if registry_ev else []
    if status in ("invalid_url", "unreachable", "blocked_robots"):
        availability = "blocked" if status == "blocked_robots" else "failed" if status == "unreachable" else "not_available"
        ev.append(env.cite(source_url=home.get("final_url") or home.get("url") or outcome.get("requested_url") or "", source_class=SC_COMPANY, retrieved_at=home.get("retrieved_at"), content_sha256=home.get("sha256"), http_status=home.get("status") or None, note=home.get("error")))
        env.claim("website", "official_website", outcome.get("requested_url"), availability, None, ev,
                  note={"blocked_robots": f"Not fetched: {home.get('error')}.", "unreachable": f"Registry-listed site could not be fetched ({home.get('error')}).", "invalid_url": "Registry website value is not a valid URL."}[status],
                  verification="not_performed")
        env.claim("hiring", "careers_page", None, "not_available", None, [], note="Official website could not be verified.")
        env.section("website", availability)
        env.section("hiring", "not_available")
        return {"status": status}
    final_url = outcome.get("final_url")
    proof = outcome.get("verification")
    if status == "verified" and proof:
        ev.append(env.cite(source_url=proof["page"], source_class=SC_COMPANY, retrieved_at=proof.get("retrieved_at"), content_sha256=proof.get("sha256"), claim_span=proof["span"],
                           note="Exact organisation number visible on the site." if proof["method"] == "organisation_number" else "Exact registered legal name visible on the site."))
        confidence = 0.97 if proof["method"] == "organisation_number" else 0.9
        if via == "registry_email_domain":
            confidence = 0.95 if proof["method"] == "organisation_number" else 0.85
        env.claim("website", "official_website", final_url, "available", confidence, ev, verification=proof["method"], registered_domain=outcome.get("registered_domain"),
                  discovered_via=via, note=None if via == "registry_website" else "Not listed as hjemmeside; domain taken from the registry e-mail address and ownership verified on the site.")
        env.section("website", "available", verification=proof["method"])
        site_ev = env.cite(source_url=final_url, source_class=SC_COMPANY, retrieved_at=home.get("retrieved_at"), content_sha256=home.get("sha256"),
                           claim_span=(outcome.get("description") or outcome.get("title") or "")[:300] or None)
        description = str(outcome.get("description") or "")
        boilerplate = len(description) < 25 or re.match(r"(?i)\s*(powered by|just another|welcome to|velkommen til\s*$|home\b|hjem\b|coming soon)", description)
        if description and not boilerplate:
            env.claim("profile", "website_self_description", outcome["description"], "available", 0.9, [site_ev], note="Company's own description (meta description) on its verified website; company-reported, not an official fact.")
        social = [item for item in outcome.get("social_links") or [] if item.get("handle_matches_legal_name")]
        if social:
            env.claim("public_activity", "social_profiles", [{"platform": item["platform"], "url": item["url"]} for item in social], "available", 0.9, [site_ev],
                      note="Linked from the verified official website and handle matches the legal name.")
        else:
            env.claim("public_activity", "social_profiles", [], "not_available", None, [site_ev], note="Verified site checked; no social profile link whose handle matches the legal name.")
        careers = outcome.get("careers")
        if careers and careers.get("sha256"):
            careers_ev = env.cite(source_url=careers["url"], source_class=SC_COMPANY, retrieved_at=careers.get("retrieved_at"), content_sha256=careers.get("sha256"), claim_span=(careers.get("title") or careers.get("link_text") or "")[:200] or None, http_status=200)
            link_ev = env.cite(source_url=careers["found_on"], source_class=SC_COMPANY, retrieved_at=home.get("retrieved_at"), content_sha256=home.get("sha256"), claim_span=f"link text: {careers.get('link_text')}")
            env.claim("hiring", "careers_page", careers["url"], "available", 0.9, [careers_ev, link_ev], note="Careers/jobs page on the verified official domain; open positions are not counted.")
            env.section("hiring", "available")
        else:
            note = "Verified site checked; no careers/jobs page link found." if not careers else f"Careers link found but page not retrievable ({careers.get('error')})."
            env.claim("hiring", "careers_page", None, "not_available", None, [site_ev], note=note)
            env.section("hiring", "not_available")
        return {"status": "verified", "url": final_url, "method": proof["method"]}
    # Unverified / conflicting / placeholder: keep the evidence, do not assert ownership.
    ev.append(env.cite(source_url=final_url or "", source_class=SC_COMPANY, retrieved_at=home.get("retrieved_at"), content_sha256=home.get("sha256"), http_status=home.get("status") or None,
                       claim_span=(outcome.get("title") or "")[:200] or None))
    notes = {
        "placeholder": "Registry-listed domain shows a parked/for-sale/placeholder page.",
        "conflicting_identity": f"Site shows a different organisation number ({', '.join(outcome.get('other_organisation_numbers_on_site') or [])}); possible parent/sister/brand site.",
        "unverified": "Neither the organisation number nor the exact legal name is visible on the fetched pages.",
    }
    env.claim("website", "official_website", final_url, "ambiguous", 0.4 if status == "unverified" else 0.2, ev, note=notes.get(status, status), verification="failed")
    env.claim("hiring", "careers_page", None, "not_available", None, [], note="Official website could not be verified, so no hiring items are attributed.")
    env.section("website", "ambiguous", note=status)
    env.section("hiring", "not_available")
    return {"status": status}


def build_summary(env: Envelope, identity: dict[str, Any], fin: dict[str, Any], roles: list[dict[str, Any]], workplaces_total: int | None, group: dict[str, Any], website: dict[str, Any]) -> None:
    if not identity:
        return
    by_field = {claim["field"]: claim for claim in env.claims if claim["availability"] == "available"}
    used: list[str] = []

    def use(field_name: str) -> dict[str, Any] | None:
        claim = by_field.get(field_name)
        if claim:
            used.extend(claim["evidence_ids"])
        return claim

    parts: list[str] = []
    name = identity.get("navn")
    form = _get(identity, "organisasjonsform", "beskrivelse")
    use("legal_name"), use("organisation_form")
    sentence = f"{name} is a{'n' if str(form or '').lower()[:1] in 'aeiouæøå' else ''} {str(form).lower()} ({_get(identity, 'organisasjonsform', 'kode')})" if form else f"{name}"
    if use("foundation_date"):
        sentence += f" founded {identity.get('stiftelsesdato')}"
    elif use("registration_date"):
        sentence += f" registered {identity.get('registreringsdatoEnhetsregisteret')}"
    if use("municipality"):
        sentence += f", based in {str(_get(identity, 'forretningsadresse', 'kommune') or '').title()}"
    parts.append(sentence + ".")
    if use("industry_primary"):
        parts.append(f"Primary industry: {_get(identity, 'naeringskode1', 'beskrivelse')} (NACE {_get(identity, 'naeringskode1', 'kode')}).")
    status = by_field.get("registry_status")
    if status and status["value"]["status"] != "active":
        use("registry_status")
        parts.append(f"Registry status: {status['value']['status'].replace('_', ' ')}.")
    if use("employees"):
        parts.append(f"Registered employees: {identity.get('antallAnsatte')}.")
    ceo = by_field.get("chief_executive")
    chair = by_field.get("board_chair")
    if ceo and isinstance(ceo["value"], dict):
        use("chief_executive")
        parts.append(f"Chief executive (daglig leder): {ceo['value']['name']}.")
    if chair and isinstance(chair["value"], dict):
        use("board_chair")
        parts.append(f"Board chair: {chair['value']['name']}.")
    if fin.get("latest_period") and "revenue" in fin:
        use("revenue"), use("latest_accounts_period")
        line = f"FY ending {fin['latest_period']['to']}: revenue {fin['revenue']:,} {fin.get('currency') or ''}".rstrip()
        if "net_result" in fin:
            use("net_result")
            line += f", net result {fin['net_result']:,}"
        history = fin.get("history") or []
        if len(history) >= 2 and isinstance(history[-2].get("revenue"), (int, float)) and history[-2]["revenue"]:
            prev = history[-2]["revenue"]
            use("financial_history")
            change = (fin["revenue"] - prev) / abs(prev) * 100
            line += f"; revenue change vs prior period {change:+.1f}% (computed from the two cited periods)"
        parts.append(line + ".")
    elif fin.get("latest_period") and "total_assets" in fin:
        use("total_assets"), use("latest_accounts_period")
        parts.append(f"FY ending {fin['latest_period']['to']}: total assets {fin['total_assets']:,} {fin.get('currency') or ''}.".replace(" .", "."))
    if workplaces_total:
        use("registered_workplace_count")
        parts.append(f"Registered workplaces (sub-units): {workplaces_total}.")
    if group.get("ultimate_parent"):
        use("ultimate_parent")
        parts.append(f"Part of the group headed by {group['ultimate_parent']['name']} ({group['ultimate_parent']['organisation_number']}).")
    elif group.get("subsidiaries"):
        use("direct_subsidiaries")
        parts.append(f"Group parent with {group['subsidiaries']} direct subsidiar{'y' if group['subsidiaries'] == 1 else 'ies'} in the registry.")
    if website.get("status") == "verified":
        use("official_website")
        parts.append(f"Verified official website: {website['url']}.")
    if by_field.get("careers_page"):
        use("careers_page")
        parts.append(f"Careers page: {by_field['careers_page']['value']}.")
    env.claim("synthesis", "company_summary", " ".join(parts), "available", CONF_DERIVED, used,
              note="Deterministic summary composed only from the cited claims in this envelope.")


# --------------------------------------------------------------------------------------------------
# Changes vs previous snapshot
# --------------------------------------------------------------------------------------------------

VOLATILE_FIELDS = {"company_summary"}


def _claim_key(claim: dict[str, Any]) -> str:
    return claim["field"]


def _claim_state(claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        if claim["field"] in VOLATILE_FIELDS:
            continue
        grouped.setdefault(_claim_key(claim), []).append(claim)
    state = {}
    for key, items in grouped.items():
        values = sorted((_canon({"availability": c["availability"], "value": c.get("value")}) for c in items))
        state[key] = {"values": values, "claims": items}
    return state


def diff_claims(previous: dict[str, Any] | None, current_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not previous:
        return []
    old = _claim_state(previous.get("claims") or [])
    new = _claim_state(current_claims)
    changes = []
    for key in sorted(set(old) | set(new)):
        before = old.get(key)
        after = new.get(key)
        if before and after and before["values"] == after["values"]:
            continue
        old_claims = before["claims"] if before else []
        new_claims = after["claims"] if after else []

        def pick(items: list[dict[str, Any]], attr: str) -> Any:
            values = [item.get(attr) for item in items]
            return values[0] if len(values) == 1 else values or None

        old_av, new_av = pick(old_claims, "availability"), pick(new_claims, "availability")
        if not before:
            kind = "added"
        elif not after:
            kind = "removed"
        elif old_av != new_av:
            kind = "availability_changed"
        else:
            kind = "value_changed"
        changes.append({
            "field": key,
            "section": (new_claims or old_claims)[0].get("section"),
            "change_type": kind,
            "old_value": pick(old_claims, "value"),
            "new_value": pick(new_claims, "value"),
            "old_availability": old_av,
            "new_availability": new_av,
            "evidence_ids": sorted({ev for item in new_claims for ev in item.get("evidence_ids", [])}, key=lambda ev: int(ev.split("-")[1])),
            "previous_run_id": _get(previous, "run", "run_id"),
            "previous_completed_at": _get(previous, "run", "completed_at"),
        })
    return changes


# --------------------------------------------------------------------------------------------------
# Orchestration for one company
# --------------------------------------------------------------------------------------------------

@dataclass
class RunContext:
    run_id: str
    client: BrregClient
    deadline: float  # monotonic; optional modules are skipped after this
    previous: dict[str, dict[str, Any]] = field(default_factory=dict)
    bulk_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    with_website: bool = True
    with_filing_years: bool = True
    filing_years_interval_s: float = 2.1
    workplace_cap: int = 200
    website_timeout_s: float = 10.0


def _unflatten_bulk(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, raw in row.items():
        if raw in (None, ""):
            continue
        value: Any = raw
        if raw in ("true", "false"):
            value = raw == "true"
        elif key == "antallAnsatte" and str(raw).isdigit():
            value = int(raw)
        elif key.endswith(".adresse"):
            value = [part.strip() for part in str(raw).split(",") if part.strip()]
        node = out
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = value
    if "registrertIMvaRegisteret" in out:
        out["registrertIMvaregisteret"] = out.pop("registrertIMvaRegisteret")
    return out


def research_company(org: str, ctx: RunContext) -> dict[str, Any]:
    started_at = utc_now()
    t0 = time.monotonic()
    stats = RequestStats()
    env = Envelope(org=org)
    skipped: list[str] = []
    terminal = "completed"

    def get(url: str, **kwargs: Any) -> Response:
        return ctx.client.get_json(url, stats=stats, **kwargs)

    if not valid_orgnr(org):
        env.claim("identity", "organisation_number", org, "not_available", None, [], note="Input is not a valid Norwegian organisation number (9 digits, MOD11).")
        env.error("input", None, "invalid organisation number", retryable=False)
        terminal = "failed"
        identity: dict[str, Any] = {}
        entity = None
    else:
        entity = get(ENTITY_URL.format(org=org))
        identity = {}
        if entity.ok and _get(entity.body, "organisasjonsnummer") == org:
            identity = build_identity(env, entity)
        elif entity.status == 410 or (entity.ok and _get(entity.body, "slettedato")):
            env.claim("status", "registry_status", {"status": "deleted"}, "available", CONF_OFFICIAL, [env.cite_response(entity, SC_REGISTRY, note="HTTP 410 Gone: entity removed from Enhetsregisteret")])
            env.section("identity", "not_available", note="deleted")
        elif entity.status == 404:
            env.claim("identity", "legal_name", None, "not_available", None, [env.cite_response(entity, SC_REGISTRY)], note="Organisation number not found in Enhetsregisteret (main units).")
            env.section("identity", "not_available")
        else:
            env.error("identity", entity, entity.error or "entity fetch failed")
            bulk = ctx.bulk_rows.get(org)
            if bulk:
                fallback = Response(BULK_URL, 200, _unflatten_bulk(bulk), bulk.get("_snapshot_sha256"), bulk.get("_retrieved_at"))
                identity = build_identity(env, fallback, source_class="official_registry_bulk")
                terminal = "partial"
            else:
                env.claim("identity", "legal_name", None, "failed", None, [env.cite_response(entity, SC_REGISTRY)], note=entity.error)
                env.section("identity", "failed")
                terminal = "failed"

    fin: dict[str, Any] = {}
    roles: list[dict[str, Any]] = []
    workplaces_total: int | None = None
    group: dict[str, Any] = {}
    website: dict[str, Any] = {}
    if identity:
        legal_form = _get(identity, "organisasjonsform", "kode")
        roles = build_roles(env, get(ROLES_URL.format(org=org)), legal_form)

        size = 200
        first = get(SUBUNITS_URL.format(org=org, size=size, page=0))
        pages = [first]
        if first.ok:
            workplaces_total = _get(first.body, "page", "totalElements")
            total_pages = _get(first.body, "page", "totalPages") or 1
            for page in range(1, min(total_pages, -(-ctx.workplace_cap // size))):
                pages.append(get(SUBUNITS_URL.format(org=org, size=size, page=page)))
            pages = [p for p in pages if p.ok] or [first]
        build_workplaces(env, pages, workplaces_total, cap=ctx.workplace_cap)

        fin = build_financials(env, get(ACCOUNTS_URL.format(org=org)))

        part_of_group = identity.get("erIKonsern")
        group = build_group(env, get(GROUP_URL.format(org=org)) if part_of_group else None, part_of_group)

        registry_ev = next((claim["evidence_ids"][0] for claim in env.claims if claim["field"] == "registry_website"), None)
        if not ctx.with_website:
            build_website(env, None, registry_ev, "website module disabled for this run")
            skipped.append("website")
        elif time.monotonic() > ctx.deadline:
            build_website(env, None, registry_ev, "run time budget reached before this module")
            skipped.append("website")
        else:
            via = "registry_website"
            target = identity.get("hjemmeside")
            candidate = email_domain_candidate(identity.get("epostadresse")) if not target and legal_form != "ENK" else None
            if candidate:
                via, target = "registry_email_domain", candidate
                registry_ev = next((claim["evidence_ids"][0] for claim in env.claims if claim["field"] == "contact_email"), None)
            outcome, site_stats = verify_registry_website(target, org=org, legal_name=str(identity.get("navn") or ""), timeout=ctx.website_timeout_s,
                                                          name_proof_homepage_only=via == "registry_email_domain")
            stats.add(requests=site_stats.requests, bytes_received=site_stats.bytes_received)
            website = build_website(env, outcome, registry_ev, None, via=via)

        expects_filings = bool(identity.get("sisteInnsendteAarsregnskap")) or bool(fin)
        if not ctx.with_filing_years:
            build_filing_years(env, None, "filing-year module disabled for this run")
            skipped.append("filing_years")
        elif not expects_filings:
            build_filing_years(env, None, "registry shows no submitted annual accounts")
        elif time.monotonic() > ctx.deadline:
            build_filing_years(env, None, "run time budget reached before this module")
            skipped.append("filing_years")
        else:
            build_filing_years(env, get(FILING_YEARS_URL.format(org=org), lane="filing_years", lane_interval_s=ctx.filing_years_interval_s), None)

        build_summary(env, identity, fin, roles, workplaces_total, group, website)

    if terminal == "completed" and any(error["module"] != "input" for error in env.errors):
        terminal = "partial"

    previous = ctx.previous.get(org)
    changes = diff_claims(previous, env.claims)
    runtime_ms = int((time.monotonic() - t0) * 1000)
    return {
        "organisation_number": org,
        "agent": {"name": AGENT_NAME, "version": AGENT_VERSION},
        "run": {
            "run_id": ctx.run_id,
            "started_at": started_at,
            "completed_at": utc_now(),
            "terminal_status": terminal,
            "previous_run_id": _get(previous, "run", "run_id") if previous else None,
        },
        "sections": env.sections,
        "claims": env.claims,
        "evidence": env.evidence,
        "changes": changes,
        "errors": env.errors,
        "operations": {
            "requests": stats.network_requests,
            "cache_hits": stats.cache_hits,
            "bytes_received": stats.bytes_received,
            "runtime_ms": runtime_ms,
            "third_party_cost_usd": 0,
            "modules_skipped": skipped,
        },
    }


def failed_envelope(org: str, run_id: str, message: str, started_at: str) -> dict[str, Any]:
    return {
        "organisation_number": org,
        "agent": {"name": AGENT_NAME, "version": AGENT_VERSION},
        "run": {"run_id": run_id, "started_at": started_at, "completed_at": utc_now(), "terminal_status": "failed", "previous_run_id": None},
        "sections": {},
        "claims": [],
        "evidence": [],
        "changes": [],
        "errors": [{"module": "agent", "source_url": None, "http_status": None, "message": message, "retryable": True, "at": utc_now()}],
        "operations": {"requests": 0, "cache_hits": 0, "bytes_received": 0, "runtime_ms": 0, "third_party_cost_usd": 0, "modules_skipped": ["all"]},
    }


def envelope_digest(envelope: dict[str, Any]) -> str:
    """Stable digest of the factual content (claims only) for idempotency checks."""
    stable = [{k: v for k, v in claim.items() if k != "evidence_ids"} for claim in envelope.get("claims", [])]
    return hashlib.sha256(_canon(stable).encode()).hexdigest()
