#!/usr/bin/env python3
"""
query_biblio.py – Look up full bibliographic data for a patent document.

Usage:
    python3 query_biblio.py AP2355A
    python3 query_biblio.py US34567A1
    python3 query_biblio.py --country AP --number-only 2355 --kind A
"""

import argparse
import json
import re
import sys

import psycopg
from psycopg.rows import dict_row

from docdb_ingestion.database import get_dsn_from_env


COUNTRY_CODES = re.compile(r"^([A-Z]{2})")
KIND_CODES = re.compile(r"([A-Z][0-9]?[A-Z]?)$")


def split_patent_number(raw: str):
    raw = raw.strip().upper()
    cm = COUNTRY_CODES.match(raw)
    if not cm:
        return None, raw, None

    country = cm.group(1)
    rest = raw[len(country):]
    km = KIND_CODES.search(rest)
    if km:
        return country, rest[:km.start()], km.group(1)
    return country, rest, None


def find_publications(cur, country, number, kind):
    if kind:
        cur.execute(
            """
            SELECT *
            FROM patent_documents
            WHERE country = %s AND doc_number = %s AND kind_code = %s
            ORDER BY date_publ, pub_doc_id;
            """,
            (country, number, kind),
        )
    else:
        cur.execute(
            """
            SELECT *
            FROM patent_documents
            WHERE country = %s AND doc_number = %s
            ORDER BY date_publ, pub_doc_id;
            """,
            (country, number),
        )
    return cur.fetchall()


def fetch_full_biblio(cur, pub):
    parties = pub.get("parties") or {}
    texts = pub.get("texts") or []

    priority_claims = []
    for priority in pub.get("priorities") or []:
        normalized_priority = dict(priority)
        if "priority_date" not in normalized_priority:
            normalized_priority["priority_date"] = normalized_priority.get("date")
        priority_claims.append(normalized_priority)

    result = {
        "application": {
            "app_doc_id": pub.get("app_doc_id"),
            "app_country": pub.get("app_country"),
            "app_number": pub.get("app_number"),
            "app_kind_code": pub.get("app_kind_code"),
            "app_date": pub.get("app_date"),
            "extra_data": pub.get("app_extra_data") or {},
        },
        "publication": {
            "pub_doc_id": pub.get("pub_doc_id"),
            "country": pub.get("country"),
            "doc_number": pub.get("doc_number"),
            "kind_code": pub.get("kind_code"),
            "extended_kind": pub.get("extended_kind"),
            "date_publ": pub.get("date_publ"),
            "family_id": pub.get("family_id"),
            "is_representative": pub.get("is_representative"),
            "is_grant": pub.get("is_grant"),
            "originating_office": pub.get("originating_office"),
            "date_added_docdb": pub.get("date_added_docdb"),
            "date_last_exchange": pub.get("date_last_exchange"),
            "extra_data": pub.get("pub_extra_data") or {},
        },
        "applicants": parties.get("applicants") or [],
        "inventors": parties.get("inventors") or [],
        "other_parties": parties.get("others") or [],
        "priority_claims": priority_claims,
        "classifications": pub.get("classifications") or [],
        "citations": pub.get("citations") or [],
        "availability_dates": pub.get("availability_dates") or [],
        "titles": [item for item in texts if item.get("text_type") == "TITLE"],
        "abstracts": [item for item in texts if item.get("text_type") == "ABSTRACT"],
    }

    cur.execute(
        """
        SELECT country, pub_doc_id, doc_number, kind_code, date_publ, is_grant
        FROM patent_documents
        WHERE app_doc_id = %s AND pub_doc_id != %s
        ORDER BY date_publ, pub_doc_id;
        """,
        (pub.get("app_doc_id"), pub.get("pub_doc_id")),
    )
    result["related_publications"] = [dict(row) for row in cur.fetchall()]
    return result


def pretty_print(data: dict, pub_number: str):
    def d(val):
        return str(val) if val is not None else "—"

    pub = data.get("publication", {})
    app = data.get("application", {})

    print("\n" + "═" * 60)
    print(f"  BIBLIOGRAPHIC DATA  ·  {pub_number.upper()}")
    print("═" * 60)

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
        for title in data["titles"]:
            print(f"  [{title.get('lang', '?').upper()}] {title.get('content', '')}")

    if data.get("abstracts"):
        print("\n── ABSTRACTS ───────────────────────────────────────────────")
        for abstract in data["abstracts"]:
            content = abstract.get("content", "")
            suffix = "…" if len(content) > 400 else ""
            print(f"  [{abstract.get('lang', '?').upper()}] {content[:400]}{suffix}")

    if data.get("applicants"):
        print("\n── APPLICANTS ──────────────────────────────────────────────")
        for party in data["applicants"]:
            print(
                f"  {party.get('sequence', '?')}. {party.get('name', '')}  "
                f"[{d(party.get('residence'))}]  ({party.get('format', '')})"
            )

    if data.get("inventors"):
        print("\n── INVENTORS ───────────────────────────────────────────────")
        for party in data["inventors"]:
            print(f"  {party.get('sequence', '?')}. {party.get('name', '')}  [{d(party.get('residence'))}]")

    if data.get("priority_claims"):
        print("\n── PRIORITY CLAIMS ─────────────────────────────────────────")
        seen = set()
        for priority in data["priority_claims"]:
            key = (priority.get("country"), priority.get("doc_number"))
            if key in seen:
                continue
            seen.add(key)
            print(
                f"  {priority.get('country')} {priority.get('doc_number')}  "
                f"date={d(priority.get('priority_date'))}  active={priority.get('is_active')}"
            )

    if data.get("classifications"):
        print("\n── CLASSIFICATIONS ─────────────────────────────────────────")
        for classification in data["classifications"]:
            print(
                f"  [{classification.get('scheme_name', '')}] {classification.get('symbol', '').strip()}  "
                f"class={d(classification.get('class_value'))}  pos={d(classification.get('symbol_pos'))}"
            )

    if data.get("citations"):
        print("\n── CITATIONS ───────────────────────────────────────────────")
        for citation in data["citations"][:10]:
            if citation.get("citation_type") == "PATENT":
                print(f"  [{citation.get('cited_phase', '')}] PATENT {d(citation.get('cited_doc_id'))}")
            else:
                snippet = (citation.get("citation_text") or "")[:120]
                print(f"  [{citation.get('cited_phase', '')}] NPL  {snippet}")

    if data.get("availability_dates"):
        print("\n── PUBLIC AVAILABILITY DATES ───────────────────────────────")
        for availability in data["availability_dates"]:
            print(f"  {availability.get('availability_type')}  :  {d(availability.get('availability_date'))}")

    if data.get("related_publications"):
        print("\n── RELATED PUBLICATIONS (same application) ─────────────────")
        for related in data["related_publications"]:
            grant = " ✓ GRANT" if related.get("is_grant") else ""
            print(
                f"  {related.get('country')}{related.get('doc_number')}{related.get('kind_code')}  "
                f"date={d(related.get('date_publ'))}{grant}"
            )

    print("\n" + "═" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Look up full bibliographic data from the DOCDB database."
    )
    parser.add_argument("number", nargs="?", help="Patent number, e.g. US34567A1 or AP2355A")
    parser.add_argument("--country", help="Country code (2 letters)")
    parser.add_argument("--number-only", dest="number_only", help="Document number only")
    parser.add_argument("--kind", help="Kind code, e.g. A1, B2")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of pretty print")
    args = parser.parse_args()

    if args.number:
        country, number, kind = split_patent_number(args.number)
    elif args.country and args.number_only:
        country = args.country.upper()
        number = args.number_only
        kind = args.kind.upper() if args.kind else None
    else:
        parser.print_help()
        sys.exit(1)

    if kind and args.kind:
        kind = args.kind.upper()

    print(f"\nSearching for: country={country}  number={number}  kind={kind or 'any'}")

    try:
        conn = psycopg.connect(get_dsn_from_env(), row_factory=dict_row)
    except Exception as exc:
        print(f"\nERROR: Could not connect to database.\n{exc}")
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
                label = f"{pub['country']}{pub['doc_number']}{pub.get('kind_code', '')}"
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
