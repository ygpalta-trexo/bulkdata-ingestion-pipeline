from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import date

class ApplicationMaster(BaseModel):
    app_doc_id: str
    app_country: str
    app_number: str
    app_kind_code: Optional[str] = None
    app_date: Optional[date] = None
    extra_data: Optional[Dict[str, Any]] = Field(default_factory=dict)

class DocumentMaster(BaseModel):
    pub_doc_id: str
    app_doc_id: str
    country: str
    doc_number: str
    kind_code: str
    extended_kind: Optional[str] = None
    date_publ: Optional[date] = None
    family_id: Optional[str] = None
    is_representative: Optional[bool] = None
    is_grant: bool = False
    originating_office: Optional[str] = None
    date_added_docdb: Optional[date] = None
    date_last_exchange: Optional[date] = None
    extra_data: Optional[Dict[str, Any]] = Field(default_factory=dict)

class PriorityClaim(BaseModel):
    format_type: str
    sequence: int
    priority_doc_id: Optional[str] = None
    country: str
    doc_number: str
    priority_date: Optional[date] = None
    linkage_type: Optional[str] = None
    is_active: Optional[bool] = None

class Party(BaseModel):
    party_type: str  # 'APPLICANT' or 'INVENTOR'
    format_type: str # 'docdb', 'docdba', 'original'
    sequence: int
    party_name: str
    residence: Optional[str] = None
    address_text: Optional[str] = None

class DesignationOfState(BaseModel):
    treaty_type: str  # 'PCT' or 'EPC'
    designation_type: str
    region_code: Optional[str] = None
    country_code: Optional[str] = None

class PatentClassification(BaseModel):
    scheme_name: str
    sequence: int
    group_number: Optional[int] = None
    rank_number: Optional[int] = None
    symbol: str
    class_value: Optional[str] = None
    symbol_pos: Optional[str] = None
    generating_office: Optional[str] = None

class CitationPassage(BaseModel):
    category: Optional[str] = None
    rel_claims: Optional[str] = None
    passage_text: Optional[str] = None

class RichCitation(BaseModel):
    cited_phase: str
    sequence: int
    srep_office: Optional[str] = None
    citation_type: str # 'PATENT' or 'NPL'
    cited_doc_id: Optional[str] = None
    dnum_type: Optional[str] = None
    npl_type: Optional[str] = None
    extracted_xp: Optional[str] = None
    opponent_name: Optional[str] = None
    citation_text: Optional[str] = None
    passages: List[CitationPassage] = Field(default_factory=list)

class PublicAvailabilityDate(BaseModel):
    availability_type: str
    availability_date: Optional[date] = None

class AbstractOrTitle(BaseModel):
    text_type: str # 'TITLE' or 'ABSTRACT'
    lang: str
    format_type: Optional[str] = None
    source: Optional[str] = None
    content: str

class ExchangeDocument(BaseModel):
    app_master: ApplicationMaster
    pub_master: DocumentMaster
    # 'C' = Create/Amend (upsert), 'D'/'DV'/'V' = Delete
    # This controls routing in bulk_upsert_safe — it is NOT stored in the database.
    operation: str = 'C'
    priorities: List[PriorityClaim] = Field(default_factory=list)
    parties: List[Party] = Field(default_factory=list)
    designations: List[DesignationOfState] = Field(default_factory=list)
    classifications: List[PatentClassification] = Field(default_factory=list)
    citations: List[RichCitation] = Field(default_factory=list)
    availability_dates: List[PublicAvailabilityDate] = Field(default_factory=list)
    abstracts_titles: List[AbstractOrTitle] = Field(default_factory=list)
