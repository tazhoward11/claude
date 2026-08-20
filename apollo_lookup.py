#!/usr/bin/env python3
"""
Apollo.io lead lookup: given company name(s), find two tiers of contacts per
company - decision-makers and marketing/champion contacts - enrich a small
sample for verified emails, and infer each company's email format
(e.g. first.last@domain.com) from the verified results.

Decision-maker and marketing tiers are matched by Apollo's normalized
seniority bands (owner, founder, c_suite, partner, vp, head, director,
manager, ...) rather than exact title strings, since exact titles vary
wildly by industry (e.g. "First Vice President" in banking).

API key is read from the APOLLO_API_KEY environment variable, or --api-key.
Never hardcode the key in this file or in shell history.

Usage:
    export APOLLO_API_KEY="..."
    python3 apollo_lookup.py "Acme Corp"
    python3 apollo_lookup.py --companies-file companies.txt --out leads.csv
    python3 apollo_lookup.py "Acme Corp" --decision-seniorities "owner,founder,c_suite,partner,vp"
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

API_BASE = "https://api.apollo.io/v1"

DEFAULT_LOCATIONS = [
    # I-35 corridor: San Marcos to Waco
    "San Marcos, Texas", "Kyle, Texas", "Buda, Texas", "Austin, Texas",
    "Round Rock, Texas", "Pflugerville, Texas", "Cedar Park, Texas",
    "Georgetown, Texas", "Taylor, Texas", "Hutto, Texas", "Temple, Texas",
    "Belton, Texas", "Killeen, Texas", "Waco, Texas",
    # ~50mi west
    "Dripping Springs, Texas", "Wimberley, Texas", "Marble Falls, Texas",
    "Burnet, Texas", "Lampasas, Texas", "Gatesville, Texas",
    # ~50mi east
    "Bastrop, Texas", "Elgin, Texas", "Smithville, Texas", "Lockhart, Texas",
    "Luling, Texas", "Rockdale, Texas", "Cameron, Texas",
]

# Decision-maker tier deliberately leaves "vp" out by default: VP is a genuinely
# senior title at a small business or tech company, but at a bank or large
# enterprise it can be a mid-level individual contributor (e.g. Frost Bank has
# dozens of "Assistant Vice President" / "Vice President" loan officers). Add
# "vp" back in per-campaign with --decision-seniorities where it's appropriate.
DEFAULT_DECISION_SENIORITIES = ["owner", "founder", "c_suite", "partner", "head"]
DEFAULT_MARKETING_TITLES = ["marketing", "brand", "communications", "chief marketing officer"]
DEFAULT_MARKETING_SENIORITIES = ["c_suite", "head", "director", "manager"]
DEFAULT_RECRUITING_TITLES = ["recruiting", "talent acquisition", "talent", "people", "human resources",
                              "chief people officer", "chief human resources officer"]
DEFAULT_RECRUITING_SENIORITIES = ["c_suite", "head", "director", "vp", "manager"]
# Gatekeepers: often the actual path to a decision-maker's calendar. Deliberately no
# seniority filter - Apollo tags these as low formal seniority despite real access.
DEFAULT_GATEKEEPER_TITLES = ["executive assistant", "assistant to the ceo", "assistant to the president",
                             "chief of staff", "office manager"]

SEARCH_URL = f"{API_BASE}/mixed_people/api_search"
MATCH_URL = f"{API_BASE}/people/match"

SESSION = requests.Session()

# Persistent credit ledger - lives next to this script so it survives across
# runs/sessions. Apollo's API exposes no usage endpoint, so this is a
# self-maintained estimate: it logs every /people/match attempt (successful
# or not - Apollo counts "unavailable" results too), and treats a person as
# a NEW credit spend only the first time their Apollo id is ever seen here,
# since re-revealing an already-revealed person is free. Cross-check against
# your actual Apollo dashboard periodically - this is an estimate, not a bill.
CREDIT_LOG_PATH = Path(__file__).parent / "apollo_credit_log.csv"
CREDIT_LOG_FIELDS = ["timestamp", "company", "person_id", "name", "title", "email_status", "new_credit_spend"]

# Below this many total candidates found for a company, --dry-run recommends
# just grabbing the top person(s) rather than chasing a bigger sample - a
# tiny Apollo footprint usually means the company isn't really Austin-based
# or is small enough that only the top decision-maker matters.
SMALL_FOOTPRINT_THRESHOLD = 20


def api_headers(api_key: str) -> dict:
    return {"Content-Type": "application/json", "x-api-key": api_key}


CORPORATE_STOPWORDS = {
    "capital", "partners", "group", "llc", "inc", "management", "ventures", "fund",
    "advisors", "adviser", "advisers", "company", "co", "the", "of", "private", "wealth",
    "financial", "holdings", "associates", "global", "solutions", "consulting", "strategies",
}


def org_similarity(queried: str, employer_name: str) -> float:
    """0-1 word-overlap similarity between a queried company name and a person's
    actual employer name (free in the search response). Used to guard against a
    fallback candidate that degraded to something generic (e.g. '1st', 'Austin',
    'Peak', 'Sunny') and matched a wholly unrelated company by accident."""
    if not employer_name:
        return 0.0
    q_words = set(re.findall(r"[a-z0-9]+", queried.lower()))
    e_words = set(re.findall(r"[a-z0-9]+", employer_name.lower()))
    q_sig = q_words - CORPORATE_STOPWORDS or q_words
    e_sig = e_words - CORPORATE_STOPWORDS or e_words
    if not q_sig or not e_sig:
        return 0.0
    return len(q_sig & e_sig) / len(q_sig | e_sig)


def search_people_with_fallback(api_key: str, company: str, locations: list[str], max_pages: int,
                                 titles: list[str] | None = None, seniorities: list[str] | None = None,
                                 include_similar_titles: bool = True,
                                 domain: str | None = None) -> tuple[list[dict], str]:
    """Apollo's org name match isn't fuzzy against extra words (e.g. 'Acme Capital'
    may not match an org listed as just 'Acme'). Retry with trailing words dropped
    until something matches. Returns (people, name_that_matched).

    Danger: dropping enough words can degrade to something dangerously generic
    ('1st Commercial Credit' -> '1st', 'Sunny River Management' -> 'Sunny',
    'Peak Rock Capital' -> 'Peak'), and a generic candidate can even collide with
    a real but unrelated company sharing a word (e.g. 'Barton Creek Equity
    Partners' vs. the unrelated 'Omni Barton Creek' golf resort - both genuinely
    contain "Barton Creek"). To guard against this, results are grouped by the
    person's actual employer name and only people at the SINGLE
    highest-similarity employer are kept - loosely-related runners-up are
    dropped rather than merged in, so a coincidental word match can't sneak in
    alongside the real target.

    If `domain` is given (a company's real domain, already known from elsewhere),
    fuzzy name matching is skipped entirely and results are keyed off the domain
    directly - unambiguous, no similarity scoring needed."""
    if domain:
        people = search_people(api_key, company, locations, titles=titles, seniorities=seniorities,
                                include_similar_titles=include_similar_titles, max_pages=max_pages, domain=domain)
        return people, f"{company} (pinned to {domain})"

    words = company.split()
    candidates = [company] + [" ".join(words[:i]) for i in range(len(words) - 1, 0, -1)]
    for candidate in candidates:
        people = search_people(api_key, candidate, locations, titles=titles, seniorities=seniorities,
                                include_similar_titles=include_similar_titles, max_pages=max_pages)
        if not people:
            continue
        by_employer = defaultdict(list)
        for p in people:
            employer = (p.get("organization") or {}).get("name") or ""
            by_employer[employer].append(p)
        scored = sorted(((org_similarity(company, name), name) for name in by_employer),
                         key=lambda x: x[0], reverse=True)
        best_score, best_name = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else -1.0
        # Require real confidence: a decent absolute score, AND (if there's more than
        # one distinct employer in the results) a clear margin over the runner-up.
        # Some real, unrelated companies genuinely share a word (e.g. "Barton Creek
        # Equity Partners" vs. the unrelated "Barton Creek Golf Academy" - both
        # contain "Barton Creek" and neither contains "Equity"), so a tie or
        # near-tie means the name alone can't distinguish them - refuse to guess
        # rather than silently pick one at random.
        if best_score < 0.4 or (len(scored) > 1 and best_score - second_score < 0.15):
            if best_score > 0:
                print(f"  '{candidate}' returned {len(scored)} different employer(s), none clearly "
                      f"'{company}' (best: '{best_name}' @ {best_score:.2f}"
                      + (f", runner-up: '{scored[1][1]}' @ {second_score:.2f}" if len(scored) > 1 else "")
                      + ") - ambiguous, rejecting, trying next fallback")
            continue
        matched_people = by_employer[best_name]
        if candidate != company:
            print(f"  no results for '{company}', falling back to '{candidate}'")
        dropped = len(people) - len(matched_people)
        if dropped:
            print(f"  kept {len(matched_people)} result(s) at employer '{best_name}' "
                  f"(similarity {best_score:.2f}); dropped {dropped} at other employer(s)")
        return matched_people, candidate
    return [], company


def search_people(api_key: str, company: str, locations: list[str], titles: list[str] | None = None,
                   seniorities: list[str] | None = None, include_similar_titles: bool = True,
                   per_page: int = 25, max_pages: int = 4, domain: str | None = None) -> list[dict]:
    results = []
    page = 1
    while page <= max_pages:
        payload = {
            "person_locations": locations,
            "page": page,
            "per_page": per_page,
        }
        if domain:
            payload["q_organization_domains_list"] = [domain]
        else:
            payload["q_organization_name"] = company
        if titles:
            payload["person_titles"] = titles
            payload["include_similar_titles"] = include_similar_titles
        if seniorities:
            payload["person_seniorities"] = seniorities
        resp = SESSION.post(SEARCH_URL, headers=api_headers(api_key), json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  [search] {company}: HTTP {resp.status_code} - {resp.text}", file=sys.stderr)
            break
        data = resp.json()
        people = data.get("people", [])
        results.extend(people)
        total_pages = data.get("pagination", {}).get("total_pages", 1)
        if page >= total_pages or not people:
            break
        page += 1
        time.sleep(0.3)
    return results


def search_company_tiers(api_key: str, company: str, locations: list[str], max_pages: int,
                          tiers: dict, domain: str | None = None) -> tuple[list[dict], str]:
    """Run each tier's search separately and merge, deduped by Apollo person id.
    Each person is tagged with which tier(s) matched them."""
    combined, seen, matched_as = [], {}, company
    for tier_name, cfg in tiers.items():
        people, matched_as = search_people_with_fallback(
            api_key, company, locations, max_pages,
            titles=cfg.get("titles"), seniorities=cfg.get("seniorities"),
            include_similar_titles=cfg.get("include_similar_titles", True), domain=domain)
        for p in people:
            pid = p.get("id")
            if pid in seen:
                existing = seen[pid]
                if tier_name not in existing["_tiers"]:
                    existing["_tiers"].append(tier_name)
                continue
            p["_tiers"] = [tier_name]
            seen[pid] = p
            combined.append(p)
    return combined, matched_as


def load_previously_revealed_ids() -> set:
    """Person ids this tool has ever attempted to enrich, from the persistent
    ledger. Re-revealing one of these is free in Apollo, so it shouldn't count
    as a new credit spend."""
    if not CREDIT_LOG_PATH.exists():
        return set()
    with open(CREDIT_LOG_PATH, newline="") as f:
        return {row["person_id"] for row in csv.DictReader(f) if row.get("person_id")}


def log_enrichment_attempt(company: str, person: dict, enriched: dict | None, is_new: bool):
    is_new_file = not CREDIT_LOG_PATH.exists()
    with open(CREDIT_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CREDIT_LOG_FIELDS)
        if is_new_file:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company": company,
            "person_id": person.get("id") or "",
            "name": (enriched or {}).get("name") or person.get("first_name") or "",
            "title": person.get("title") or "",
            "email_status": (enriched or {}).get("email_status") or "no_response",
            "new_credit_spend": "yes" if is_new else "no (already revealed)",
        })


def enrich_person(api_key: str, person: dict, company: str, revealed_ids: set) -> dict | None:
    payload = {"id": person.get("id")} if person.get("id") else {
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name_obfuscated", "").split("*")[0] or None,
        "organization_name": (person.get("organization") or {}).get("name"),
    }
    resp = SESSION.post(MATCH_URL, headers=api_headers(api_key), json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  [enrich] {payload}: HTTP {resp.status_code} - {resp.text}", file=sys.stderr)
        return None
    enriched = resp.json().get("person")
    pid = person.get("id")
    is_new = pid not in revealed_ids
    log_enrichment_attempt(company, person, enriched, is_new)
    if pid:
        revealed_ids.add(pid)
    return enriched


EMAIL_RE = re.compile(r"^([^@]+)@(.+)$")


def guess_format(local_part: str, first: str, last: str) -> str:
    lp = local_part.lower()
    f, l = (first or "").lower(), (last or "").lower()
    if not f or not l:
        return "unknown"
    templates = {
        f"{f}.{l}": "first.last",
        f"{f}{l}": "firstlast",
        f"{f[0]}{l}": "flast",
        f"{f}{l[0]}": "firstl",
        f"{f}_{l}": "first_last",
        f"{f[0]}.{l}": "f.last",
        f"{l}.{f}": "last.first",
        f"{l}{f[0]}": "lastf",
        f: "first",
        l: "last",
    }
    return templates.get(lp, f"other ({lp})")


MASK_RE = re.compile(r"^([^*]*)\*+([^*]*)$")


def mask_matches(candidate_last: str, masked: str) -> bool:
    """Check a candidate last name against Apollo's obfuscated form, e.g. 'Mu***l'.
    Note: the star count is fixed-width and does NOT encode the real name's length
    (a 4-letter and a 9-letter last name both mask to e.g. 'Xx***x') so length can't
    be used as a signal - only the prefix/suffix around the stars."""
    m = MASK_RE.match(masked or "")
    if not m:
        return candidate_last.strip().lower() == (masked or "").strip().lower()
    prefix, suffix = m.group(1).lower(), m.group(2).lower()
    cl = candidate_last.strip().lower()
    if len(cl) < len(prefix) + len(suffix):
        return False
    return cl.startswith(prefix) and cl.endswith(suffix)


def title_similarity(a: str, b: str) -> float:
    """Word-overlap similarity between two job titles, 0-1. Used only to break ties
    when multiple people share a first name + last-name mask at the same company."""
    words_a = set(re.findall(r"[a-z]+", (a or "").lower()))
    words_b = set(re.findall(r"[a-z]+", (b or "").lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def resolve_candidate(candidate: dict, apollo_people: list[dict]) -> tuple[dict | None, str]:
    """Match a (first, last, title) candidate sourced from outside Apollo (e.g. a
    company team page) against this company's actual obfuscated search results.
    Returns (matched_apollo_person_or_None, reason). Refuses to guess between two
    real people who both fit the same first-name + mask (e.g. Joe Johnson vs.
    Joe Johnston both match 'Jo***n') unless title breaks the tie clearly."""
    first, last, title = candidate["first"], candidate["last"], candidate.get("title", "")
    hits = [p for p in apollo_people
            if (p.get("first_name") or "").strip().lower() == first.strip().lower()
            and mask_matches(last, p.get("last_name_obfuscated") or "")]
    if not hits:
        return None, "no Apollo record matches this name/mask at this company - not cross-validated"
    if len(hits) == 1:
        return hits[0], "unique match on first name + last-name mask"
    scored = sorted(((title_similarity(title, h.get("title") or ""), h) for h in hits),
                     key=lambda x: x[0], reverse=True)
    if len(scored) >= 2 and scored[0][0] - scored[1][0] >= 0.3 and scored[0][0] > 0:
        return scored[0][1], f"ambiguous ({len(hits)} people share this name+mask); resolved by title match"
    names = ", ".join(f"{h.get('first_name')} {h.get('last_name_obfuscated')} ({h.get('title')})" for h in hits)
    return None, f"AMBIGUOUS - {len(hits)} different people match ({names}); title didn't disambiguate, skipped"


def apply_known_format(first: str, last: str, domain: str, fmt: str) -> str | None:
    """Build a predicted email for a known full name using an already-confirmed format."""
    f, l = (first or "").strip().lower(), (last or "").strip().lower()
    if not f or not l or not domain:
        return None
    builders = {
        "first.last": f"{f}.{l}", "firstlast": f"{f}{l}", "flast": f"{f[0]}{l}",
        "firstl": f"{f}{l[0]}", "first_last": f"{f}_{l}", "f.last": f"{f[0]}.{l}",
        "last.first": f"{l}.{f}", "lastf": f"{l}{f[0]}", "first": f, "last": l,
    }
    local = builders.get(fmt)
    return f"{local}@{domain}" if local else None


def run(companies: list[str], tiers: dict, locations: list[str], api_key: str,
        enrich: bool, out_path: str, max_pages: int, sample_size: int, known_names_path: str | None,
        company_domains_path: str | None = None):
    all_rows = []
    known_names = defaultdict(list)  # company -> [{"first", "last", "title"}, ...] supplied by the user
    if known_names_path:
        with open(known_names_path, newline="") as f:
            for parts in csv.reader(f):
                parts = [p.strip() for p in parts]
                if len(parts) >= 3:
                    known_names[parts[0]].append({
                        "first": parts[1], "last": parts[2],
                        "title": parts[3] if len(parts) > 3 else "",
                    })

    company_domains = {}  # company -> domain, for pinning past ambiguous name matches
    if company_domains_path:
        with open(company_domains_path, newline="") as f:
            for parts in csv.reader(f):
                parts = [p.strip() for p in parts]
                if len(parts) >= 2 and parts[1]:
                    company_domains[parts[0]] = parts[1]

    revealed_ids = load_previously_revealed_ids()
    run_new_spend = 0

    for company in companies:
        print(f"Searching: {company}")
        domain_pin = company_domains.get(company)
        people, matched_as = search_company_tiers(api_key, company, locations, max_pages, tiers, domain=domain_pin)
        tier_counts = {t: sum(1 for p in people if t in p["_tiers"]) for t in tiers}
        breakdown = ", ".join(f"{t}={n}" for t, n in tier_counts.items())
        print(f"  found {len(people)} unique match(es) across tiers: {breakdown}")

        confirmed_domain = None
        confirmed_format = None
        format_domain_votes = Counter()  # (fmt, domain) -> count - see note below
        enriched_count = 0
        person_rows = []  # (person, row) so free-name people can be filled in after format is confirmed

        for p in people:
            last_masked = p.get("last_name_obfuscated") or ""
            is_unmasked = last_masked and "*" not in last_masked
            row = {
                "company": company, "matched_as": matched_as,
                "tier": "+".join(p.get("_tiers", [])),
                "name": f"{p.get('first_name', '')} {last_masked}".strip(),
                "title": p.get("title"), "linkedin_url": p.get("linkedin_url"),
                "email": "", "email_status": "", "guessed_format": "", "note": "",
            }

            if enrich and confirmed_format is None and enriched_count < sample_size:
                was_new = p.get("id") not in revealed_ids
                enriched = enrich_person(api_key, p, company, revealed_ids)
                enriched_count += 1
                if was_new:
                    run_new_spend += 1
                time.sleep(0.3)
                if enriched:
                    row["name"] = enriched.get("name") or row["name"]
                    row["email"] = enriched.get("email") or ""
                    row["email_status"] = enriched.get("email_status") or ""
                    if row["email"] and row["email_status"] == "verified":
                        m = EMAIL_RE.match(row["email"])
                        if m:
                            local, domain = m.groups()
                            fmt = guess_format(local, enriched.get("first_name"), enriched.get("last_name"))
                            row["guessed_format"] = fmt
                            # Vote on (format, domain) together, not format alone: a company
                            # can have enrichments land on two different real domains (e.g. an
                            # "Operating Partner" whose Apollo profile is tied to a portfolio
                            # company's domain, not the fund's own domain), and those two
                            # unrelated domains can coincidentally produce the same-looking
                            # format (e.g. both happen to be bare-last-name). Voting on format
                            # alone would let that coincidence "confirm" a format for the wrong
                            # domain. Only count it confirmed once the SAME domain backs the
                            # SAME format twice.
                            #
                            # Exception: if a domain was explicitly pinned (--company-domains),
                            # that's a stronger signal than a vote count - Apollo's own domain
                            # filter isn't airtight (an "Operating Partner" can still surface
                            # tied to an affiliated portfolio company's domain despite the pin),
                            # so a vote for any OTHER domain shouldn't be allowed to manufacture
                            # a false tie against the domain the user already told us is correct.
                            if domain_pin and domain != domain_pin:
                                row["note"] = (f"verified but domain '{domain}' doesn't match pinned "
                                               f"'{domain_pin}' - excluded from format voting")
                            elif not fmt.startswith("other") and fmt != "unknown":
                                key = (fmt, domain)
                                format_domain_votes[key] += 1
                                if format_domain_votes[key] >= min(2, sample_size):
                                    confirmed_format, confirmed_domain = fmt, domain
            elif is_unmasked:
                # Apollo didn't obfuscate this one - full name is already free from
                # search. Don't spend a credit; fill in a predicted email below once
                # the company's format is confirmed.
                row["note"] = "name not obfuscated by Apollo (free); email pending format confirmation"
            else:
                row["note"] = "not enriched (sample budget spent) - last name obfuscated, real email unknown"

            all_rows.append(row)
            person_rows.append((p, row))

        if not confirmed_format and format_domain_votes:
            top_count = max(format_domain_votes.values())
            top_keys = [k for k, v in format_domain_votes.items() if v == top_count]
            if len(top_keys) == 1:
                confirmed_format, confirmed_domain = top_keys[0]
            else:
                # Genuine tie between different (format, domain) combos - e.g. sample
                # landed on two different real domains with no majority. Picking one
                # would just be an arbitrary guess dressed up as a confirmed result.
                print(f"  sample split evenly across {len(top_keys)} different (format, domain) "
                      f"combos with no majority: {top_keys} - refusing to guess which is real")

        status = (f"format confirmed: {confirmed_format} @ {confirmed_domain}"
                  if confirmed_format else "could not confirm a format from sample")
        print(f"  {status} (spent {enriched_count} enrichment credit(s) on this company)")

        # Fill in free predicted emails for anyone Apollo left unobfuscated, now that
        # the format is known - no known-names file needed, no credit spent.
        if confirmed_format and confirmed_domain:
            for p, row in person_rows:
                if row["email"]:
                    continue
                last_masked = p.get("last_name_obfuscated") or ""
                if last_masked and "*" not in last_masked:
                    predicted = apply_known_format(p.get("first_name"), last_masked, confirmed_domain, confirmed_format)
                    if predicted:
                        row["email"] = predicted
                        row["email_status"] = "predicted (not verified)"
                        row["guessed_format"] = confirmed_format
                        row["note"] = "name not obfuscated by Apollo, format applied for free, no credit spent"

        # Apply the confirmed format to any full names you already know, for free -
        # but only after cross-validating each one against Apollo's own obfuscated
        # records for this company, so lookalike names (Johnson vs. Johnston) can't
        # get silently mismatched to the wrong person.
        if confirmed_format and confirmed_domain:
            for candidate in known_names.get(company, []):
                matched_person, reason = resolve_candidate(candidate, people)
                row = {
                    "company": company, "matched_as": matched_as, "tier": "known_names",
                    "name": f"{candidate['first']} {candidate['last']}",
                    "title": candidate.get("title", ""), "linkedin_url": "",
                    "email": "", "email_status": "", "guessed_format": "",
                }
                if matched_person is None:
                    row["email_status"] = "skipped"
                    row["note"] = reason
                else:
                    predicted = apply_known_format(candidate["first"], candidate["last"],
                                                    confirmed_domain, confirmed_format)
                    row["email"] = predicted or ""
                    row["email_status"] = "predicted (not verified)"
                    row["guessed_format"] = confirmed_format
                    row["note"] = f"from --known-names, no credit spent - {reason}"
                all_rows.append(row)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["company", "matched_as", "tier", "name", "title", "linkedin_url",
                                                "email", "email_status", "guessed_format", "note"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} rows to {out_path}")

    if enrich:
        lifetime_spend = sum(1 for row in csv.DictReader(open(CREDIT_LOG_PATH)) if row["new_credit_spend"] == "yes") \
            if CREDIT_LOG_PATH.exists() else 0
        print(f"\nCredits: ~{run_new_spend} new this run, ~{lifetime_spend} lifetime total via this tool "
              f"(estimate only - ledger at {CREDIT_LOG_PATH}; check your Apollo dashboard for the real number)")


def run_dry_run_triage(companies: list[str], tiers: dict, locations: list[str], api_key: str,
                        max_pages: int, out_path: str, small_threshold: int,
                        company_domains_path: str | None = None):
    """Free search-only pass (no enrichment, no credits) across a company list.
    Categorizes each company by how many candidates Apollo actually has, and
    recommends how many credits are worth spending - so you can approve an
    actual number before anything gets charged, instead of guessing upfront."""
    company_domains = {}
    if company_domains_path:
        with open(company_domains_path, newline="") as f:
            for parts in csv.reader(f):
                parts = [p.strip() for p in parts]
                if len(parts) >= 2 and parts[1]:
                    company_domains[parts[0]] = parts[1]

    rows = []
    counts = Counter()
    recommended_total = 0

    for company in companies:
        print(f"Checking: {company}")
        domain_pin = company_domains.get(company)
        people, matched_as = search_company_tiers(api_key, company, locations, max_pages, tiers, domain=domain_pin)
        tier_counts = {t: sum(1 for p in people if t in p["_tiers"]) for t in tiers}
        total = len(people)

        if total == 0:
            category, recommended = "zero", 0
        elif total < small_threshold:
            category, recommended = "small", 1
        else:
            category, recommended = "large", 2
        counts[category] += 1
        recommended_total += recommended

        breakdown = ", ".join(f"{t}={n}" for t, n in tier_counts.items())
        print(f"  {total} candidate(s) ({breakdown}) -> {category}, recommend {recommended} credit(s)")

        row = {"company": company, "matched_as": matched_as, "total_candidates": total,
               "category": category, "recommended_credits": recommended}
        row.update({f"{t}_count": tier_counts.get(t, 0) for t in tiers})
        rows.append(row)

    fieldnames = ["company", "matched_as", "total_candidates"] + \
                 [f"{t}_count" for t in tiers] + ["category", "recommended_credits"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n=== Triage summary (wrote {out_path}) ===")
    print(f"  {counts['zero']} companies: zero candidates found - need manual domain research, 0 credits")
    print(f"  {counts['small']} companies: small footprint (<{small_threshold}) - recommend 1 credit each "
          f"(just grab the top person) = {counts['small']} credit(s)")
    print(f"  {counts['large']} companies: larger footprint (>={small_threshold}) - recommend 2 credits + free "
          f"web lookup each = {counts['large'] * 2} credit(s)")
    print(f"  Estimated total for this batch: {recommended_total} credit(s) "
          f"(nothing has been spent yet - this was a free search-only pass)")


JUNK_EMAIL = re.compile(r"(\.png|\.jpe?g|\.gif|\.svg|\.webp|@2x|sentry|wixpress|example\.com|"
                         r"user@domain|yourname|godaddy|squarespace|latinotype|sentry\.io)", re.I)


def scrape_site_emails(domain: str) -> list[str]:
    """Last-resort fallback for companies Apollo has no people for at all.
    Small owner-operated businesses usually publish a contact address on their
    own site; a general inbox at a 1-3 person shop is typically read by the
    owner, which beats having nothing."""
    found = set()
    for path in ("", "/contact", "/contact-us", "/about"):
        for scheme in ("https://", "https://www."):
            try:
                r = SESSION.get(f"{scheme}{domain}{path}", timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                for e in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", r.text):
                    if not JUNK_EMAIL.search(e) and len(e) < 60:
                        found.add(e.lower())
                break
            except Exception:
                continue
    # prefer addresses on the company's own domain
    own = [e for e in found if e.endswith("@" + domain.lower())]
    return sorted(own) or sorted(found)


def neverbounce_check(email: str, nb_key: str) -> str:
    """Returns valid / invalid / catchall / unknown. Costs 1 NeverBounce credit."""
    try:
        r = SESSION.get("https://api.neverbounce.com/v4/single/check",
                        params={"key": nb_key, "email": email, "address_info": 0,
                                "credits_info": 0, "timeout": 20}, timeout=45)
        return r.json().get("result", "error")
    except Exception:
        return "error"


def run_targets(targets_path: str, api_key: str, out_path: str, nb_key: str | None,
                 no_enrich: bool = False):
    """Enrich a specific list of named people rather than discovering them.

    Input CSV: company,domain,first,last[,title]

    Built because the discovery path (search a company, pick who looks relevant)
    is the wrong shape when you already know exactly who you want. Matches each
    person against Apollo's obfuscated last name before spending a credit, so a
    credit never lands on the wrong person. Once any address at a domain is
    verified, its format is applied for free to that domain's remaining targets."""
    rows = []
    with open(targets_path, newline="") as f:
        for p in csv.reader(f):
            p = [x.strip() for x in p]
            if len(p) >= 4 and p[0].lower() != "company":
                rows.append({"company": p[0], "domain": p[1], "first": p[2], "last": p[3],
                             "title": p[4] if len(p) > 4 else ""})
    print(f"{len(rows)} target(s) across {len(set(r['domain'] for r in rows))} domain(s)")

    revealed = load_previously_revealed_ids()
    people_cache: dict = {}
    fmt_by_domain: dict = {}
    spent = 0
    out = []

    for r in rows:
        dom, fn, ln = r["domain"], r["first"], r["last"]
        if dom not in people_cache:
            people_cache[dom] = search_people(api_key, r["company"], [], max_pages=1, domain=dom)
        pool = people_cache[dom]
        hits = [p for p in pool
                if (p.get("first_name") or "").strip().lower() == fn.lower()
                and mask_matches(ln, p.get("last_name_obfuscated") or "")]

        row = dict(r, email="", email_status="", source="", note="")
        if len(hits) == 1 and not no_enrich:
            p = hits[0]
            was_new = p.get("id") not in revealed
            e = enrich_person(api_key, p, r["company"], revealed)
            if was_new:
                spent += 1
            time.sleep(0.3)
            em = (e or {}).get("email") or ""
            row["email"] = em
            row["email_status"] = (e or {}).get("email_status") or "no email on file"
            row["source"] = "apollo (credit spent)"
            row["title"] = p.get("title") or r["title"]
            if em and row["email_status"] == "verified" and "@" in em:
                local, d = em.split("@", 1)
                f2 = guess_format(local, fn, ln)
                if not f2.startswith("other") and f2 != "unknown":
                    fmt_by_domain.setdefault(d, f2)
        elif len(hits) > 1:
            row["note"] = f"AMBIGUOUS - {len(hits)} people match {fn} {ln}, skipped"
        elif not pool:
            row["note"] = "no Apollo record at this domain"
        else:
            row["note"] = f"{len(pool)} people at domain but no match for {fn} {ln}"
        out.append(row)

    # free pass: apply a confirmed domain format to targets we could not enrich
    for row in out:
        if row["email"]:
            continue
        fmt = fmt_by_domain.get(row["domain"])
        if fmt:
            pred = apply_known_format(row["first"], row["last"], row["domain"], fmt)
            if pred:
                row.update(email=pred, email_status="predicted (not verified)",
                           source=f"format '{fmt}' confirmed at this domain, no credit")

    # website fallback for domains where Apollo knows nobody
    for dom in {r["domain"] for r in out if not r["email"] and not people_cache.get(r["domain"])}:
        for e in scrape_site_emails(dom)[:1]:
            for row in out:
                if row["domain"] == dom and not row["email"]:
                    row.update(email=e, email_status="general inbox",
                               source="scraped from company website, no credit")

    if nb_key:
        # Last resort: for anyone still with no address, guess the usual patterns and
        # let NeverBounce say which mailbox actually exists. Only worth trying on small
        # business domains - a corporate catchall accepts every guess and proves nothing,
        # so a catchall hit is reported as such rather than treated as a find.
        for row in out:
            if row["email"]:
                continue
            f, l, d = row["first"].lower(), row["last"].lower(), row["domain"]
            for cand in (f, f"{f[0]}{l}", f"{f}.{l}", f"{f}{l}"):
                res = neverbounce_check(f"{cand}@{d}", nb_key)
                time.sleep(0.15)
                if res == "valid":
                    row.update(email=f"{cand}@{d}", email_status="found by pattern test",
                               source="NeverBounce pattern test, no Apollo credit")
                    break
                if res == "catchall":
                    row["note"] = (row["note"] + " | " if row["note"] else "") + \
                        f"{d} is catchall - patterns cannot be tested"
                    break

        print("verifying with NeverBounce...")
        for row in out:
            if row["email"] and row["email_status"] not in ("verified", "found by pattern test"):
                row["nb_result"] = neverbounce_check(row["email"], nb_key)
                time.sleep(0.15)
            elif row["email"]:
                row["nb_result"] = "n/a (already confirmed)"
            else:
                row["nb_result"] = ""

    fields = ["company", "domain", "first", "last", "title", "email", "email_status", "source", "note"]
    if nb_key:
        fields.append("nb_result")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)

    got = sum(1 for r in out if r["email"])
    print(f"\n{got}/{len(out)} have an address | {spent} Apollo credit(s) spent")
    for r in out:
        tag = r.get("nb_result") or r["email_status"]
        print(f"  {r['first']} {r['last']:14s} {r['email'] or '-':40s} {tag}")
    print(f"\nWrote {out_path}")


def csv_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("company", nargs="?", help="Single company name")
    src.add_argument("--companies-file", help="Path to a text file, one company name per line")
    src.add_argument("--targets", help="Path to a CSV (company,domain,first,last[,title]) of specific "
                                        "people to enrich. Use this when you already know exactly who you "
                                        "want, instead of discovering people by company. Each person is "
                                        "matched against Apollo's obfuscated last name before a credit is "
                                        "spent, a confirmed domain format is applied free to that domain's "
                                        "other targets, and companies Apollo has no record of fall back to "
                                        "scraping the company website for a published address.")

    parser.add_argument("--decision-seniorities", default=",".join(DEFAULT_DECISION_SENIORITIES),
                         help="Comma-separated Apollo seniority bands for the decision-maker tier. "
                              "Valid values: owner,founder,c_suite,partner,vp,head,director,manager,senior,"
                              "entry,intern. Pass '' to disable this tier's seniority filter.")
    parser.add_argument("--decision-titles", default="",
                         help="Optional explicit job titles for the decision-maker tier, combined (AND) with "
                              "--decision-seniorities if both are set")
    parser.add_argument("--marketing-titles", default=",".join(DEFAULT_MARKETING_TITLES),
                         help="Comma-separated title keywords for the marketing/champion tier")
    parser.add_argument("--marketing-seniorities", default=",".join(DEFAULT_MARKETING_SENIORITIES),
                         help="Comma-separated Apollo seniority bands for the marketing tier")
    parser.add_argument("--recruiting-titles", default=",".join(DEFAULT_RECRUITING_TITLES),
                         help="Comma-separated title keywords for the recruiting/talent tier (Head of Talent, "
                              "Chief People Officer, Talent Acquisition, HR leadership). Pass '' to disable.")
    parser.add_argument("--recruiting-seniorities", default=",".join(DEFAULT_RECRUITING_SENIORITIES),
                         help="Comma-separated Apollo seniority bands for the recruiting/talent tier")
    parser.add_argument("--gatekeeper-titles", default=",".join(DEFAULT_GATEKEEPER_TITLES),
                         help="Comma-separated title keywords for the gatekeeper tier (EAs, chiefs of staff, "
                              "office managers) - often the real path to a decision-maker's calendar. "
                              "Pass '' to disable this tier.")
    parser.add_argument("--no-similar-titles", action="store_true",
                         help="Disable Apollo's automatic expansion to similar job titles")

    parser.add_argument("--locations", help="Comma-separated locations; defaults to the San Marcos-Waco corridor")
    parser.add_argument("--api-key", default=os.environ.get("APOLLO_API_KEY"),
                         help="Apollo API key (defaults to APOLLO_API_KEY env var)")
    parser.add_argument("--no-enrich", action="store_true",
                         help="Skip enrichment (people/match) entirely; search results only, no emails, no credits spent")
    parser.add_argument("--sample-size", type=int, default=2,
                         help="Max people to enrich per company (across both tiers combined) to confirm the "
                              "email format (default 2). Enrichment stops early once the format is confirmed twice.")
    parser.add_argument("--known-names", help="Path to a CSV (company,first,last[,title]) of people you already "
                                               "know the full name of (e.g. from a company's team page); each is "
                                               "cross-checked against Apollo's obfuscated search results (first "
                                               "name + last-name mask, using title to break ties) before an email "
                                               "is predicted for free. Ambiguous or unmatched names are skipped, "
                                               "not guessed.")
    parser.add_argument("--company-domains", help="Path to a CSV (company,domain) pinning specific companies to "
                                                    "their real domain, bypassing fuzzy name matching entirely. "
                                                    "Use this for companies whose name collides with unrelated "
                                                    "businesses (e.g. shares a place name) that fuzzy matching "
                                                    "can't confidently resolve on its own.")
    parser.add_argument("--out", default="apollo_leads.csv", help="Output CSV path")
    parser.add_argument("--max-pages", type=int, default=4, help="Max pages of search results per company per tier")
    parser.add_argument("--dry-run", action="store_true",
                         help="Free search-only triage pass across the company list: no enrichment, no credits "
                              "spent. Reports how many candidates Apollo actually has per company and recommends "
                              "a credit budget, so you can approve a specific number before spending anything.")
    parser.add_argument("--verify", action="store_true",
                         help="Verify resulting emails with NeverBounce (1 NeverBounce credit each). "
                              "Reads the key from NEVERBOUNCE_API_KEY. Apollo-verified addresses are "
                              "skipped since they are already confirmed.")
    parser.add_argument("--small-threshold", type=int, default=SMALL_FOOTPRINT_THRESHOLD,
                         help=f"--dry-run only: below this many total candidates, a company is 'small' and gets "
                              f"a 1-credit recommendation instead of 2 (default {SMALL_FOOTPRINT_THRESHOLD})")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("No API key: set APOLLO_API_KEY env var or pass --api-key")

    nb_key = os.environ.get("NEVERBOUNCE_API_KEY") if args.verify else None
    if args.verify and not nb_key:
        parser.error("--verify needs NEVERBOUNCE_API_KEY set")

    if args.targets:
        run_targets(args.targets, args.api_key, args.out, nb_key, no_enrich=args.no_enrich)
        return

    if args.companies_file:
        with open(args.companies_file) as f:
            companies = [line.strip() for line in f if line.strip()]
    else:
        companies = [args.company]

    locations = csv_list(args.locations) if args.locations else DEFAULT_LOCATIONS
    include_similar = not args.no_similar_titles

    tiers = {
        # Order matters: enrichment credits get spent on whichever tier's people
        # come first in the combined list. Recruiting/marketing people are less
        # likely to show up on a public "Leadership" team page than a CEO/founder,
        # so they get priority for paid credits - decision-makers are usually
        # findable for free via the company website instead.
        "recruiting": {
            "seniorities": csv_list(args.recruiting_seniorities) or None,
            "titles": csv_list(args.recruiting_titles) or None,
            "include_similar_titles": include_similar,
        },
        "marketing": {
            "seniorities": csv_list(args.marketing_seniorities) or None,
            "titles": csv_list(args.marketing_titles) or None,
            "include_similar_titles": include_similar,
        },
        "decision_maker": {
            "seniorities": csv_list(args.decision_seniorities) or None,
            "titles": csv_list(args.decision_titles) or None,
            "include_similar_titles": include_similar,
        },
        "gatekeeper": {
            "seniorities": None,
            "titles": csv_list(args.gatekeeper_titles) or None,
            "include_similar_titles": include_similar,
        },
    }
    tiers = {k: v for k, v in tiers.items() if v.get("titles") or v.get("seniorities")}

    if args.dry_run:
        run_dry_run_triage(companies, tiers, locations, args.api_key, max_pages=args.max_pages,
                            out_path=args.out, small_threshold=args.small_threshold,
                            company_domains_path=args.company_domains)
        return

    run(companies, tiers, locations, args.api_key, enrich=not args.no_enrich,
        out_path=args.out, max_pages=args.max_pages, sample_size=args.sample_size,
        known_names_path=args.known_names, company_domains_path=args.company_domains)


if __name__ == "__main__":
    main()
