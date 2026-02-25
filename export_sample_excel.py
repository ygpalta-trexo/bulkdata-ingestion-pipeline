import argparse
import json
import os
import pandas as pd
import openpyxl
from typing import List, Dict
from dotenv import load_dotenv

from docdb_ingestion.database import DatabaseManager
from query_biblio import fetch_full_biblio

def fetch_top_pubs(db, limit: int = 100) -> List[Dict]:
    """Fetch the first X publications and their full bibliographic data."""
    # Query for the documents that have the most comprehensive data associated with them
    # Query for documents that have at least 3 out of the 4 key data enrichments
    with db.conn.cursor() as cur:
        cur.execute(f"""
            SELECT m.*
            FROM document_master m
            LEFT JOIN (
                SELECT pub_doc_id, 1 as has_inv 
                FROM parties WHERE party_type = 'INVENTOR' GROUP BY pub_doc_id
            ) as inv ON m.pub_doc_id = inv.pub_doc_id
            LEFT JOIN (
                SELECT pub_doc_id, 1 as has_cls 
                FROM patent_classifications GROUP BY pub_doc_id
            ) as cls ON m.pub_doc_id = cls.pub_doc_id
            LEFT JOIN (
                SELECT pub_doc_id, 1 as has_pri 
                FROM priority_claims GROUP BY pub_doc_id
            ) as pri ON m.pub_doc_id = pri.pub_doc_id
            LEFT JOIN (
                SELECT pub_doc_id, 1 as has_cit 
                FROM rich_citations_network GROUP BY pub_doc_id
            ) as cit ON m.pub_doc_id = cit.pub_doc_id
            WHERE (COALESCE(inv.has_inv, 0) + COALESCE(cls.has_cls, 0) + COALESCE(pri.has_pri, 0) + COALESCE(cit.has_cit, 0)) >= 3
            ORDER BY m.country ASC, m.doc_number ASC, m.kind_code ASC
            LIMIT {limit}
        """)
        pubs = cur.fetchall()
        
        results = []
        for p in pubs:
            results.append(fetch_full_biblio(cur, dict(p)))
        return results

def flatten_for_excel(data: List[Dict]) -> Dict[str, pd.DataFrame]:
    """Flatten the deeply nested JSON into multiple clean Pandas DataFrames (Tabs for Excel)."""
    
    # 1. Main bibliographic sheet (1 row = 1 publication)
    main_rows = []
    
    # Detail sheets
    parties_rows = []
    priority_rows = []
    classifications_rows = []
    citations_rows = []
    abstracts_titles_rows = []
    
    for item in data:
        pub = item.get("publication", {})
        app = item.get("application", {})
        
        pub_id = pub.get("pub_doc_id")
        pub_num_display = f"{pub.get('country', '')}{pub.get('doc_number', '')}{pub.get('kind_code', '')}"
        
        # Titles & Abstracts collection
        title = ""
        for t in item.get("titles", []):
            if t.get("lang") == "en":
                title = t.get("content", "")
                break
        if not title and item.get("titles"): # fallback to any
            title = item.get("titles")[0].get("content", "")
            
        for t in item.get("titles", []):
            abstracts_titles_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Type": "TITLE",
                "Language": t.get("lang"),
                "Content": t.get("content", "")
            })
            
        for a in item.get("abstracts", []):
            abstracts_titles_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Type": "ABSTRACT",
                "Language": a.get("lang"),
                "Content": a.get("content", "")
            })
            
        # Quick summary fields for the main sheet
        def _fmt_party(p):
            res = p.get('residence')
            return f"{p['party_name']} [{res}]" if res else p['party_name']
            
        applicants_summary = " | ".join([_fmt_party(a) for a in item.get("applicants", [])])
        inventors_summary = " | ".join([_fmt_party(i) for i in item.get("inventors", [])])
        
        main_rows.append({
            "Pub Doc ID": pub_id,
            "Publication Number": pub_num_display,
            "Publication Date": pub.get("date_publ"),
            "Is Grant": pub.get("is_grant"),
            "Title (English/Primary)": title,
            "Application Number": f"{app.get('app_country', '')}{app.get('app_number', '')}{app.get('app_kind_code', '')}",
            "Application Date": app.get("app_date"),
            "Applicants": applicants_summary,
            "Inventors": inventors_summary,
            "Count: Priorities": len(item.get("priority_claims", [])),
            "Count: Classifications": len(item.get("classifications", [])),
            "Count: Citations": len(item.get("citations", []))
        })
        
        # 2. Parties details
        for a in item.get("applicants", []):
            parties_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Role": "APPLICANT",
                "Sequence": a.get("sequence"),
                "Name": a.get("party_name"),
                "Country": a.get("residence")
            })
        for i in item.get("inventors", []):
            parties_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Role": "INVENTOR",
                "Sequence": i.get("sequence"),
                "Name": i.get("party_name"),
                "Country": i.get("residence")
            })
            
        # 3. Priority claims details
        for pri in item.get("priority_claims", []):
            priority_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Sequence": pri.get("sequence"),
                "Priority Country": pri.get("country"),
                "Priority Number": pri.get("doc_number"),
                "Priority Date": pri.get("priority_date"),
                "Active": pri.get("is_active")
            })
            
        # 4. Classifications details
        for cls in item.get("classifications", []):
            classifications_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Scheme": cls.get("scheme_name"),
                "Symbol": cls.get("symbol"),
                "Value": cls.get("class_value"),
                "Position": cls.get("symbol_pos")
            })
            
        # 5. Citations details
        for cit in item.get("citations", []):
            cit_info = f"PATENT {cit.get('cited_doc_id')}" if cit.get('citation_type') == 'PATENT' else f"NPL {cit.get('citation_text', '')}"
            citations_rows.append({
                "Pub Doc ID": pub_id,
                "Publication Number": pub_num_display,
                "Phase": cit.get("cited_phase"),
                "Sequence": cit.get("sequence"),
                "Type": cit.get("citation_type"),
                "Citation Reference": cit_info
            })

    # Convert to DataFrames
    return {
        "1. Main Data": pd.DataFrame(main_rows),
        "2. Applicants & Inventors": pd.DataFrame(parties_rows),
        "3. Priority Claims": pd.DataFrame(priority_rows),
        "4. Classifications": pd.DataFrame(classifications_rows),
        "5. Citations": pd.DataFrame(citations_rows),
        "6. Titles and Abstracts": pd.DataFrame(abstracts_titles_rows)
    }

def main():
    parser = argparse.ArgumentParser(description="Export 100 sample documents to Excel for PM review")
    parser.add_argument("--limit", type=int, default=100, help="Number of publications to export")
    parser.add_argument("--output", default="docdb_sample.xlsx", help="Output Excel filename")
    args = parser.parse_args()

    from query_biblio import get_dsn

    db_url = get_dsn()
    if not db_url:
        print("ERROR: DATABASE_URL not set in environment or .env file.")
        return

    db = DatabaseManager(db_url)
    db.connect()

    print(f"Fetching {args.limit} recent publications from the database...")
    data = fetch_top_pubs(db, args.limit)
    
    if not data:
        print("No data found in the database. Did you run the ingestion pipeline?")
        return
        
    print(f"Flattening {len(data)} records for Excel...")
    sheets = flatten_for_excel(data)
    
    print(f"Writing to {args.output}...")
    with pd.ExcelWriter(args.output, engine='openpyxl') as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            
            # Auto-adjust column widths
            worksheet = writer.sheets[sheet_name]
            for idx, col in enumerate(df.columns):
                # Calculate the max length in the column (capped)
                max_len = max(
                    df[col].astype(str).map(len).max() if not df.empty else 0,
                    len(str(col))
                )
                adjusted_width = min(max_len + 2, 50)  # max width 50 chars
                worksheet.column_dimensions[openpyxl.utils.get_column_letter(idx + 1)].width = adjusted_width
                
    print(f"✅ Summary report exported successfully to {args.output}")

if __name__ == "__main__":
    main()
