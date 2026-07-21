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
# Gatekeepers: often the actual path to a decision-maker's calendar. Deliberately no
# seniority filter - Apollo tags these as low formal seniority despite real access.
DEFAULT_GATEKEEPER_TITLES = ["executive assistant", "assistant to the ceo", "assistant to the president",
                             "chief of staff", "office manager"]

SEARCH_URL = f"{API_BASE}/mixed_people/api_search"
MATCH_URL = f"{API_BASE}/people/match"

SESSION = requests.Session()


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


def enrich_person(api_key: str, person: dict) -> dict | None:
    payload = {"id": person.get("id")} if person.get("id") else {
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name_obfuscated", "").split("*")[0] or None,
        "organization_name": (person.get("organization") or {}).get("name"),
    }
    resp = SESSION.post(MATCH_URL, headers=api_headers(api_key), json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  [enrich] {payload}: HTTP {resp.status_code} - {resp.text}", file=sys.stderr)
        return None
    return resp.json().get("person")


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
                enriched = enrich_person(api_key, p)
                enriched_count += 1
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
                            if not fmt.startswith("other") and fmt != "unknown":
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


def csv_list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("company", nargs="?", help="Single company name")
    src.add_argument("--companies-file", help="Path to a text file, one company name per line")

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
    args = parser.parse_args()

    if not args.api_key:
        parser.error("No API key: set APOLLO_API_KEY env var or pass --api-key")

    if args.companies_file:
        with open(args.companies_file) as f:
            companies = [line.strip() for line in f if line.strip()]
    else:
        companies = [args.company]

    locations = csv_list(args.locations) if args.locations else DEFAULT_LOCATIONS
    include_similar = not args.no_similar_titles

    tiers = {
        "decision_maker": {
            "seniorities": csv_list(args.decision_seniorities) or None,
            "titles": csv_list(args.decision_titles) or None,
            "include_similar_titles": include_similar,
        },
        "marketing": {
            "seniorities": csv_list(args.marketing_seniorities) or None,
            "titles": csv_list(args.marketing_titles) or None,
            "include_similar_titles": include_similar,
        },
        "gatekeeper": {
            "seniorities": None,
            "titles": csv_list(args.gatekeeper_titles) or None,
            "include_similar_titles": include_similar,
        },
    }
    tiers = {k: v for k, v in tiers.items() if v.get("titles") or v.get("seniorities")}

    run(companies, tiers, locations, args.api_key, enrich=not args.no_enrich,
        out_path=args.out, max_pages=args.max_pages, sample_size=args.sample_size,
        known_names_path=args.known_names, company_domains_path=args.company_domains)


if __name__ == "__main__":
    main()
