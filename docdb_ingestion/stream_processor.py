import zipfile
import lxml.etree as ET
from typing import Iterator, Dict, Any, Optional
import logging
from datetime import datetime
import os
import tempfile
import shutil
from .models import (
    ExchangeDocument, ApplicationMaster, DocumentMaster, PriorityClaim,
    Party, DesignationOfState, PatentClassification, RichCitation, CitationPassage,
    PublicAvailabilityDate, AbstractOrTitle
)

NS = {'exch': 'http://www.epo.org/exchange'}
logger = logging.getLogger(__name__)

# Heuristic list of Grant Kind Codes per EPO specifications
GRANT_KIND_CODES = {'B1', 'B2', 'B3', 'C', 'C1', 'C2', 'E'}

def parse_date(date_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y%m%d").date()
    except ValueError:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None

def text(node: Optional[ET.Element]) -> Optional[str]:
    return node.text if node is not None else None

def process_zip_file(zip_path: str, dtd_dir: Optional[str] = None) -> Iterator[ExchangeDocument]:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            if dtd_dir and os.path.exists(dtd_dir):
                for item in os.listdir(dtd_dir):
                    s = os.path.join(dtd_dir, item)
                    d = os.path.join(temp_dir, item)
                    if os.path.isfile(s):
                        shutil.copy2(s, d)

            xml_filename = None
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for info in zf.infolist():
                    if info.filename.endswith('.xml'):
                        zf.extract(info, temp_dir)
                        xml_filename = info.filename
                        break
            
            if not xml_filename:
                logger.warning(f"No XML file found in {zip_path}")
                return

            xml_path = os.path.join(temp_dir, xml_filename)
            yield from parse_xml_file(xml_path)

    except zipfile.BadZipFile:
        logger.error(f"Error: Bad ZIP file {zip_path}")
    except Exception as e:
        logger.error(f"Error processing {zip_path}: {e}")
        raise

def parse_xml_file(xml_path: str) -> Iterator[ExchangeDocument]:
    context = ET.iterparse(
        xml_path, 
        events=("end",), 
        tag=f"{{{NS['exch']}}}exchange-document",
        load_dtd=True,
        no_network=True
    )
    
    for event, elem in context:
        yield extract_document_data(elem)
        elem.clear() 
        while elem.getprevious() is not None:
            del elem.getparent()[0]

def extract_document_data(elem: ET.Element) -> ExchangeDocument:
    country = elem.get('country', '')
    doc_number = elem.get('doc-number', '')
    kind = elem.get('kind', '')
    date_publ = elem.get('date-publ')
    pub_doc_id = elem.get('doc-id')
    family_id = elem.get('family-id')
    status = elem.get('status', 'C')
    
    if not pub_doc_id:
        pub_doc_id = f"{country}{doc_number}{kind}"

    is_grant = False
    if kind in GRANT_KIND_CODES:
        is_grant = True
    
    # Check printed-with-grant
    for grant_tag in elem.findall(".//exch:dates-of-public-availability/exch:printed-with-grant", namespaces=NS):
        is_grant = True
        break
    
    app_master = None
    is_rep = False
    
    # Extract Application Master (Root)
    # NOTE: child tags like <document-id>, <country>, <doc-number>, <kind>, <date>
    # inside <application-reference> have NO namespace prefix in the DOCDB XML.
    for app_node in elem.findall(".//exch:application-reference", namespaces=NS):
        format_type = app_node.get('data-format', '')
        if format_type == 'docdb':
            app_doc_id = app_node.get('doc-id')
            if app_node.get('is-representative') == 'YES':
                is_rep = True
            
            # Try bare tag first (most DOCDB XML), then namespaced as fallback
            doc_id_node = app_node.find("document-id")
            if doc_id_node is None:
                doc_id_node = app_node.find("exch:document-id", namespaces=NS)
            if doc_id_node is not None:
                a_c = text(doc_id_node.find("country")) or ''
                a_n = text(doc_id_node.find("doc-number")) or ''
                a_k = text(doc_id_node.find("kind")) or ''
                a_d = text(doc_id_node.find("date"))
                
                if not app_doc_id:
                    app_doc_id = f"{a_c}{a_n}{a_k}"
                    
                app_master = ApplicationMaster(
                     app_doc_id=app_doc_id,
                     app_country=a_c,
                     app_number=a_n,
                     app_kind_code=a_k,
                     app_date=parse_date(a_d)
                )
                break
            elif app_doc_id:
                # doc-id attribute exists but no document-id child -- use attribute only
                app_master = ApplicationMaster(
                    app_doc_id=app_doc_id,
                    app_country=country,
                    app_number='',
                )
                break
                
    if not app_master:
        # Fallback if no docdb application reference exists
        app_master = ApplicationMaster(
            app_doc_id=f"UNKNOWN_{pub_doc_id}",
            app_country="XX",
            app_number="UNKNOWN"
        )
        
    pub_master = DocumentMaster(
        pub_doc_id=pub_doc_id,
        app_doc_id=app_master.app_doc_id, # Link foreign key back to root application
        country=country,
        doc_number=doc_number,
        kind_code=kind,
        date_publ=parse_date(date_publ),
        family_id=family_id,
        exchange_status=status,
        is_representative=is_rep,
        is_grant=is_grant
    )

    priorities = []
    seen_priorities = set()  # Deduplicate by (country, doc_number)
    for pri_node in elem.findall(".//exch:priority-claims/exch:priority-claim", namespaces=NS):
        format_type = pri_node.get('data-format', '')
        # Only keep docdb format — it has date, country, active flag.
        # epodoc/docdba are duplicates with less information.
        if format_type != 'docdb':
            continue
        seq = int(pri_node.get('sequence', '0'))
        
        # priority-claim's <document-id> also has no namespace prefix
        doc_id_node = pri_node.find("document-id")
        if doc_id_node is None:
            doc_id_node = pri_node.find("exch:document-id", namespaces=NS)
        if doc_id_node is not None:
             p_doc_id = doc_id_node.get('doc-id')
             p_country = text(doc_id_node.find("country")) or ''
             p_number  = text(doc_id_node.find("doc-number")) or ''
             
             dedup_key = (seq, p_country, p_number)
             if dedup_key in seen_priorities:
                 continue
             seen_priorities.add(dedup_key)
             
             active_indicator = text(pri_node.find("exch:priority-active-indicator", namespaces=NS))
             is_active = True if active_indicator == 'Y' else (False if active_indicator == 'N' else None)
             
             priorities.append(PriorityClaim(
                 format_type=format_type,
                 sequence=seq,
                 priority_doc_id=p_doc_id,
                 country=p_country,
                 doc_number=p_number,
                 priority_date=parse_date(text(doc_id_node.find("date"))),
                 linkage_type=text(pri_node.find("exch:priority-linkage-type", namespaces=NS)),
                 is_active=is_active
             ))

    parties = []
    # Format preference chain (highest wins per sequence slot):
    #   docdb (3)  — canonical: normalized name + residence country  [always prefer]
    #   docdba (2) — Latin transliteration: fallback if docdb absent
    #   original (1) — native script (kanji, arabic): retained for multilingual search
    #   epodoc     — SKIP: no name/residence data for parties
    seen_parties: dict = {}  # key=(party_type, seq) -> stored format priority
    FORMAT_PRIORITY = {'docdb': 3, 'docdba': 2, 'original': 1}

    for tag_name, p_type in [("exch:applicants/exch:applicant", "APPLICANT"), ("exch:inventors/exch:inventor", "INVENTOR")]:
        for p_node in elem.findall(f".//exch:parties/{tag_name}", namespaces=NS):
            fmt = p_node.get('data-format', '')
            
            # Only skip epodoc — carries no name/residence for parties
            if fmt not in FORMAT_PRIORITY:
                continue
            
            name = text(p_node.find(".//name")) or text(p_node.find(".//exch:name", namespaces=NS))
            res  = text(p_node.find(".//residence/country")) or text(p_node.find(".//exch:country", namespaces=NS))
            addr = text(p_node.find(".//address/text"))  or text(p_node.find(".//exch:text", namespaces=NS))
            seq  = int(p_node.get('sequence', '0'))
            
            if not name:
                continue
            
            dedup_key = (p_type, seq)
            existing_prio = seen_parties.get(dedup_key, -1)
            if FORMAT_PRIORITY[fmt] > existing_prio:
                # Replace or add: higher-priority format wins
                seen_parties[dedup_key] = FORMAT_PRIORITY[fmt]
                # Remove old entry if it exists (replace with better format)
                parties[:] = [p for p in parties if not (p.party_type == p_type and p.sequence == seq)]
                parties.append(Party(
                    party_type=p_type,
                    format_type=fmt,
                    sequence=seq,
                    party_name=name,
                    residence=res,
                    address_text=addr
                ))

    designations = []
    for epc_tag in ["exch:designation-epc", "exch:designation-pct"]:
        block = elem.find(f".//{epc_tag}", namespaces=NS)
        if block is not None:
            treaty = 'EPC' if 'epc' in epc_tag else 'PCT'
            for child in block:
                desig_type = child.tag.split('}')[-1]
                seen_countries = set()
                # Try bare <country> first (no exch: prefix), then namespaced as fallback
                country_nodes = child.findall(".//country") or child.findall(".//exch:country", namespaces=NS)
                for c in country_nodes:
                    if c.text and c.text not in seen_countries:
                        seen_countries.add(c.text)
                        designations.append(DesignationOfState(
                            treaty_type=treaty,
                            designation_type=desig_type,
                            country_code=c.text
                        ))

    classifications = []
    # NOTE: <patent-classification> and ALL children are bare tags (no exch: prefix).
    # lxml cannot mix namespaced parent + bare child in one XPath, so find parent first.
    for classifications_block in elem.findall(".//exch:patent-classifications", namespaces=NS):
        for c_set in classifications_block.findall("patent-classification"):
            sym = text(c_set.find("classification-symbol")) or text(c_set.find("text"))
            scheme_node = c_set.find("classification-scheme")
            scheme = (scheme_node.get('scheme', '') if scheme_node is not None else '') or c_set.get('scheme', '')
            if sym:
                classifications.append(PatentClassification(
                    scheme_name=scheme,
                    sequence=int(c_set.get('sequence', '0')),
                    symbol=sym.strip(),
                    class_value=text(c_set.find("classification-value")),
                    # group_number and rank_number are IPC-specific attributes,
                    # absent from CPC/CPCI records — left None for those
                    group_number=int(c_set.get('group-number')) if c_set.get('group-number') else None,
                    rank_number=int(c_set.get('rank-number')) if c_set.get('rank-number') else None,
                    symbol_pos=text(c_set.find("symbol-position")),
                    generating_office=text(c_set.find("generating-office"))
                ))

    citations = []
    for cit_node in elem.findall(".//exch:references-cited/exch:citation", namespaces=NS):
        # NOTE: <patcit> and <nplcit> are bare tags; <document-id> children inside are also bare
        pat_node = cit_node.find("patcit")
        npl_node = cit_node.find("nplcit")
        
        c_type = 'PATENT' if pat_node is not None else 'NPL' if npl_node is not None else 'UNKNOWN'
        
        # Bare document-id inside patcit
        doc_node = pat_node.find("document-id") if pat_node is not None else None
        
        cit = RichCitation(
            cited_phase=cit_node.get('cited-phase', ''),
            sequence=int(cit_node.get('sequence', '0')),
            srep_office=cit_node.get('srep-office'),
            citation_type=c_type,
            npl_type=npl_node.get('npl-type') if npl_node is not None else None,
            extracted_xp=npl_node.get('extracted-xp') if npl_node is not None else None,
            # nplcit text is also bare
            citation_text=text(npl_node.find("text")) if npl_node is not None else None
        )
        
        if doc_node is not None:
             # Country, doc-number, kind inside document-id are all bare
             c = text(doc_node.find("country")) or ''
             n = text(doc_node.find("doc-number")) or ''
             k = text(doc_node.find("kind")) or ''
             cit.cited_doc_id = f"{c}{n}{k}"
             cit.dnum_type = pat_node.get('dnum-type')
             
        passages = []
        for rel in cit_node.findall("exch:rel-passage", namespaces=NS):
             passages.append(CitationPassage(
                 category=text(rel.find("exch:category", namespaces=NS)),
                 rel_claims=text(rel.find("exch:rel-claims", namespaces=NS)),
                 passage_text=text(rel.find("exch:passage", namespaces=NS))
             ))
        cit.passages = passages
        citations.append(cit)

    avails = []
    for avail_node in elem.findall(".//exch:dates-of-public-availability/*", namespaces=NS):
         # <document-id> inside availability nodes is bare (no exch: prefix)
         doc_id_node = avail_node.find("document-id")
         if doc_id_node is None:
             doc_id_node = avail_node.find("exch:document-id", namespaces=NS)
         if doc_id_node is not None:
             # <date> inside is also bare
             d = parse_date(text(doc_id_node.find("date")) or text(doc_id_node.find("exch:date", namespaces=NS)))
             if d:
                 avails.append(PublicAvailabilityDate(
                     availability_type=avail_node.tag.split('}')[-1],
                     availability_date=d
                 ))
                 
    abstracts = []
    for txt in elem.findall(".//exch:abstract", namespaces=NS):
        # <p> tags inside abstract may be bare or namespaced
        paras = txt.findall("exch:p", namespaces=NS) or txt.findall("p")
        content = "\n".join([p.text for p in paras if p.text])
        if content:
             abstracts.append(AbstractOrTitle(
                 text_type='ABSTRACT',
                 lang=txt.get('lang', ''),
                 format_type=txt.get('data-format'),
                 source=txt.get('abstract-source'),
                 content=content
             ))
             
    for txt in elem.findall(".//exch:invention-title", namespaces=NS):
        if txt.text:
             abstracts.append(AbstractOrTitle(
                 text_type='TITLE',
                 lang=txt.get('lang', ''),
                 format_type=txt.get('data-format'),
                 source=None,
                 content=txt.text.strip()
             ))

    return ExchangeDocument(
        app_master=app_master,
        pub_master=pub_master,
        priorities=priorities,
        parties=parties,
        designations=designations,
        classifications=classifications,
        citations=citations,
        availability_dates=avails,
        abstracts_titles=abstracts
    )
