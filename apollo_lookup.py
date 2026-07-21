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


def run(companies: list[str], titles: list[str], locations: list[str], api_key: str,
        enrich: bool, out_path: str, max_pages: int):
    all_rows = []
    by_company_domain_rows = defaultdict(list)

    for company in companies:
        print(f"Searching: {company}")
        people = search_people(api_key, company, titles, locations, max_pages=max_pages)
        print(f"  found {len(people)} match(es)")

        for p in people:
            row = {
                "company": company,
                "name": f"{p.get('first_name', '')} {p.get('last_name_obfuscated', '')}".strip(),
                "title": p.get("title"),
                "linkedin_url": p.get("linkedin_url"),
                "email": "",
                "email_status": "",
                "guessed_format": "",
            }

            if enrich:
                enriched = enrich_person(api_key, p)
                time.sleep(0.3)
                if enriched:
                    row["name"] = enriched.get("name") or row["name"]
                    row["email"] = enriched.get("email") or ""
                    row["email_status"] = enriched.get("email_status") or ""
                    if row["email"] and row["email_status"] == "verified":
                        m = EMAIL_RE.match(row["email"])
                        if m:
                            local, domain = m.groups()
                            row["guessed_format"] = guess_format(
                                local, enriched.get("first_name"), enriched.get("last_name"))
                            by_company_domain_rows[(company, domain)].append(row)

            all_rows.append(row)

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["company", "name", "title", "linkedin_url",
                                                "email", "email_status", "guessed_format"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} rows to {out_path}")

    if enrich and by_company_domain_rows:
        print("\nInferred email formats:")
        for (company, domain), rows in by_company_domain_rows.items():
            print(f"  {company} ({domain}): {infer_company_format(rows)}")


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
                         help="Skip enrichment (people/match) to save credits; search results only, no emails")
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
        out_path=args.out, max_pages=args.max_pages)


if __name__ == "__main__":
    main()
