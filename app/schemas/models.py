from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class FieldInstruction(BaseModel):
    field_name: str = Field(description="snake_case name for the column/field")
    css_selector: str = Field(description="Relative CSS selector inside a single row, e.g. td:nth-child(2)")
    extract_type: str = Field(default="text", description="'text' for innerText, or attribute name like 'href'")


class DetailModalInstruction(BaseModel):
    has_detail_trigger: bool = True
    trigger_selector: Optional[str] = Field(default="td:nth-child(1) button, button:has-text('Detalii'), a:has-text('Detalii')")
    container_selector: Optional[str] = Field(default=".modal-content")
    close_selector: Optional[str] = Field(default=".modal-footer button, button.close, button:has-text('Închide')")
    kv_row_selector: Optional[str] = Field(default="table tr")
    label_selector: Optional[str] = Field(default="td:nth-child(1), th:nth-child(1)")
    value_selector: Optional[str] = Field(default="td:nth-child(2)")


class PaginationInstruction(BaseModel):
    url_template: str
    rows_per_page: int = 10
    total_pages_detected: int = 1


class ExtractionBlueprint(BaseModel):
    domain: str
    repeating_row_selector: str = "table tbody tr"
    pagination: PaginationInstruction
    summary_fields: List[FieldInstruction]
    detail_modal: DetailModalInstruction


class PDFPageContent(BaseModel):
    page_number: int
    full_text: str


class PDFTableContent(BaseModel):
    page_number: int
    table_index: int
    rows: List[List[Optional[str]]]


class ExtractedPDFDocument(BaseModel):
    pdf_url: str
    cached_hit: bool = False
    file_sha256: Optional[str] = None
    page_count: int = 0
    tables: List[PDFTableContent] = Field(default_factory=list)
    pages: List[PDFPageContent] = Field(default_factory=list)
    error: Optional[str] = None


class UnifiedRowData(BaseModel):
    row_index: int
    page_number: int
    summary_table: Dict[str, Optional[str]]
    detail_modal: Dict[str, Optional[str]]
    documents_pdf: Dict[str, ExtractedPDFDocument]


class DiscoverRequest(BaseModel):
    url: str = Field(examples=["https://nomenclator.anm.ro/medicamente?page=1"])
    force_reanalyze: bool = Field(default=False)


class DiscoverResponse(BaseModel):
    source_url: str
    endpoint_id: str
    live_api_url: str
    total_pages_detected: int
    rows_per_page: int
    total_rows_estimated: int
    blueprint: ExtractionBlueprint
    sample_preview_row: Optional[Dict[str, Any]] = None


class ExtractRequest(BaseModel):
    url: str = Field(examples=["https://nomenclator.anm.ro/medicamente?page=1"])
    rows_to_extract: int = Field(default=20, ge=1, le=100000)
    start_page: int = Field(default=1, ge=1)


class ExtractResponse(BaseModel):
    source_url: str
    endpoint_id: str
    live_api_url: str
    total_rows_estimated: int
    rows_requested: int
    rows_returned: int
    data: List[UnifiedRowData]