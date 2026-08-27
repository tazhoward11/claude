# Context

Taz is an Account Executive at the **Austin Business Journal (ABJ)**, selling
advertising, sponsorships, events and special sections (Book of Lists, Best
Places to Work, Women in Business, Power Players, CREA, WILCO Table).

The job in this repo is one thing: **find the right decision-maker contacts at
Austin-area companies for ad sales outreach, without wasting Apollo credits.**

He gets **2,500 Apollo credits/month**. Target ratio: **1 credit ≈ 2 contacts.**

# Rules

**Always use `apollo_lookup.py`.** Never hand-roll a lookup script, never do
one-off lookups in chat. If the script can't do something, fix the script.

**Ask before spending credits.** State the estimate, wait for a number, then
run with `--max-credits` set to it. Never exceed the approved number, including
for tests.

**Don't print results into chat.** The script writes the workbook; report one
line (contacts / companies / credits / filename) and send the file. Dumping a
contact table costs more than the Apollo run did.

**Be brief.** No bug narration, no recaps, no restating his rules back to him.
Fix, commit, one line.

# Who counts as a contact

Decision makers — owner, founder, C-level, president, partner, VP, director —
**or** marketing people, highest position first.

- Max **4 per company**.
- Never four junior marketers. If there's a VP of Marketing and a Marketing
  Director, take both, then add the CEO or another senior local decision maker.
- Junior marketing is an execution contact, not a decision-maker slot.
- If nobody clears the bar, take the highest available and say so.
- Location filters on the **person**, not company HQ — Austin corridor
  (San Marcos to Waco, ~50mi east/west). See `DEFAULT_LOCATIONS`.

**Office-leader heuristic.** At multi-office firms the local budget holder has
"Office Managing Partner", "Regional President" or "Market Leader" in the title.
An unqualified "Director" at a law or accounting firm is a practice role, not a
buyer. "VP" at a large bank is often an individual contributor.

**Satellite-office trap.** Only chase execs at companies actually headquartered
locally. AMD's global CEO is not an Austin contact. Some local brands run
marketing through a parent (Dell Children's goes through Ascension).

# The normal run

```
export APOLLO_API_KEY="..."
python3 apollo_lookup.py --companies-file companies.txt \
    --out leads.csv --max-credits 60 --quiet
```

Writes `leads.xlsx`: a **Contacts** tab (ranked, capped at 4/company) and a
**Needs Manual Research** tab. One clean tab each — **no color coding**, he has
said the color coding was messy.

Add `--company-domains domains.csv` (rows: `Company Name,example.com`) whenever
domains are known; it prevents fuzzy name matches from grabbing the wrong firm.

Other modes: `--targets people.csv` for specific named people, `--dry-run` for a
free triage that spends nothing.

# How credits work

- Search (`mixed_people/api_search`) is **free**. Enrichment (`people/match`)
  costs 1 credit and charges even when the result is "unavailable".
- Re-revealing someone already revealed for the team is **free**.
- The script enriches a small sample per company to learn the email format, then
  applies that format to everyone else at the domain for free.
- A **first-name-only** format needs no surname, so Apollo's masked last names
  stop mattering — those people are all reachable free.
- Ledger: `apollo_credit_log.csv`. It's an estimate; Apollo has no usage endpoint.

# Hard-won lessons — do not relearn these

- **Never guess a masked surname.** `Mu***l` star counts are fixed-width and
  encode nothing. 7 of 10 guesses were wrong (Kaspar≠Kaiser, Schaub≠Schwab,
  Hansen≠Hanson). Match on the mask and let enrichment return the real name.
- **Never guess an email from a pattern seen once.** `amalbarran@` implied
  `smorgan@`; the real address was `semorgan@`. Spend the credit.
- **"No Apollo record" is usually a too-narrow filter, not an absent company.**
  Verify before telling him a company isn't there — this claim has been wrong before.
- **A missing town silently drops people.** KLP returned zero because Manor
  wasn't in `DEFAULT_LOCATIONS`. Check the town before concluding "nobody local".
- **Rank before enriching**, or the sample spends its credits on HR managers
  while the CEO comes back blank.
- Catchall domains accept any address, so pattern-testing against them is
  meaningless. NeverBounce key is in `NEVERBOUNCE_API_KEY`; `--verify` uses it.

# Keeping usage down

Long chats re-send their whole history every message, so cost grows with thread
length. Prefer: a fresh chat per batch, big batches over many small ones, lists
committed as files rather than pasted into chat, and Taz running the script
himself — a normal run needs no Claude involvement at all.
