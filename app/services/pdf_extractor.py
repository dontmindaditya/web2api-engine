import hashlib
from urllib.parse import urljoin
import pymupdf as fitz
import httpx
from app.schemas.models import (
    ExtractedPDFDocument,
    PDFPageContent,
    PDFTableContent,
)

# In-memory cache keyed by absolute PDF URL so duplicate PDFs across rows resolve in 0ms
_PDF_MEMORY_CACHE: dict[str, ExtractedPDFDocument] = {}


def normalize_key(text: str) -> str:
    """Converts modal labels like 'Rezumat caracteristici produs' into clean snake_case keys."""
    clean = (
        text.lower()
        .replace("ă", "a")
        .replace("â", "a")
        .replace("î", "i")
        .replace("ș", "s")
        .replace("ş", "s")
        .replace("ț", "t")
        .replace("ţ", "t")
    )
    clean = "".join(ch if ch.isalnum() else "_" for ch in clean)
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_") or "document_pdf"


async def download_and_extract_pdf(
    raw_pdf_url: str, base_page_url: str, client: httpx.AsyncClient
) -> ExtractedPDFDocument:
    """
    Streams a PDF URL into RAM, caches by URL, and extracts 100% of pages and tables into JSON.
    """
    full_pdf_url = urljoin(base_page_url, raw_pdf_url.strip())

    # 1. Check Cache First (Huge speedup when multiple rows share the same drug PDF)
    if full_pdf_url in _PDF_MEMORY_CACHE:
        cached = _PDF_MEMORY_CACHE[full_pdf_url].model_copy(deep=True)
        cached.cached_hit = True
        return cached

    try:
        resp = await client.get(full_pdf_url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        pdf_bytes = resp.content
        sha256_hash = hashlib.sha256(pdf_bytes).hexdigest()

        # 2. Parse PDF in RAM using PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_list: list[PDFPageContent] = []
        tables_list: list[PDFTableContent] = []

        for page_idx, page in enumerate(doc):
            page_text = page.get_text("text").strip()
            pages_list.append(
                PDFPageContent(page_number=page_idx + 1, full_text=page_text)
            )

            # Extract any structured tables on this PDF page
            try:
                found_tables = page.find_tables()
                for tab_idx, tab in enumerate(found_tables.tables):
                    tables_list.append(
                        PDFTableContent(
                            page_number=page_idx + 1,
                            table_index=tab_idx + 1,
                            rows=tab.extract(),
                        )
                    )
            except Exception:
                pass

        doc.close()

        extracted_doc = ExtractedPDFDocument(
            pdf_url=full_pdf_url,
            cached_hit=False,
            file_sha256=sha256_hash,
            page_count=len(pages_list),
            tables=tables_list,
            pages=pages_list,
        )
        _PDF_MEMORY_CACHE[full_pdf_url] = extracted_doc
        return extracted_doc

    except Exception as exc:
        return ExtractedPDFDocument(
            pdf_url=full_pdf_url,
            cached_hit=False,
            error=str(exc),
        )