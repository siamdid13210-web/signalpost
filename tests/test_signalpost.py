from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.brreg_client import Response, parse_json  # noqa: E402
from norway_company_agent.signalpost import (  # noqa: E402
    Envelope, build_financials, build_group, build_identity, build_roles, build_website, diff_claims, valid_orgnr,
)
from norway_company_agent.site_verify import find_identity, legal_name_regex  # noqa: E402

ORG = "810034882"


def resp(body, status=200, url="https://data.brreg.no/x", retryable=False, error=None):
    return Response(url=url, status=status, body=body, raw_sha256="0" * 64, retrieved_at="2026-11-17T00:00:00Z", retryable=retryable, error=error)


def account(kind, year, revenue, org=ORG):
    return {
        "id": int(year) * 10 + (1 if kind == "KONSERN" else 0), "regnskapstype": kind, "valuta": "NOK",
        "virksomhet": {"organisasjonsnummer": org},
        "regnskapsperiode": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"},
        "resultatregnskapResultat": {"aarsresultat": 5, "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": revenue}}},
        "eiendeler": {"sumEiendeler": 100},
    }


def claims(env, field):
    return [c for c in env.claims if c["field"] == field]


class FinancialTests(unittest.TestCase):
    def test_latest_company_period_selected_and_group_kept_separate(self):
        env = Envelope(org=ORG)
        body = [account("KONSERN", 2025, 999), account("SELSKAP", 2025, 30), account("SELSKAP", 2023, 10), account("SELSKAP", 2024, 20), account("KONSERN", 2024, 888)]
        build_financials(env, resp(body))
        revenue = claims(env, "revenue")[0]
        self.assertEqual(revenue["value"]["amount"], 30)
        self.assertEqual(revenue["value"]["period"]["to"], "2025-12-31")
        self.assertEqual(revenue["value"]["account_type"], "company")
        self.assertEqual([row["revenue"] for row in claims(env, "financial_history")[0]["value"]], [10, 20, 30])
        self.assertEqual(claims(env, "group_consolidated_financials")[0]["value"]["revenue"], 999)
        ids = {ev["id"]: ev for ev in env.evidence}
        pointers = [ids[i]["json_pointer"] for i in revenue["evidence_ids"]]
        self.assertIn("/1/resultatregnskapResultat/driftsresultat/driftsinntekter/sumDriftsinntekter", pointers)

    def test_missing_metric_is_not_published_as_zero(self):
        env = Envelope(org=ORG)
        record = account("SELSKAP", 2025, 30)
        del record["eiendeler"]
        build_financials(env, resp([record]))
        self.assertEqual(claims(env, "total_assets"), [])

    def test_wrong_company_records_dropped(self):
        env = Envelope(org=ORG)
        build_financials(env, resp([account("SELSKAP", 2025, 30, org="923609016")]))
        self.assertEqual(claims(env, "revenue"), [])
        self.assertEqual(claims(env, "annual_accounts")[0]["availability"], "not_available")
        self.assertTrue(env.errors)

    def test_404_and_unsupported_layout(self):
        env = Envelope(org=ORG)
        build_financials(env, resp(None, status=404, error="HTTP 404"))
        self.assertEqual(claims(env, "annual_accounts")[0]["availability"], "not_available")
        env = Envelope(org=ORG)
        build_financials(env, resp(None, status=500, retryable=False, error="HTTP 500: oppstillingsplan (BANK)"))
        self.assertEqual(claims(env, "annual_accounts")[0]["availability"], "not_available")
        self.assertEqual(env.errors, [])
        env = Envelope(org=ORG)
        build_financials(env, resp(None, status=503, retryable=True, error="HTTP 503"))
        self.assertEqual(claims(env, "annual_accounts")[0]["availability"], "failed")

    def test_amounts_parsed_exactly(self):
        self.assertEqual(parse_json(b'{"a": 2375000.00, "b": 0.1}'), {"a": 2375000, "b": 0.1})


class IdentityAndRolesTests(unittest.TestCase):
    def test_missing_employee_count_is_not_zero(self):
        env = Envelope(org=ORG)
        build_identity(env, resp({"organisasjonsnummer": ORG, "navn": "X AS", "harRegistrertAntallAnsatte": False, "organisasjonsform": {"kode": "AS"}}))
        employees = claims(env, "employees")[0]
        self.assertEqual(employees["availability"], "not_available")
        self.assertIsNone(employees["value"])

    def test_sole_proprietor_contact_details_omitted(self):
        env = Envelope(org=ORG)
        build_identity(env, resp({"organisasjonsnummer": ORG, "navn": "Ola", "telefon": "123", "organisasjonsform": {"kode": "ENK"}}))
        self.assertEqual(claims(env, "contact_phone"), [])

    def test_roles_skip_deregistered_and_birthdates(self):
        env = Envelope(org=ORG)
        body = {"rollegrupper": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "roller": [
            {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"fodselsdato": "1980-01-01", "navn": {"fornavn": "Kari", "etternavn": "Nordmann"}}},
            {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "avregistrert": True, "person": {"navn": {"fornavn": "Old", "etternavn": "Boss"}}},
        ]}]}
        build_roles(env, resp(body), "AS")
        self.assertEqual(claims(env, "chief_executive")[0]["value"]["name"], "Kari Nordmann")
        self.assertEqual(claims(env, "board_chair")[0]["availability"], "not_available")
        self.assertNotIn("1980", str(env.claims) + str(env.evidence))

    def test_group_tree(self):
        tree = {"organisasjonsnummer": "1", "navn": "TOP", "children": [{"organisasjonsnummer": ORG, "navn": "ME", "parentOrganisasjonsnummer": "1", "parentNavn": "TOP", "grunnlag": "100%", "children": [{"organisasjonsnummer": "3", "navn": "SUB"}]}]}
        env = Envelope(org=ORG)
        build_group(env, resp(tree), True)
        self.assertEqual(claims(env, "ultimate_parent")[0]["value"]["organisation_number"], "1")
        self.assertEqual(claims(env, "direct_parent")[0]["value"]["ownership_basis"], "100%")
        self.assertEqual(claims(env, "direct_subsidiaries")[0]["value"][0]["organisation_number"], "3")

    def test_orgnr_checksum(self):
        self.assertTrue(valid_orgnr("923609016"))
        self.assertFalse(valid_orgnr("923609017"))
        self.assertFalse(valid_orgnr("12345"))


class WebsiteTests(unittest.TestCase):
    def test_name_regex_tolerates_only_suffix_spelling(self):
        self.assertTrue(legal_name_regex("AUTOBJØRN A/S").search("© 2026 Autobjørn AS"))
        self.assertTrue(legal_name_regex("FINN BRYNILDSRUD & SØNNER AS").search("Finn Brynildsrud & Sønner A/S"))
        self.assertFalse(legal_name_regex("NORDIC BYGG AS").search("Nordic Bygg Holding AS"))
        self.assertFalse(legal_name_regex("BYGG AS").search("byggas"))

    def test_orgnr_and_conflicting_orgnr_detection(self):
        found = find_identity("Kontakt: Org.nr 810 034 882 MVA", ORG, "X AS")
        self.assertTrue(found["orgnr_span"])
        found = find_identity("Sandnes Elektriske AS · Org.nr: 923 609 016", ORG, "SANDNES ELEKTRISKE AS")
        self.assertTrue(found["name_span"])
        self.assertEqual(found["other_orgnrs"], ["923609016"])

    def test_unverified_site_is_ambiguous_not_dropped(self):
        env = Envelope(org=ORG)
        outcome = {"status": "unverified", "final_url": "https://x.no/", "homepage_fetch": {"url": "https://x.no/", "sha256": "a", "retrieved_at": "t", "status": 200}, "title": "X"}
        build_website(env, outcome, None, None)
        site = claims(env, "official_website")[0]
        self.assertEqual(site["availability"], "ambiguous")
        self.assertTrue(site["evidence_ids"])
        self.assertEqual(claims(env, "careers_page")[0]["availability"], "not_available")


class ChangeTests(unittest.TestCase):
    def test_changes_and_idempotency(self):
        old = {"run": {"run_id": "r1"}, "claims": [{"field": "employees", "section": "identity", "value": {"count": 5}, "availability": "available", "evidence_ids": []},
                                                  {"field": "legal_name", "section": "identity", "value": "A AS", "availability": "available", "evidence_ids": []}]}
        new = [{"field": "employees", "section": "identity", "value": {"count": 7}, "availability": "available", "evidence_ids": ["ev-1"]},
               {"field": "legal_name", "section": "identity", "value": "A AS", "availability": "available", "evidence_ids": ["ev-2"]}]
        changes = diff_claims(old, new)
        self.assertEqual([(c["field"], c["change_type"]) for c in changes], [("employees", "value_changed")])
        self.assertEqual(diff_claims({"run": {}, "claims": new}, new), [])
        self.assertEqual(diff_claims(None, new), [])


if __name__ == "__main__":
    unittest.main()


class RobotsTests(unittest.TestCase):
    def test_unreadable_robots_is_not_permission(self):
        import urllib.error
        from norway_company_agent import site_verify

        class Boom:
            def __init__(self, exc):
                self.exc = exc

            def open(self, *args, **kwargs):
                raise self.exc

        stats = site_verify.SiteStats()
        site_verify._robots_cache.clear()
        self.assertIs(site_verify._robots("https://t1.example", 1, stats, opener=Boom(TimeoutError())), site_verify.ROBOTS_UNAVAILABLE)
        self.assertIs(site_verify._robots("https://t2.example", 1, stats, opener=Boom(urllib.error.HTTPError("u", 503, "x", {}, None))), site_verify.ROBOTS_UNAVAILABLE)
        self.assertIsNone(site_verify._robots("https://t3.example", 1, stats, opener=Boom(urllib.error.HTTPError("u", 404, "x", {}, None))))
        site_verify._robots_cache["https://t1.example"] = site_verify.ROBOTS_UNAVAILABLE
        self.assertIsNone(site_verify.robots_allows("https://t1.example/page", 1, stats))
        site_verify._robots_cache.clear()
