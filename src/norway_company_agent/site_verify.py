"""Registry-listed website verification (company-owned evidence layer).

A website is only claimed as the company's *verified* official website when the exact organisation
number, or the exact registered legal name, is visible in the HTML text of a page on that site.
Footers are kept (org numbers usually live there) — unlike main-text extractors that strip them.
Everything else is reported, with evidence, as unverified/ambiguous rather than dropped.

Politeness: robots.txt is honoured (cached per origin), one homepage + at most a few same-domain
pages per company, small byte caps, short timeouts, no JavaScript rendering.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from .brreg_client import USER_AGENT, utc_now
from .identity import assess_social_identity
from .website import SAFE_OPENER, SafeRedirectHandler, _registered_domain, _social_links, assert_public_url, normalize_homepage

PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "this domain may be for sale", "hugedomains", "parked at",
    "domenet er til salgs", "dette domenet er registrert", "her flytter snart en ny gjest",
    "buy this domain", "domain parking", "website coming soon", "under construction",
)
PARKING_DOMAINS = {"hugedomains.com", "sedo.com", "dan.com", "afternic.com", "parkingcrew.net", "bodis.com", "domainmarket.com", "undeveloped.com", "sav.com"}
IDENTITY_PAGE_TERMS = (
    "kontakt", "contact", "om-oss", "om_oss", "omoss", "about", "personvern", "privacy",
    "vilkar", "vilkaar", "terms", "impressum", "kundeservice", "firma", "selskap",
)
CAREERS_TERMS = (
    "karriere", "ledige-stillinger", "ledigestillinger", "ledige stillinger", "stillinger", "jobb",
    "jobbe-hos", "jobbe hos", "career", "careers", "jobs", "vacancies", "work-with-us", "join-us",
)
ORG_LABEL = re.compile(r"(?:org(?:anisasjons)?\.?\s*-?\s*n(?:umme)?r\.?|foretaksregisteret|organization number|company (?:reg(?:istration)?\.?\s*)?(?:no|number))\s*[:.]?\s*(?:no\s*)?(\d[\d . ]{7,12}\d)", re.I)


@dataclass
class SiteFetch:
    url: str
    final_url: str | None = None
    status: int = 0
    html: str | None = None
    sha256: str | None = None
    retrieved_at: str | None = None
    error: str | None = None
    blocked_by_robots: bool = False


@dataclass
class SiteStats:
    requests: int = 0
    bytes_received: int = 0
    latencies_ms: list[int] = field(default_factory=list)


ROBOTS_UNAVAILABLE = "unavailable"
_robots_cache: dict[str, Any] = {}
_robots_lock = threading.Lock()


def _robots(origin: str, timeout: float, stats: SiteStats, opener: Any = None) -> Any:
    """Return a parser, None (no robots.txt: 4xx => crawling allowed), or ROBOTS_UNAVAILABLE.

    A robots.txt that cannot be read (timeout, network error, 5xx) is NOT treated as permission:
    the site is reported as blocked rather than fetched.
    """
    with _robots_lock:
        if origin in _robots_cache:
            return _robots_cache[origin]
    opener = opener or SAFE_OPENER
    result: Any
    try:
        request = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": USER_AGENT})
        stats.requests += 1
        with opener.open(request, timeout=timeout) as response:
            text = response.read(500_000).decode("utf-8", errors="replace")
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        result = parser
    except urllib.error.HTTPError as exc:
        result = None if 400 <= exc.code < 500 else ROBOTS_UNAVAILABLE
    except Exception:
        result = ROBOTS_UNAVAILABLE
    with _robots_lock:
        _robots_cache[origin] = result
    return result


def robots_allows(url: str, timeout: float, stats: SiteStats) -> bool | None:
    """True/False per robots.txt; None when robots.txt could not be read (treated as not allowed)."""
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    parser = _robots(origin, timeout, stats)
    if parser is None:
        return True
    if parser is ROBOTS_UNAVAILABLE:
        return None
    # can_fetch falls back to the "*" group when no group names our user agent.
    return parser.can_fetch(USER_AGENT, url)


class RobotsDisallowedRedirect(urllib.error.URLError):
    pass


class RobotsAwareRedirectHandler(SafeRedirectHandler):
    """Public-address check (SSRF) plus robots.txt check for every redirect target, before requesting it."""

    timeout = 10.0

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        allowed = robots_allows(newurl, self.timeout, SiteStats())
        if not allowed:
            raise RobotsDisallowedRedirect(f"redirect target {newurl[:120]} not allowed by robots.txt" if allowed is False else f"robots.txt for redirect target {newurl[:120]} could not be read")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SITE_OPENER = urllib.request.build_opener(RobotsAwareRedirectHandler())


def fetch_page(url: str, *, timeout: float, max_bytes: int, stats: SiteStats) -> SiteFetch:
    result = SiteFetch(url=url, retrieved_at=utc_now())
    try:
        assert_public_url(url)
    except ValueError as exc:
        result.error = f"unsafe or unresolvable URL: {exc}"
        return result
    allowed = robots_allows(url, timeout, stats)
    if not allowed:
        result.blocked_by_robots = True
        result.error = "robots.txt disallows this page" if allowed is False else "robots.txt could not be read (timeout/network/5xx); not fetched"
        return result
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "nb,no,en;q=0.8"})
    try:
        stats.requests += 1
        with SITE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            result.status = response.status
            result.final_url = response.geturl()
            ctype = response.headers.get("content-type", "")
            charset = response.headers.get_content_charset() or "utf-8"
        stats.bytes_received += len(raw)
        stats.latencies_ms.append(int((time.monotonic() - started) * 1000))
        result.retrieved_at = utc_now()
        result.sha256 = hashlib.sha256(raw).hexdigest()
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        if "html" not in ctype.lower() and "xml" not in ctype.lower():
            result.error = f"unsupported content type: {ctype[:60]}"
            return result
        try:
            result.html = raw.decode(charset, errors="replace")
        except LookupError:
            result.html = raw.decode("utf-8", errors="replace")
        return result
    except urllib.error.HTTPError as exc:
        stats.latencies_ms.append(int((time.monotonic() - started) * 1000))
        result.status = exc.code
        result.final_url = exc.geturl() if hasattr(exc, "geturl") else None
        result.retrieved_at = utc_now()
        result.error = f"HTTP {exc.code}"
        return result
    except RobotsDisallowedRedirect as exc:
        result.retrieved_at = utc_now()
        result.blocked_by_robots = True
        result.error = str(exc.reason)
        return result
    except Exception as exc:  # network, TLS, timeout, redirect to non-public host
        stats.latencies_ms.append(int((time.monotonic() - started) * 1000))
        result.retrieved_at = utc_now()
        result.error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return result


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def visible_text(soup: BeautifulSoup) -> str:
    for node in soup(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    return _fold(soup.get_text(" "))


def orgnr_pattern(org: str) -> re.Pattern[str]:
    a, b, c = org[:3], org[3:6], org[6:]
    sep = r"[\s.\- ]?"
    return re.compile(rf"(?<!\d){a}{sep}{b}{sep}{c}(?!\d)")


LEGAL_FORM_TOKEN_PATTERNS = {
    "as": r"a\s?[./]?\s?s\.?",          # AS, A/S, A.S., A S
    "asa": r"a\s?[./]?\s?s\s?[./]?\s?a\.?",
    "ans": r"ans\.?",
    "da": r"d\.?\s?a\.?",
    "sa": r"s\.?\s?a\.?",
}


def legal_name_regex(name: str) -> re.Pattern[str] | None:
    """Exact legal name as a phrase: same tokens in the same order, case-insensitive, tolerant only to
    whitespace, '&'/'og', and the spelling of the legal-form suffix (AS vs A/S)."""
    folded = _fold(name)
    folded = re.sub(r"\bA\s*/\s*S\b", "AS", folded, flags=re.I)
    tokens = re.findall(r"[^\s]+", folded)
    if not tokens:
        return None
    pattern = ""
    for index, token in enumerate(tokens):
        low = token.casefold()
        if low in ("&", "og"):
            pattern += r"\s*(?:&|og|and)\s*"
            continue
        piece = LEGAL_FORM_TOKEN_PATTERNS[low] if index == len(tokens) - 1 and index > 0 and low in LEGAL_FORM_TOKEN_PATTERNS else re.escape(token)
        if pattern and not pattern.endswith(r"\s*"):
            pattern += r",?\s+"
        pattern += piece
    return re.compile(r"(?<![\w])" + pattern + r"(?![\w])", re.I)


def _span(text: str, start: int, end: int, pad: int = 60) -> str:
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


def find_identity(text: str, org: str, legal_name: str) -> dict[str, Any]:
    """Return evidence of exact org-number / exact legal-name presence and conflicting org numbers."""
    found: dict[str, Any] = {"orgnr_span": None, "name_span": None, "other_orgnrs": []}
    match = orgnr_pattern(org).search(text)
    if match:
        found["orgnr_span"] = _span(text, match.start(), match.end())
    name_re = legal_name_regex(legal_name)
    name_match = name_re.search(text) if name_re else None
    if name_match:
        found["name_span"] = _span(text, name_match.start(), name_match.end())
    others = set()
    for label in ORG_LABEL.finditer(text):
        digits = re.sub(r"\D", "", label.group(1))
        if len(digits) == 9 and digits != org:
            others.add(digits)
    found["other_orgnrs"] = sorted(others)
    return found


def _same_site_links(base_url: str, soup: BeautifulSoup, terms: tuple[str, ...], domain: str, limit: int) -> list[tuple[str, str]]:
    ranked: dict[str, tuple[int, str, str]] = {}
    base_parsed = urllib.parse.urlparse(base_url)
    base_key = (base_parsed.netloc.lower().removeprefix("www.") + (base_parsed.path.rstrip("/") or "/")).casefold()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or _registered_domain(url) != domain:
            continue
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|zip|docx?|xlsx?)$", parsed.path, re.I):
            continue
        anchor_text = _fold(anchor.get_text(" ")).casefold()
        haystack = (urllib.parse.unquote(parsed.path).casefold() + " " + anchor_text).replace("å", "a").replace("ø", "o").replace("æ", "ae")
        rank = next((index for index, term in enumerate(terms) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        key = (parsed.netloc.lower().removeprefix("www.") + (parsed.path.rstrip("/") or "/")).casefold()
        if key == base_key:
            continue
        if key not in ranked or rank < ranked[key][0]:
            ranked[key] = (rank, clean, anchor_text[:80])
    ordered = sorted(ranked.values(), key=lambda item: (item[0], len(item[1]), item[1]))
    return [(url, text) for _, url, text in ordered[:limit]]


def verify_registry_website(
    registry_url: str | None,
    *,
    org: str,
    legal_name: str,
    timeout: float = 10.0,
    max_bytes: int = 1_500_000,
    max_identity_pages: int = 3,
    check_careers: bool = True,
    name_proof_homepage_only: bool = False,
) -> tuple[dict[str, Any], SiteStats]:
    stats = SiteStats()
    supplied = str(registry_url or "").strip()
    normalized = normalize_homepage(supplied)
    outcome: dict[str, Any] = {
        "registry_url": supplied or None,
        "requested_url": normalized,
        "status": "not_listed" if not supplied else "invalid_url" if not normalized else "pending",
        "pages": [],
    }
    if not normalized:
        return outcome, stats

    home = fetch_page(normalized, timeout=timeout, max_bytes=max_bytes, stats=stats)
    explicitly_disallowed = home.blocked_by_robots and "not allowed" in (home.error or "") or "disallows" in (home.error or "")
    if home.html is None and not re.match(r"^https?://", supplied, re.I) and not explicitly_disallowed:
        alt = "http://" + supplied.lstrip("/")
        fallback = fetch_page(alt, timeout=timeout, max_bytes=max_bytes, stats=stats)
        if fallback.html is not None or fallback.blocked_by_robots:
            home = fallback
    outcome["homepage_fetch"] = {k: getattr(home, k) for k in ("url", "final_url", "status", "sha256", "retrieved_at", "error")}
    if home.blocked_by_robots:
        outcome["status"] = "blocked_robots"
        return outcome, stats
    if home.final_url and _registered_domain(home.final_url) in PARKING_DOMAINS:
        outcome["final_url"] = home.final_url
        outcome["status"] = "placeholder"
        outcome["parked_or_placeholder"] = True
        return outcome, stats
    if home.html is None:
        outcome["status"] = "unreachable"
        return outcome, stats

    final_url = home.final_url or home.url
    domain = _registered_domain(final_url)
    if domain in PARKING_DOMAINS:
        outcome.update({"final_url": final_url, "status": "placeholder", "parked_or_placeholder": True})
        return outcome, stats
    outcome["final_url"] = final_url
    outcome["registered_domain"] = domain
    outcome["redirected_off_domain"] = _registered_domain(normalized) != domain
    soup = BeautifulSoup(home.html, "lxml")
    title = _fold(soup.title.get_text(" ")) if soup.title else ""
    desc_tag = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
    description = _fold(str(desc_tag.get("content") or "")) if desc_tag else ""
    social = _social_links(final_url, soup)
    identity_links = _same_site_links(final_url, soup, IDENTITY_PAGE_TERMS, domain, max_identity_pages)
    careers_links = _same_site_links(final_url, soup, CAREERS_TERMS, domain, 2) if check_careers else []
    text = visible_text(soup)
    outcome["title"] = title[:300]
    outcome["description"] = description[:600]

    page_records = [{"url": final_url, "sha256": home.sha256, "retrieved_at": home.retrieved_at, "identity": find_identity(text, org, legal_name), "text_len": len(text)}]
    folded = text.casefold()
    parked = any(marker in folded for marker in PARKED_MARKERS) and len(text) < 3000
    outcome["parked_or_placeholder"] = parked

    def verified_by(records: list[dict[str, Any]]) -> dict[str, Any] | None:
        for kind in ("orgnr_span", "name_span"):
            for position, record in enumerate(records):
                if kind == "name_span" and name_proof_homepage_only and position > 0:
                    continue
                if record["identity"].get(kind):
                    return {"method": "organisation_number" if kind == "orgnr_span" else "exact_legal_name", "page": record["url"], "span": record["identity"][kind], "sha256": record["sha256"], "retrieved_at": record["retrieved_at"]}
        return None

    proof = verified_by(page_records)
    if not (proof and proof["method"] == "organisation_number") and not parked:
        for url, _ in identity_links:
            page = fetch_page(url, timeout=timeout, max_bytes=min(max_bytes, 1_000_000), stats=stats)
            if page.html is None or _registered_domain(page.final_url or url) != domain:
                continue
            if any(record["url"].rstrip("/") == (page.final_url or url).rstrip("/") for record in page_records):
                continue
            page_soup = BeautifulSoup(page.html, "lxml")
            social.extend(_social_links(page.final_url or url, page_soup))
            page_text = visible_text(page_soup)
            page_records.append({"url": page.final_url or url, "sha256": page.sha256, "retrieved_at": page.retrieved_at, "identity": find_identity(page_text, org, legal_name), "text_len": len(page_text)})
            proof = verified_by(page_records)
            if proof and proof["method"] == "organisation_number":
                break
    other_orgnrs = sorted({o for record in page_records for o in record["identity"]["other_orgnrs"]})
    orgnr_seen = any(record["identity"]["orgnr_span"] for record in page_records)
    outcome["pages"] = page_records
    outcome["other_organisation_numbers_on_site"] = other_orgnrs
    if parked:
        outcome["status"] = "placeholder"
    elif proof and proof["method"] == "organisation_number":
        outcome["status"] = "verified"
    elif proof and not other_orgnrs:
        outcome["status"] = "verified"
    elif proof and other_orgnrs and not orgnr_seen:
        # Exact name present but the site labels a *different* organisation number: parent/sister company risk.
        outcome["status"] = "conflicting_identity"
    else:
        outcome["status"] = "conflicting_identity" if other_orgnrs and not orgnr_seen else "unverified"
    outcome["verification"] = proof

    social_dedup = {(item["platform"], item["url"]): item for item in social}
    profile = {"name": legal_name, "organisation_number": org}
    assessed = [assess_social_identity(profile, item) for item in social_dedup.values()]
    outcome["social_links"] = sorted(
        [{"platform": item["platform"], "url": item["url"], "handle_matches_legal_name": item["publishable"]} for item in assessed],
        key=lambda item: (item["platform"], item["url"]),
    )

    careers: dict[str, Any] | None = None
    if outcome["status"] == "verified" and careers_links:
        url, anchor = careers_links[0]
        page = fetch_page(url, timeout=timeout, max_bytes=min(max_bytes, 1_000_000), stats=stats)
        if page.html is not None and page.status == 200 and _registered_domain(page.final_url or url) == domain:
            careers_soup = BeautifulSoup(page.html, "lxml")
            careers_title = _fold(careers_soup.title.get_text(" ")) if careers_soup.title else ""
            careers = {"url": page.final_url or url, "link_text": anchor, "title": careers_title[:200], "sha256": page.sha256, "retrieved_at": page.retrieved_at, "found_on": final_url}
        else:
            careers = {"url": url, "error": page.error or f"HTTP {page.status}", "found_on": final_url}
    outcome["careers"] = careers
    outcome["careers_links_checked"] = bool(check_careers)
    return outcome, stats


GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no", "msn.com",
    "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me", "mail.com", "gmx.com",
    "gmx.net", "online.no", "frisurf.no", "broadpark.no", "altibox.no", "getmail.no", "start.no", "c2i.net", "lyse.net",
    "epost.no", "chello.no", "tele2.no", "telia.no", "ebnett.no", "bluezone.no", "enivest.net", "nextgentel.com", "haugnett.no",
    "eidsiva.net", "vikenfiber.no", "tussa.com", "netcom.no", "post.no", "posten.no", "spray.no", "sol.no", "kpnmail.no",
}


def email_domain_candidate(email: str | None) -> str | None:
    """A company-specific domain from the registry e-mail address, or None for free/ISP mail domains."""
    match = re.fullmatch(r"[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", str(email or "").strip())
    if not match:
        return None
    domain = _registered_domain("https://" + match.group(1).lower())
    if not domain or domain in GENERIC_EMAIL_DOMAINS:
        return None
    return domain
