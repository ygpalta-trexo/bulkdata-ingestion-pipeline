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

def xml_to_dict(node: Optional[ET.Element], max_depth: int = 50, _current_depth: int = 0) -> Any:
    """
    Recursively convert XML element to dict, capturing all nesting levels.
    
    Args:
        node: XML element to convert
        max_depth: Maximum nesting depth to prevent infinite recursion (default 50)
        _current_depth: Internal tracking of current recursion depth
    
    Returns:
        Dictionary representation of XML, preserving all data at any nesting level.
        Returns None if node is None, string if text-only content, dict otherwise.
    """
    if node is None:
        return None
    
    if _current_depth > max_depth:
        logger.warning(f"xml_to_dict: Max nesting depth ({max_depth}) exceeded. Stopping recursion.")
        return None
    
    result = {}
    if node.attrib:
        for k, v in node.attrib.items():
            clean_k = k.split('}')[-1] if '}' in k else k
            result[clean_k] = v
    
    for child in node:
        # Handle potential lxml element corruption
        try:
            if hasattr(child, 'tag') and isinstance(child.tag, str):
                child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            else:
                logger.warning(f"Skipping child with invalid tag: {type(child.tag)}")
                continue
        except (TypeError, AttributeError) as e:
            logger.warning(f"Skipping child due to tag access error: {e}")
            continue
            
        child_dict = xml_to_dict(child, max_depth=max_depth, _current_depth=_current_depth + 1)
        
        if child_tag in result:
            if type(result[child_tag]) is list:
                result[child_tag].append(child_dict)
            else:
                result[child_tag] = [result[child_tag], child_dict]
        else:
            result[child_tag] = child_dict
        
        # Capture tail text: text appearing after a child's closing tag.
        # e.g. <root><child/>tail text here</root> — 'tail text here' is child.tail
        if child.tail and child.tail.strip():
            tail_key = f"{child_tag}#tail"
            result[tail_key] = child.tail.strip()
            
    text_content = node.text.strip() if node.text and node.text.strip() else None
    if text_content:
        if not result:
            return text_content
        else:
            result['#text'] = text_content
            
    return result if result else None

def prune_dict(d: Any, global_keys: set, context_keys: dict = None, current_parent: str = None) -> Any:
    """
    Recursively removes specific keys from a dictionary and all its nested dictionaries/lists.
    - global_keys: set of keys to remove anywhere they appear
    - context_keys: dict mapping a parent_key -> set(child_keys_to_remove). 
      (e.g., if parent is 'publication-reference', only remove 'document-id' but keep others).
    Also removes empty wrappers (dicts or lists that become empty after pruning).
    """
    if context_keys is None:
        context_keys = {}
        
    if isinstance(d, dict):
        cleaned = {}
        for k, v in d.items():
            # Check global removal
            if k in global_keys:
                continue
            
            # Check contextual removal (e.g. are we currently inside 'publication-reference' and is 'k' == 'document-id'?)
            if current_parent in context_keys and k in context_keys[current_parent]:
                continue
                
            pruned_v = prune_dict(v, global_keys, context_keys, current_parent=k)
            if pruned_v is not None and pruned_v != {} and pruned_v != []:
                cleaned[k] = pruned_v
        return cleaned if cleaned else None
    elif isinstance(d, list):
        cleaned_list = []
        for item in d:
            pruned_item = prune_dict(item, global_keys, context_keys, current_parent=current_parent)
            if pruned_item is not None and pruned_item != {} and pruned_item != []:
                cleaned_list.append(pruned_item)
        return cleaned_list if cleaned_list else None
    else:
        return d

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
        load_dtd=True,   # Must be True to resolve EPO-specific entities (&delta;, &bgr; etc)
        no_network=True  # DTDs are copied to the same temp dir as the XML by process_zip_file
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

    # Skill rule: CV (Create Void) and DV (Delete Void) are bare identifier stubs
    # with NO doc-id. They represent withdrawn publications and must be skipped
    # unless the user explicitly wants to track withdrawn status.
    if status.upper() in ('CV', 'DV'):
        logger.debug(f"Skipping void document (status={status}): {country}{doc_number}{kind}")
        return ExchangeDocument(
            app_master=ApplicationMaster(app_doc_id=f"VOID_{country}{doc_number}", app_country=country, app_number=doc_number),
            pub_master=DocumentMaster(pub_doc_id=f"VOID_{country}{doc_number}{kind}", app_doc_id=f"VOID_{country}{doc_number}", country=country, doc_number=doc_number, kind_code=kind),
            operation='SKIP',
        )

    if not pub_doc_id:
        pub_doc_id = f"{country}{doc_number}{kind}"

    is_grant = False
    if kind in GRANT_KIND_CODES:
        is_grant = True
    
    # Check printed-with-grant
    for grant_tag in elem.findall(".//exch:dates-of-public-availability/exch:printed-with-grant", namespaces=NS):
        is_grant = True
        break

    # Read is-representative and metadata from the ROOT exchange-document element.
    # EPO places these as direct attributes on <exchange-document is-representative="YES|NO">.
    # This is the canonical source (more reliable than from the nested application-reference child).
    is_rep = (elem.get('is-representative', 'NO').upper() == 'YES')
    originating_office = elem.get('originating-office')
    date_added_docdb = parse_date(elem.get('date-added-docdb'))
    date_last_exchange = parse_date(elem.get('date-of-last-exchange'))
    
    # Extract Application Master (Root)
    # NOTE: child tags like <document-id>, <country>, <doc-number>, <kind>, <date>
    # inside <application-reference> have NO namespace prefix in the DOCDB XML.
    app_master = None
    for app_node in elem.findall(".//exch:application-reference", namespaces=NS):
        format_type = app_node.get('data-format', '')
        if format_type == 'docdb':
            app_doc_id = app_node.get('doc-id')
            # NOTE: is-representative is now read directly from the root exchange-document element.
            
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
        app_doc_id=app_master.app_doc_id,
        country=country,
        doc_number=doc_number,
        kind_code=kind,
        date_publ=parse_date(date_publ),
        family_id=family_id,
        is_representative=is_rep,
        is_grant=is_grant,
        originating_office=originating_office,
        date_added_docdb=date_added_docdb,
        date_last_exchange=date_last_exchange,
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
    
    # CRITICAL: Capture full tree BEFORE removing any blocks to ensure no nested data is lost.
    # This preserves all unhandled fields at any nesting level.
    full_tree = xml_to_dict(elem)
    
    # Known blocks that have unpredictable structure (like abstract, title, citations)
    # or deeply nested but fully mapped arrays where we want to drop the whole block.
    # We do NOT put purely structural wrappers like 'parties' or 'bibliographic-data' here,
    # nor do we put 'publication-reference', so they can naturally host unknown custom tags.
    fully_handled_keys = {
        "applicants",
        "inventors",
        "designation-epc",
        "designation-pct",
        "patent-classifications",
        "classifications-ipcr",
        "references-cited",
        "dates-of-public-availability",
        "abstract",
        "invention-title",
        "language-of-publication",
        "classification-ipc",
        "classification-national"
    }
    
    # Context-aware pruning: Only remove these generic leaf tags when they appear 
    # directly inside their standard EPO parent containers.
    # This safely collapses fully-mapped blocks (like empty priority-claims) while
    # preserving these exact same tags if they appear inside an unknown <doc-fake> block.
    context_handled_keys = {
        "document-id": {"country", "doc-number", "kind", "date", "name", "lang", "doc-id"},
        "publication-reference": {"data-format", "sequence"},
        "application-reference": {"data-format", "sequence", "is-representative", "doc-id"},
        "priority-claim": {"data-format", "sequence", "priority-active-indicator", "priority-linkage-type"},
        "applicant": {"sequence", "data-format"},
        "applicant-name": {"name"},
        "inventor": {"sequence", "data-format"},
        "inventor-name": {"name"},
        "classification-ipcr": {"sequence"}
    }
    
    # Known root-level attributes that were extracted into proper columns
    known_attributes = {
        'country', 'doc-number', 'kind', 'date-publ', 'doc-id', 
        'family-id', 'status', 'system',
        'is-representative',       # extracted directly from root elem to is_representative column
        'originating-office',      # promoted to originating_office column
        'date-added-docdb',        # promoted to date_added_docdb column
        'date-of-last-exchange',   # promoted to date_last_exchange column
    }
    
    # Build extra_data by recursively removing known keys from the full tree
    app_extra_data = {}
    pub_extra_data = {}
    if full_tree and isinstance(full_tree, dict):
        # First remove known root attributes to prevent bloat at the top level
        root_cleaned = {k: v for k, v in full_tree.items() if k not in known_attributes}
        
        # Then recursively prune deeply nested handled arrays/blocks
        pruned = prune_dict(root_cleaned, fully_handled_keys, context_handled_keys)
        
        if pruned and isinstance(pruned, dict):
            # Partition application-level extra data
            biblio = pruned.get("bibliographic-data", {})
            if isinstance(biblio, dict) and "application-reference" in biblio:
                app_refs = biblio.pop("application-reference")
                app_extra_data = {"bibliographic-data": {"application-reference": app_refs}}
                
                # Clean up empty bibliographic-data wrapper in pub_master
                if not biblio:
                    pruned.pop("bibliographic-data")
                    
            pub_extra_data = pruned
    
    # Log any unhandled data for debugging
    if pub_extra_data or app_extra_data:
        unhandled = list(pub_extra_data.keys()) + list(app_extra_data.keys())
        logger.info(f"Document {pub_doc_id} has unhandled fields: {unhandled}")
        logger.debug(f"Unhandled pub data: {pub_extra_data} | Unhandled app data: {app_extra_data}")
    
    app_master.extra_data = app_extra_data if app_extra_data else {}
    pub_master.extra_data = pub_extra_data if pub_extra_data else {}

    return ExchangeDocument(
        app_master=app_master,
        pub_master=pub_master,
        operation=status,  # 'C'=Create/Amend (upsert) | 'D'/'DV'/'V'=Delete
        priorities=priorities,
        parties=parties,
        designations=designations,
        classifications=classifications,
        citations=citations,
        availability_dates=avails,
        abstracts_titles=abstracts
    )
