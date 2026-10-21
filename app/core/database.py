import base64
import json
import sqlite3
import zlib
from app.core.config import BASE_DIR

DB_PATH = BASE_DIR / "data_cache" / "web2api.db"
DB_PATH.parent.mkdir(exist_ok=True)


def reassemble_db_if_chunked():
    """Reassembles 45MB GitHub chunks into web2api.db automatically on cloud startup."""
    parts = sorted(DB_PATH.parent.glob("web2api.db.part*"))
    if parts and not DB_PATH.exists():
        print(f"[Database] Reassembling {len(parts)} chunks into {DB_PATH.name}...")
        with open(DB_PATH, "wb") as out_file:
            for part in parts:
                out_file.write(part.read_bytes())
        print("[Database] Reassembly complete!")


def compress_blob(data_dict: dict) -> bytes:
    """Compresses a dict into raw binary ZLIB bytes (33% smaller than Base64)."""
    raw_bytes = json.dumps(data_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return zlib.compress(raw_bytes, level=9)


def decompress_any(stored_val: bytes | str) -> dict:
    """Decompresses binary BLOB, Base64 ZLIB, or raw JSON string seamlessly."""
    if isinstance(stored_val, bytes):
        return json.loads(zlib.decompress(stored_val).decode("utf-8"))
    if isinstance(stored_val, str):
        if stored_val.startswith("ZLIB:"):
            raw = zlib.decompress(base64.b64decode(stored_val[5:]))
            return json.loads(raw.decode("utf-8"))
        return json.loads(stored_val)
    return {}


def get_conn() -> sqlite3.Connection:
    reassemble_db_if_chunked()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS endpoints (
                endpoint_id TEXT PRIMARY KEY,
                source_url TEXT,
                total_pages_detected INTEGER,
                rows_per_page INTEGER,
                total_rows_estimated INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pdf_blobs (
                pdf_url TEXT PRIMARY KEY,
                pdf_blob BLOB
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scraped_rows (
                endpoint_id TEXT,
                row_index INTEGER,
                page_number INTEGER,
                row_json BLOB,
                PRIMARY KEY (endpoint_id, row_index)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page ON scraped_rows(endpoint_id, page_number)")
        conn.commit()
    finally:
        conn.close()


def save_endpoint_meta(endpoint_id: str, source_url: str, total_pages: int, rows_per_page: int):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO endpoints
            (endpoint_id, source_url, total_pages_detected, rows_per_page, total_rows_estimated)
            VALUES (?, ?, ?, ?, ?)
            """,
            (endpoint_id, source_url, total_pages, rows_per_page, total_pages * rows_per_page),
        )
        conn.commit()


def save_row_incrementally(endpoint_id: str, row_index: int, page_number: int, row_dict: dict):
    """Deduplicates PDFs into pdf_blobs and stores the row as a tiny binary BLOB."""
    row_copy = dict(row_dict)
    docs = row_copy.get("documents_pdf") or {}
    slim_docs = {}

    with get_conn() as conn:
        for doc_key, doc_val in docs.items():
            if isinstance(doc_val, dict) and doc_val.get("pdf_url"):
                url = doc_val["pdf_url"]
                conn.execute(
                    "INSERT OR IGNORE INTO pdf_blobs (pdf_url, pdf_blob) VALUES (?, ?)",
                    (url, compress_blob(doc_val)),
                )
                slim_docs[doc_key] = {"__pdf_ref__": url}
            else:
                slim_docs[doc_key] = doc_val

        row_copy["documents_pdf"] = slim_docs
        conn.execute(
            """
            INSERT OR REPLACE INTO scraped_rows (endpoint_id, row_index, page_number, row_json)
            VALUES (?, ?, ?, ?)
            """,
            (endpoint_id, row_index, page_number, compress_blob(row_copy)),
        )
        conn.commit()


def _hydrate_row_pdfs(conn: sqlite3.Connection, row_dict: dict, local_cache: dict) -> dict:
    """Re-attaches the full PDF content from pdf_blobs when serving API requests."""
    docs = row_dict.get("documents_pdf") or {}
    for doc_key, doc_val in list(docs.items()):
        if isinstance(doc_val, dict) and "__pdf_ref__" in doc_val:
            url = doc_val["__pdf_ref__"]
            if url not in local_cache:
                pdf_row = conn.execute(
                    "SELECT pdf_blob FROM pdf_blobs WHERE pdf_url = ?", (url,)
                ).fetchone()
                if pdf_row:
                    local_cache[url] = decompress_any(pdf_row["pdf_blob"])
                else:
                    local_cache[url] = {"pdf_url": url}
            docs[doc_key] = local_cache[url]
    return row_dict


def clear_endpoint_rows(endpoint_id: str):
    pass


def fetch_cached_rows(
    endpoint_id: str,
    limit: int = 100,
    offset: int = 0,
    page_number: int | None = None,
    search: str | None = None,
):
    pdf_hydrate_cache = {}
    with get_conn() as conn:
        meta = conn.execute(
            "SELECT * FROM endpoints WHERE endpoint_id = ?", (endpoint_id,)
        ).fetchone()

        total_cached = conn.execute(
            "SELECT COUNT(*) as cnt FROM scraped_rows WHERE endpoint_id = ?", (endpoint_id,)
        ).fetchone()["cnt"]

        if page_number is not None:
            cursor = conn.execute(
                """
                SELECT row_json FROM scraped_rows
                WHERE endpoint_id = ? AND page_number = ?
                ORDER BY row_index ASC LIMIT ? OFFSET ?
                """,
                (endpoint_id, page_number, limit, offset),
            )
            rows = [
                _hydrate_row_pdfs(conn, decompress_any(r["row_json"]), pdf_hydrate_cache)
                for r in cursor.fetchall()
            ]
        elif search:
            q = search.strip().lower()
            cursor = conn.execute(
                "SELECT row_json FROM scraped_rows WHERE endpoint_id = ? ORDER BY row_index ASC",
                (endpoint_id,),
            )
            rows = []
            skipped = 0
            for r in cursor:
                decoded = decompress_any(r["row_json"])
                summary_str = json.dumps(decoded.get("summary_table", {}), ensure_ascii=False).lower()
                if q in summary_str:
                    if skipped < offset:
                        skipped += 1
                        continue
                    rows.append(_hydrate_row_pdfs(conn, decoded, pdf_hydrate_cache))
                    if len(rows) >= limit:
                        break
        else:
            cursor = conn.execute(
                """
                SELECT row_json FROM scraped_rows
                WHERE endpoint_id = ?
                ORDER BY row_index ASC LIMIT ? OFFSET ?
                """,
                (endpoint_id, limit, offset),
            )
            rows = [
                _hydrate_row_pdfs(conn, decompress_any(r["row_json"]), pdf_hydrate_cache)
                for r in cursor.fetchall()
            ]

    return {
        "meta": dict(meta) if meta else None,
        "total_cached": total_cached,
        "rows": rows,
    }


init_db()