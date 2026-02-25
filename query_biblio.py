#!/usr/bin/env python3
"""
query_biblio.py – Look up full bibliographic data for a patent document.

Usage:
    python3 docdb_ingestion/query_biblio.py AP2355A
    python3 docdb_ingestion/query_biblio.py US34567 A1
    python3 docdb_ingestion/query_biblio.py --country AP --number 2355 --kind A
"""

import sys
import re
import os
import json
import argparse
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Argument parsing & number splitting
# ---------------------------------------------------------------------------

COUNTRY_CODES = re.compile(r'^([A-Z]{2,3})')
KIND_CODES = re.compile(r'([A-Z][0-9]?[A-Z]?)$')

def split_patent_number(raw: str):
    """
    Split a freeform patent number like 'US12345678B2' or 'AP2355A'
    into (country, number, kind).  Kind is optional.
    """
    raw = raw.strip().upper()

    # Match 2-character country prefix
    cm = COUNTRY_CODES.match(raw)
    if not cm:
        return None, raw, None
    country = cm.group(1)
    rest = raw[len(country):]

    # Try to match a trailing kind code (1-3 chars, starts with letter)
    km = KIND_CODES.search(rest)
    if km:
        kind = km.group(1)
        number = rest[:km.start()]
    else:
        kind = None
        number = rest

    return country, number, kind or None


def get_dsn():
    load_dotenv()
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return dsn
    user     = os.getenv("POSTGRES_USER",     "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "password")
    host     = os.getenv("POSTGRES_HOST",     "localhost")
    port     = os.getenv("POSTGRES_PORT",     "5432")
    dbname   = os.getenv("POSTGRES_DB",       "bulk-data")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def find_publications(cur, country, number, kind):
    """Return matching document_master rows."""
    if kind:
        cur.execute("""
            SELECT * FROM document_master
            WHERE country = %s AND doc_number = %s AND kind_code = %s
        """, (country, number, kind))
    else:
        cur.execute("""
            SELECT * FROM document_master
            WHERE country = %s AND doc_number = %s
            ORDER BY date_publ
        """, (country, number))
    return cur.fetchall()


def fetch_full_biblio(cur, pub):
    pub_doc_id = pub["pub_doc_id"]
    app_doc_id = pub["app_doc_id"]

    INTERNAL_FIELDS = {'created_at', 'updated_at', 'format_type', 'party_type'}

    def clean(row: dict, exclude: set = None) -> dict:
        skip = INTERNAL_FIELDS | (exclude or set())
        return {k: v for k, v in row.items() if k not in skip}

    result = {}

    # --- Application ---
    cur.execute("SELECT * FROM application_master WHERE app_doc_id = %s", (app_doc_id,))
    app = cur.fetchone()
    result["application"] = clean(dict(app)) if app else {}

    # --- Publication ---
    result["publication"] = clean(dict(pub), exclude={'app_doc_id', 'exchange_status', 'is_representative'})

    # --- Parties ---
    cur.execute("""
        SELECT party_type, sequence, party_name, residence, address_text
        FROM parties WHERE pub_doc_id = %s
        ORDER BY party_type, sequence
    """, (pub_doc_id,))
    parties = cur.fetchall()
    result["applicants"] = [clean(dict(p)) for p in parties if p["party_type"] == "APPLICANT"]
    result["inventors"]  = [clean(dict(p)) for p in parties if p["party_type"] == "INVENTOR"]

    # --- Priority Claims ---
    cur.execute("""
        SELECT format_type, sequence, priority_doc_id, country, doc_number, date AS priority_date, linkage_type, is_active
        FROM priority_claims WHERE pub_doc_id = %s
        ORDER BY sequence
    """, (pub_doc_id,))
    result["priority_claims"] = [dict(r) for r in cur.fetchall()]

    # --- Abstracts & Titles ---
    cur.execute("""
        SELECT text_type, lang, format_type, source, content
        FROM abstracts_and_titles WHERE pub_doc_id = %s
        ORDER BY text_type, lang
    """, (pub_doc_id,))
    texts = cur.fetchall()
    result["titles"]    = [dict(t) for t in texts if t["text_type"] == "TITLE"]
    result["abstracts"] = [dict(t) for t in texts if t["text_type"] == "ABSTRACT"]

    # --- Classifications ---
    cur.execute("""
        SELECT scheme_name, sequence, symbol, class_value, symbol_pos, generating_office
        FROM patent_classifications WHERE pub_doc_id = %s
        ORDER BY scheme_name, sequence
    """, (pub_doc_id,))
    result["classifications"] = [dict(r) for r in cur.fetchall()]

    # --- Citations ---
    cur.execute("""
        SELECT citation_id, cited_phase, sequence, citation_type, cited_doc_id, dnum_type,
               npl_type, extracted_xp, citation_text
        FROM rich_citations_network WHERE pub_doc_id = %s
        ORDER BY sequence
    """, (pub_doc_id,))
    citations = cur.fetchall()
    cit_list = []
    for cit in citations:
        c = dict(cit)
        cur.execute("""
            SELECT category, rel_claims, passage_text
            FROM citation_passage_mapping WHERE citation_id = %s
        """, (cit["citation_id"],))
        c["passages"] = [dict(p) for p in cur.fetchall()]
        cit_list.append(c)
    result["citations"] = cit_list

    # --- Public Availability Dates ---
    cur.execute("""
        SELECT availability_type, availability_date
        FROM public_availability_dates WHERE pub_doc_id = %s
        ORDER BY availability_date
    """, (pub_doc_id,))
    result["availability_dates"] = [dict(r) for r in cur.fetchall()]

    # --- Sibling publications (same application) ---
    cur.execute("""
        SELECT pub_doc_id, country, doc_number, kind_code, date_publ, is_grant
        FROM document_master WHERE app_doc_id = %s AND pub_doc_id != %s
        ORDER BY date_publ
    """, (app_doc_id, pub_doc_id))
    result["related_publications"] = [dict(r) for r in cur.fetchall()]

    return result


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def pretty_print(data: dict, pub_number: str):
    def d(val):
        return str(val) if val is not None else "—"

    pub  = data.get("publication", {})
    app  = data.get("application", {})

    print("\n" + "═"*60)
    print(f"  BIBLIOGRAPHIC DATA  ·  {pub_number.upper()}")
    print("═"*60)

    print("\n── APPLICATION ─────────────────────────────────────────────")
    print(f"  App Doc ID  : {d(app.get('app_doc_id'))}")
    print(f"  Country     : {d(app.get('app_country'))}")
    print(f"  Number      : {d(app.get('app_number'))}")
    print(f"  Kind        : {d(app.get('app_kind_code'))}")
    print(f"  Filing Date : {d(app.get('app_date'))}")

    print("\n── PUBLICATION ─────────────────────────────────────────────")
    print(f"  Pub Doc ID  : {d(pub.get('pub_doc_id'))}")
    print(f"  Country     : {d(pub.get('country'))}")
    print(f"  Number      : {d(pub.get('doc_number'))}")
    print(f"  Kind Code   : {d(pub.get('kind_code'))}")
    print(f"  Pub Date    : {d(pub.get('date_publ'))}")
    print(f"  Is Grant    : {'YES ✓' if pub.get('is_grant') else 'No'}")
    print(f"  Family ID   : {d(pub.get('family_id'))}")

    if data.get("titles"):
        print("\n── TITLES ──────────────────────────────────────────────────")
        for t in data["titles"]:
            print(f"  [{t.get('lang','?').upper()}] {t.get('content','')}")

    if data.get("abstracts"):
        print("\n── ABSTRACTS ───────────────────────────────────────────────")
        for a in data["abstracts"]:
            print(f"  [{a.get('lang','?').upper()}] {a.get('content','')[:400]}{'…' if len(a.get('content',''))>400 else ''}")

    if data.get("applicants"):
        print("\n── APPLICANTS ──────────────────────────────────────────────")
        for p in data["applicants"]:
            print(f"  {p.get('sequence','?')}. {p.get('party_name','')}  [{d(p.get('residence'))}]  ({p.get('format_type','')})")

    if data.get("inventors"):
        print("\n── INVENTORS ───────────────────────────────────────────────")
        for p in data["inventors"]:
            print(f"  {p.get('sequence','?')}. {p.get('party_name','')}  [{d(p.get('residence'))}]")

    if data.get("priority_claims"):
        print("\n── PRIORITY CLAIMS ─────────────────────────────────────────")
        seen = set()
        for pc in data["priority_claims"]:
            key = (pc.get("country"), pc.get("doc_number"))
            if key in seen:
                continue
            seen.add(key)
            print(f"  {pc.get('country')} {pc.get('doc_number')}  date={d(pc.get('priority_date'))}  active={pc.get('is_active')}")

    if data.get("classifications"):
        print("\n── CLASSIFICATIONS ─────────────────────────────────────────")
        for cl in data["classifications"]:
            print(f"  [{cl.get('scheme_name','')}] {cl.get('symbol','').strip()}  class={d(cl.get('class_value'))}  pos={d(cl.get('symbol_pos'))}")

    if data.get("citations"):
        print("\n── CITATIONS ───────────────────────────────────────────────")
        for ct in data["citations"][:10]:   # cap at 10 for display
            if ct.get("citation_type") == "PATENT":
                print(f"  [{ct.get('cited_phase','')}] PATENT {d(ct.get('cited_doc_id'))}")
            else:
                snippet = (ct.get("citation_text") or "")[:120]
                print(f"  [{ct.get('cited_phase','')}] NPL  {snippet}")

    if data.get("availability_dates"):
        print("\n── PUBLIC AVAILABILITY DATES ───────────────────────────────")
        for av in data["availability_dates"]:
            print(f"  {av.get('availability_type')}  :  {d(av.get('availability_date'))}")

    if data.get("related_publications"):
        print("\n── RELATED PUBLICATIONS (same application) ─────────────────")
        for rp in data["related_publications"]:
            grant = " ✓ GRANT" if rp.get("is_grant") else ""
            print(f"  {rp.get('country')}{rp.get('doc_number')}{rp.get('kind_code')}  date={d(rp.get('date_publ'))}{grant}")

    print("\n" + "═"*60 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Look up full bibliographic data from the DOCDB database."
    )
    parser.add_argument("number", nargs="?",
                        help="Patent number, e.g. US34567A1 or AP2355A")
    parser.add_argument("--country", help="Country code (2 letters)")
    parser.add_argument("--number-only", dest="number_only",
                        help="Document number only")
    parser.add_argument("--kind",   help="Kind code, e.g. A1, B2")
    parser.add_argument("--json",   action="store_true",
                        help="Output raw JSON instead of pretty print")
    args = parser.parse_args()

    # Resolve country/number/kind
    if args.number:
        country, number, kind = split_patent_number(args.number)
    elif args.country and args.number_only:
        country = args.country.upper()
        number  = args.number_only
        kind    = args.kind.upper() if args.kind else None
    else:
        parser.print_help()
        sys.exit(1)

    if kind and args.kind:
        kind = args.kind.upper()   # CLI flag overrides auto-parsed

    print(f"\nSearching for: country={country}  number={number}  kind={kind or 'any'}")

    try:
        conn = psycopg.connect(get_dsn(), row_factory=dict_row)
    except Exception as e:
        print(f"\nERROR: Could not connect to database.\n{e}")
        sys.exit(1)

    with conn.cursor() as cur:
        pubs = find_publications(cur, country, number, kind)

        if not pubs:
            print(f"\n  No records found for {country} {number} {kind or ''}.\n")
            sys.exit(0)

        print(f"  Found {len(pubs)} publication(s).\n")

        all_results = []
        for pub in pubs:
            biblio = fetch_full_biblio(cur, pub)
            all_results.append(biblio)
            if not args.json:
                label = f"{pub['country']}{pub['doc_number']}{pub.get('kind_code','')}"
                pretty_print(biblio, label)

        if args.json:
            def serial(obj):
                import datetime
                if isinstance(obj, (datetime.date, datetime.datetime)):
                    return obj.isoformat()
                raise TypeError(f"Type {type(obj)} not serializable")
            print(json.dumps(all_results, indent=2, default=serial))

    conn.close()


if __name__ == "__main__":
    main()
