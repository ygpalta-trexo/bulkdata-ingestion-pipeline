import zipfile
import html.entities
import io
import re
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

# ── Entity declarations injected into DOCTYPE for EPO XML compatibility ────────
#
# Two families of named entities appear in EPO DOCDB XML:
#
#   1. HTML5 entities  (&ldquo; &zcaron; &nbsp; …) — undefined in plain XML.
#   2. ISO SGML entities (&Dgr; &agr; &phgr; …) — defined in docdb-common.dtd.
#
# The EPO DTD is only available when the outer delivery ZIP includes a Root/DTDS/
# directory.  Several delivery weeks omit it, causing lxml to silently skip the
# SYSTEM reference and leave all ISO entities undefined.
#
# Fix: inject BOTH families into the DOCTYPE internal subset so parsing is fully
# self-contained regardless of whether the external DTD is present.
# Only the first 8 KB (where DOCTYPE lives) is rewritten; the remainder of the
# file streams from disk via shutil.copyfileobj — no full-file RAM load.

# ── EPO / ISO SGML entities NOT covered by HTML5 ─────────────────────────────
# EPO DOCDB uses ISO 8879 SGML character entity sets that predate HTML5.
# These use a "gr" suffix naming convention (iso-grk1/iso-grk2) instead of the
# HTML5 names (&delta; vs &dgr;, &Delta; vs &Dgr;).
_EPO_ISO_ENTITIES: dict[str, str] = {
    # ── ISO Greek 1 — lowercase ───────────────────────────────────────────────
    "agr":   "\u03B1",  # α  alpha
    "bgr":   "\u03B2",  # β  beta
    "ggr":   "\u03B3",  # γ  gamma
    "dgr":   "\u03B4",  # δ  delta
    "egr":   "\u03B5",  # ε  epsilon
    "zgr":   "\u03B6",  # ζ  zeta
    "eegr":  "\u03B7",  # η  eta
    "thgr":  "\u03B8",  # θ  theta
    "igr":   "\u03B9",  # ι  iota
    "kgr":   "\u03BA",  # κ  kappa
    "lgr":   "\u03BB",  # λ  lambda
    "mgr":   "\u03BC",  # μ  mu
    "ngr":   "\u03BD",  # ν  nu
    "xgr":   "\u03BE",  # ξ  xi
    "ogr":   "\u03BF",  # ο  omicron
    "pgr":   "\u03C0",  # π  pi
    "rgr":   "\u03C1",  # ρ  rho
    "sfgr":  "\u03C2",  # ς  final sigma
    "sgr":   "\u03C3",  # σ  sigma
    "tgr":   "\u03C4",  # τ  tau
    "ugr":   "\u03C5",  # υ  upsilon
    "phgr":  "\u03C6",  # φ  phi
    "khgr":  "\u03C7",  # χ  chi
    "psgr":  "\u03C8",  # ψ  psi
    "ohgr":  "\u03C9",  # ω  omega
    # ── ISO Greek 2 — uppercase ───────────────────────────────────────────────
    "Agr":   "\u0391",  # Α  Alpha
    "Bgr":   "\u0392",  # Β  Beta
    "Ggr":   "\u0393",  # Γ  Gamma
    "Dgr":   "\u0394",  # Δ  Delta
    "Egr":   "\u0395",  # Ε  Epsilon
    "Zgr":   "\u0396",  # Ζ  Zeta
    "EEgr":  "\u0397",  # Η  Eta
    "THgr":  "\u0398",  # Θ  Theta
    "Igr":   "\u0399",  # Ι  Iota
    "Kgr":   "\u039A",  # Κ  Kappa
    "Lgr":   "\u039B",  # Λ  Lambda
    "Mgr":   "\u039C",  # Μ  Mu
    "Ngr":   "\u039D",  # Ν  Nu
    "Xgr":   "\u039E",  # Ξ  Xi
    "Ogr":   "\u039F",  # Ο  Omicron
    "Pgr":   "\u03A0",  # Π  Pi
    "Rgr":   "\u03A1",  # Ρ  Rho
    "Sgr":   "\u03A3",  # Σ  Sigma
    "Tgr":   "\u03A4",  # Τ  Tau
    "Ugr":   "\u03A5",  # Υ  Upsilon
    "PHgr":  "\u03A6",  # Φ  Phi
    "KHgr":  "\u03A7",  # Χ  Chi
    "PSgr":  "\u03A8",  # Ψ  Psi
    "OHgr":  "\u03A9",  # Ω  Omega
    # ── ISO Greek 3 — accented / polytonic Greek ──────────────────────────────
    "aacgr":   "\u03AC",  # ά
    "aaegr":   "\u03AE",  # ή
    "adigr":   "\u03AA",  # Ϊ
    "aeacgr":  "\u03AD",  # έ
    "aiacgr":  "\u03AF",  # ί
    "aidigr":  "\u03CA",  # ϊ
    "aoacgr":  "\u03CC",  # ό
    "auacgr":  "\u03CD",  # ύ
    "audigr":  "\u03CB",  # ϋ
    "aohacgr": "\u03CE",  # ώ
    "Aacgr":   "\u0386",  # Ά
    "Aaegr":   "\u0389",  # Ή
    "Adigr":   "\u03AB",  # Ϋ
    "Aeacgr":  "\u0388",  # Έ
    "Aiacgr":  "\u038A",  # Ί
    "Aoacgr":  "\u038C",  # Ό
    "Auacgr":  "\u038E",  # Ύ
    # ── ISO Numerics — fractions absent from HTML5 ────────────────────────────
    "half":   "\u00BD",  # ½
    "frac14": "\u00BC",  # ¼
    "frac34": "\u00BE",  # ¾
    "frac13": "\u2153",  # ⅓
    "frac23": "\u2154",  # ⅔
    "frac15": "\u2155",  # ⅕
    "frac25": "\u2156",  # ⅖
    "frac35": "\u2157",  # ⅗
    "frac45": "\u2158",  # ⅘
    "frac16": "\u2159",  # ⅙
    "frac56": "\u215A",  # ⅚
    "frac18": "\u215B",  # ⅛
    "frac38": "\u215C",  # ⅜
    "frac58": "\u215D",  # ⅝
    "frac78": "\u215E",  # ⅞
    # ── ISO spacing / misc symbols ────────────────────────────────────────────
    "hairsp": "\u200A",  # hair space
    "numsp":  "\u2007",  # figure space
    "puncsp": "\u2008",  # punctuation space
    "emsp13": "\u2004",  # ⅓-em space
    "emsp14": "\u2005",  # ¼-em space
    "ohm":    "\u2126",  # Ω Ohm sign (distinct from Greek Omega U+03A9)
}


def _build_html_entity_decls() -> bytes:
    """Build XML <!ENTITY> declarations covering HTML5 + EPO ISO SGML entities.

    Merge strategy (no duplicates):
      • HTML5 entities are emitted first (sorted by name).
      • EPO ISO entities are appended only if the name was not already emitted
        by the HTML5 pass.
    In XML the first declaration of a given entity name wins, so HTML5
    definitions take precedence for any overlap (same codepoints anyway).
    Entity values use numeric character references (&#{N};) for encoding safety.
    """
    import re as _re
    _valid_xml_name = _re.compile(r"^[a-zA-Z_][\w.\-:]*$")

    seen: set[str] = set()
    decls: list[bytes] = []

    # ── Pass 1: HTML5 ─────────────────────────────────────────────────────────
    html5_names = {name.rstrip(";") for name in html.entities.html5.keys() if name.rstrip(";")}
    # Ensure XML built-in apostrophe entity is covered in uppercase form too.
    html5_names.add("apos")

    for name in sorted(html5_names):
        if not _valid_xml_name.match(name):
            continue
        if name in seen:
            continue

        raw_value = html.entities.html5.get(name + ";")
        if raw_value is None:
            if name == "apos":
                raw_value = "'"
            else:
                continue

        if isinstance(raw_value, str):
            chars = raw_value
        else:
            chars = "".join(raw_value)

        value = "".join(f"&#{ord(c)};" for c in chars)
        decls.append(f'<!ENTITY {name} "{value}">'.encode())
        seen.add(name)

        upper_name = name.upper()
        if upper_name != name and upper_name not in seen and _valid_xml_name.match(upper_name):
            decls.append(f'<!ENTITY {upper_name} "{value}">'.encode())
            seen.add(upper_name)

    # ── Pass 2: EPO ISO SGML (skip anything HTML5 already covered) ────────────
    for name, char in sorted(_EPO_ISO_ENTITIES.items()):
        if name in seen:
            continue
        seen.add(name)
        decls.append(f'<!ENTITY {name} "&#{ord(char)};">'.encode())

    return b"".join(decls)


_HTML_ENTITY_DECLS: bytes = _build_html_entity_decls()

# Entity names (bytes) that are declared in _HTML_ENTITY_DECLS plus XML built-ins.
# _make_html_patched_xml extends this with any names found in docdb-entities.dtd.
_KNOWN_ENTITY_NAMES: frozenset[bytes] = frozenset(
    re.findall(rb"<!ENTITY ([A-Za-z_][A-Za-z0-9._:-]*)", _HTML_ENTITY_DECLS)
) | frozenset([b"amp", b"lt", b"gt", b"apos", b"quot"])

_ENTITY_REF_RE = re.compile(rb"&([A-Za-z_][A-Za-z0-9._:-]*);")
_MAX_ENTITY_LEN = 64


def _stream_escape_unknown_entities(
    src_file, dst_file, known: frozenset, chunk_size: int = 1 << 20
) -> None:
    """Stream src_file → dst_file, replacing &name; for unknown entities with &amp;name;."""
    pending = b""
    while True:
        raw = src_file.read(chunk_size)
        if not raw:
            break
        data = pending + raw
        split = len(data)
        amp = data.rfind(b"&", max(0, split - _MAX_ENTITY_LEN - 2))
        if amp != -1 and b";" not in data[amp:]:
            split = amp
        to_write, pending = data[:split], data[split:]
        dst_file.write(_ENTITY_REF_RE.sub(
            lambda m: m.group(0) if m.group(1) in known else b"&amp;" + m.group(1) + b";",
            to_write,
        ))
    if pending:
        dst_file.write(_ENTITY_REF_RE.sub(
            lambda m: m.group(0) if m.group(1) in known else b"&amp;" + m.group(1) + b";",
            pending,
        ))


def _make_html_patched_xml(xml_path: str) -> str:
    """
    Write a sibling temp file whose DOCTYPE is augmented with HTML entity
    declarations, while preserving any SYSTEM/PUBLIC DTD reference so that
    EPO-specific entities (&Dgr;, &bgr; …) remain resolvable from the local
    copy of docdb-common.dtd in the same temp directory.

    Only the DOCTYPE declaration (first 8 KB) is modified; the rest of the file
    is streamed directly from disk via shutil.copyfileobj, so even 2 GB XML
    files do not need to be loaded into RAM.

    Returns the path of the patched temp file. The caller is responsible for
    deleting it when done.
    """
    import re as _re

    HEADER_LIMIT = 8192  # DOCTYPE is always within the first 8 KB

    with open(xml_path, "rb") as f:
        header = f.read(HEADER_LIMIT)
        rest_offset = f.tell()

    # ── Augment the DOCTYPE internal subset (do NOT replace SYSTEM ref) ────────
    if b"<!DOCTYPE" in header:
        if _re.search(rb"<!DOCTYPE[^>]*\[", header):
            # Has an existing internal subset — append HTML decls before the closing ]
            patched_header = _re.sub(
                rb"(\]\s*>)",
                _HTML_ENTITY_DECLS + rb"\1",
                header, count=1,
            )
        else:
            # Has SYSTEM/PUBLIC ref only — add internal subset before the closing >
            patched_header = _re.sub(
                rb"(<!DOCTYPE[^\[>]*?)(>)",
                rb"\1 [" + _HTML_ENTITY_DECLS + rb"]\2",
                header, count=1,
            )
    elif b"<?xml" in header:
        patched_header = _re.sub(
            rb"(<\?xml[^?]*\?>)",
            rb"\1\n<!DOCTYPE exchange-documents [" + _HTML_ENTITY_DECLS + rb"]>",
            header, count=1,
        )
    else:
        patched_header = b"<!DOCTYPE exchange-documents [" + _HTML_ENTITY_DECLS + b"]>\n" + header

    # Build known entity set: hardcoded HTML5+ISO + docdb-entities.dtd if available
    # (the pipeline copies it to the same temp dir as the XML when the DTDS dir is found).
    known = _KNOWN_ENTITY_NAMES
    dtd_path = os.path.join(os.path.dirname(xml_path), "docdb-entities.dtd")
    if os.path.exists(dtd_path):
        with open(dtd_path, "rb") as dtd_f:
            known = known | frozenset(
                re.findall(rb"<!ENTITY ([A-Za-z_][A-Za-z0-9._:-]*)", dtd_f.read())
            )

    patched_path = xml_path + "._htmlpatched"
    with open(patched_path, "wb") as out:
        out.write(patched_header)
        with open(xml_path, "rb") as orig:
            orig.seek(rest_offset)
            _stream_escape_unknown_entities(orig, out, known)

    return patched_path



def parse_xml_file(xml_path: str, recover_on_entity_error: bool = False) -> Iterator[ExchangeDocument]:
    """
    Parse the XML file, optionally recovering from unknown entity errors by replacing them with a placeholder.
    If recover_on_entity_error is True, unknown entities are replaced with '�' and a warning is logged.
    """
    patched_path = _make_html_patched_xml(xml_path)
    try:
        try:
            context = ET.iterparse(
                patched_path,
                events=("end",),
                tag=f"{{{NS['exch']}}}exchange-document",
                load_dtd=True,    # resolves EPO-specific entities (&delta;, &bgr;, &Dgr;…)
                no_network=True,  # DTDs are in the same temp dir as the XML
            )
            for event, elem in context:
                yield extract_document_data(elem)
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
        except ET.XMLSyntaxError as e:
            if not recover_on_entity_error or 'Entity' not in str(e):
                raise
            logger.warning(f"XMLSyntaxError: {e}. Attempting recovery by replacing unknown entities with '🔷'.")
            # Fallback: replace all &foo; with U+FFFD and re-parse
            with open(patched_path, 'rb') as f:
                xml_bytes = f.read()
            xml_bytes = re.sub(br'&[A-Za-z0-9_]+;', b'\xef\xbf\xbd', xml_bytes)
            temp_recovered = patched_path + '.recovered'
            with open(temp_recovered, 'wb') as f:
                f.write(xml_bytes)
            try:
                context = ET.iterparse(
                    temp_recovered,
                    events=("end",),
                    tag=f"{{{NS['exch']}}}exchange-document",
                    load_dtd=False,  # Entities are now gone
                    no_network=True,
                )
                for event, elem in context:
                    yield extract_document_data(elem)
                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]
            finally:
                try:
                    os.unlink(temp_recovered)
                except OSError:
                    pass
    finally:
        try:
            os.unlink(patched_path)
        except OSError:
            pass



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
        logger.info(f"Skipping void document (status={status}): {country}{doc_number}{kind}")
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
        logger.debug(f"Document {pub_doc_id} has unhandled fields: {unhandled}")
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
