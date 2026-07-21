#!/usr/bin/env python3
"""
Apollo.io lead lookup: given company name(s) + job titles, find people in the
target region, enrich them for verified emails, and infer each company's
email format (e.g. first.last@domain.com) from the verified results.

API key is read from the APOLLO_API_KEY environment variable, or --api-key.
Never hardcode the key in this file or in shell history.

Usage:
    export APOLLO_API_KEY="..."
    python3 apollo_lookup.py "Acme Movers" --titles "Owner,Operations Manager"
    python3 apollo_lookup.py --companies-file companies.txt --titles "Owner,GM" --out leads.csv
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

SEARCH_URL = f"{API_BASE}/mixed_people/api_search"
MATCH_URL = f"{API_BASE}/people/match"

SESSION = requests.Session()


def api_headers(api_key: str) -> dict:
    return {"Content-Type": "application/json", "x-api-key": api_key}


def search_people_with_fallback(api_key: str, company: str, titles: list[str], locations: list[str],
                                 max_pages: int) -> tuple[list[dict], str]:
    """Apollo's org name match isn't fuzzy against extra words (e.g. 'Acme Capital'
    may not match an org listed as just 'Acme'). Retry with trailing words dropped
    until something matches. Returns (people, name_that_matched)."""
    words = company.split()
    candidates = [company] + [" ".join(words[:i]) for i in range(len(words) - 1, 0, -1)]
    for candidate in candidates:
        people = search_people(api_key, candidate, titles, locations, max_pages=max_pages)
        if people:
            if candidate != company:
                print(f"  no results for '{company}', falling back to '{candidate}'")
            return people, candidate
    return [], company


def search_people(api_key: str, company: str, titles: list[str], locations: list[str],
                   per_page: int = 25, max_pages: int = 4) -> list[dict]:
    results = []
    page = 1
    while page <= max_pages:
        payload = {
            "q_organization_name": company,
            "person_titles": titles,
            "person_locations": locations,
            "page": page,
            "per_page": per_page,
        }
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
    }
    return templates.get(lp, f"other ({lp})")


def infer_company_format(rows: list[dict]) -> str:
    votes = Counter(r["guessed_format"] for r in rows if r.get("email_status") == "verified"
                     and r["guessed_format"] not in ("unknown",) and not r["guessed_format"].startswith("other"))
    if not votes:
        return "insufficient data"
    fmt, count = votes.most_common(1)[0]
    return f"{fmt} (seen in {count}/{sum(votes.values())} verified emails)"


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
        "last.first": f"{l}.{f}", "lastf": f"{l}{f[0]}", "first": f,
    }
    local = builders.get(fmt)
    return f"{local}@{domain}" if local else None


def run(companies: list[str], titles: list[str], locations: list[str], api_key: str,
        enrich: bool, out_path: str, max_pages: int, sample_size: int, known_names_path: str | None):
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

    for company in companies:
        print(f"Searching: {company}")
        people, matched_as = search_people_with_fallback(api_key, company, titles, locations, max_pages)
        print(f"  found {len(people)} match(es)")

        confirmed_domain = None
        confirmed_format = None
        format_votes = Counter()
        enriched_count = 0

        for p in people:
            row = {
                "company": company, "matched_as": matched_as,
                "name": f"{p.get('first_name', '')} {p.get('last_name_obfuscated', '')}".strip(),
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
                            confirmed_domain = domain
                            if not fmt.startswith("other") and fmt != "unknown":
                                format_votes[fmt] += 1
                                if format_votes[fmt] >= min(2, sample_size):
                                    confirmed_format = fmt
            else:
                row["note"] = "not enriched (sample budget spent) - last name obfuscated, real email unknown"

            all_rows.append(row)

        if not confirmed_format and format_votes:
            confirmed_format = format_votes.most_common(1)[0][0]

        status = (f"format confirmed: {confirmed_format} @ {confirmed_domain}"
                  if confirmed_format else "could not confirm a format from sample")
        print(f"  {status} (spent {enriched_count} enrichment credit(s) on this company)")

        # Apply the confirmed format to any full names you already know, for free -
        # but only after cross-validating each one against Apollo's own obfuscated
        # records for this company, so lookalike names (Johnson vs. Johnston) can't
        # get silently mismatched to the wrong person.
        if confirmed_format and confirmed_domain:
            for candidate in known_names.get(company, []):
                matched_person, reason = resolve_candidate(candidate, people)
                row = {
                    "company": company, "matched_as": matched_as,
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
        writer = csv.DictWriter(f, fieldnames=["company", "matched_as", "name", "title", "linkedin_url",
                                                "email", "email_status", "guessed_format", "note"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} rows to {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("company", nargs="?", help="Single company name")
    src.add_argument("--companies-file", help="Path to a text file, one company name per line")
    parser.add_argument("--titles", required=True, help="Comma-separated job titles, e.g. 'Owner,Operations Manager'")
    parser.add_argument("--locations", help="Comma-separated locations; defaults to the San Marcos-Waco corridor")
    parser.add_argument("--api-key", default=os.environ.get("APOLLO_API_KEY"),
                         help="Apollo API key (defaults to APOLLO_API_KEY env var)")
    parser.add_argument("--no-enrich", action="store_true",
                         help="Skip enrichment (people/match) entirely; search results only, no emails, no credits spent")
    parser.add_argument("--sample-size", type=int, default=2,
                         help="Max people to enrich per company to confirm the email format (default 2). "
                              "Enrichment stops early once the format is confirmed twice.")
    parser.add_argument("--known-names", help="Path to a CSV (company,first,last[,title]) of people you already "
                                               "know the full name of (e.g. from a company's team page); each is "
                                               "cross-checked against Apollo's obfuscated search results (first "
                                               "name + last-name mask, using title to break ties) before an email "
                                               "is predicted for free. Ambiguous or unmatched names are skipped, "
                                               "not guessed.")
    parser.add_argument("--out", default="apollo_leads.csv", help="Output CSV path")
    parser.add_argument("--max-pages", type=int, default=4, help="Max pages of search results per company")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("No API key: set APOLLO_API_KEY env var or pass --api-key")

    if args.companies_file:
        with open(args.companies_file) as f:
            companies = [line.strip() for line in f if line.strip()]
    else:
        companies = [args.company]

    titles = [t.strip() for t in args.titles.split(",") if t.strip()]
    locations = [l.strip() for l in args.locations.split(",")] if args.locations else DEFAULT_LOCATIONS

    run(companies, titles, locations, args.api_key, enrich=not args.no_enrich,
        out_path=args.out, max_pages=args.max_pages, sample_size=args.sample_size,
        known_names_path=args.known_names)


if __name__ == "__main__":
    main()
