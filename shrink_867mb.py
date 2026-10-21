import os
import sqlite3
import sys
from pathlib import Path

# Fix Windows console encoding for UTF-8 characters and emojis
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.core.database import DB_PATH, compress_blob, decompress_any

SLIM_DB_PATH = DB_PATH.parent / "web2api_slim.db"
CHUNK_SIZE = 45 * 1024 * 1024  # 45 MB per chunk for GitHub


def build_deduplicated_slim_db():
    if not DB_PATH.exists():
        print("❌ web2api.db not found!")
        return

    old_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f"[*] Original Database Size: {old_mb:.2f} MB")

    old_conn = sqlite3.connect(str(DB_PATH))
    old_conn.row_factory = sqlite3.Row
    total_rows = old_conn.execute("SELECT COUNT(*) FROM scraped_rows").fetchone()[0]

    # Check if a fully compressed slim DB already exists from a previous run
    already_complete = False
    if SLIM_DB_PATH.exists():
        try:
            chk_conn = sqlite3.connect(str(SLIM_DB_PATH))
            chk_rows = chk_conn.execute("SELECT COUNT(*) FROM scraped_rows").fetchone()[0]
            chk_pdfs = chk_conn.execute("SELECT COUNT(*) FROM pdf_blobs").fetchone()[0]
            chk_conn.close()
            if chk_rows == total_rows and total_rows > 0:
                print(f"[*] Found verified complete slim DB with all {chk_rows} rows & {chk_pdfs} unique PDFs!")
                already_complete = True
                seen_pdfs_count = chk_pdfs
                processed = chk_rows
            else:
                SLIM_DB_PATH.unlink()
        except Exception:
            if SLIM_DB_PATH.exists():
                SLIM_DB_PATH.unlink()

    if not already_complete:
        new_conn = sqlite3.connect(str(SLIM_DB_PATH))
        new_conn.execute("PRAGMA journal_mode = OFF")
        new_conn.execute("PRAGMA synchronous = OFF")

        # Create tables in new slim DB
        new_conn.execute("""
            CREATE TABLE endpoints (
                endpoint_id TEXT PRIMARY KEY,
                source_url TEXT,
                total_pages_detected INTEGER,
                rows_per_page INTEGER,
                total_rows_estimated INTEGER
            )
        """)
        new_conn.execute("""
            CREATE TABLE pdf_blobs (
                pdf_url TEXT PRIMARY KEY,
                pdf_blob BLOB
            )
        """)
        new_conn.execute("""
            CREATE TABLE scraped_rows (
                endpoint_id TEXT,
                row_index INTEGER,
                page_number INTEGER,
                row_json BLOB,
                PRIMARY KEY (endpoint_id, row_index)
            )
        """)

        # 1. Copy endpoints metadata
        for ep in old_conn.execute("SELECT * FROM endpoints"):
            new_conn.execute(
                "INSERT OR REPLACE INTO endpoints VALUES (?, ?, ?, ?, ?)",
                (
                    ep["endpoint_id"],
                    ep["source_url"],
                    ep["total_pages_detected"],
                    ep["rows_per_page"],
                    ep["total_rows_estimated"],
                ),
            )

        # 2. Stream all rows, deduplicate PDFs into pdf_blobs, and compress into binary BLOBs
        print(f"[*] Deduplicating PDFs & compressing {total_rows} rows...")

        seen_pdfs = set()
        cursor = old_conn.execute(
            "SELECT endpoint_id, row_index, page_number, row_json FROM scraped_rows ORDER BY row_index ASC"
        )

        row_batch = []
        pdf_batch = []
        processed = 0

        for r in cursor:
            row_dict = decompress_any(r["row_json"])
            docs = row_dict.get("documents_pdf") or {}
            slim_docs = {}

            for doc_key, doc_val in docs.items():
                if isinstance(doc_val, dict) and doc_val.get("pdf_url"):
                    url = doc_val["pdf_url"]
                    if url not in seen_pdfs:
                        seen_pdfs.add(url)
                        pdf_batch.append((url, compress_blob(doc_val)))
                    slim_docs[doc_key] = {"__pdf_ref__": url}
                else:
                    slim_docs[doc_key] = doc_val

            row_dict["documents_pdf"] = slim_docs
            row_batch.append(
                (r["endpoint_id"], r["row_index"], r["page_number"], compress_blob(row_dict))
            )
            processed += 1

            if len(row_batch) >= 500:
                if pdf_batch:
                    new_conn.executemany(
                        "INSERT OR IGNORE INTO pdf_blobs VALUES (?, ?)", pdf_batch
                    )
                    pdf_batch.clear()
                new_conn.executemany(
                    "INSERT OR REPLACE INTO scraped_rows VALUES (?, ?, ?, ?)", row_batch
                )
                new_conn.commit()
                row_batch.clear()
                print(f"  -> Processed {processed}/{total_rows} rows | Unique PDFs stored: {len(seen_pdfs)}")

        if pdf_batch:
            new_conn.executemany("INSERT OR IGNORE INTO pdf_blobs VALUES (?, ?)", pdf_batch)
        if row_batch:
            new_conn.executemany("INSERT OR REPLACE INTO scraped_rows VALUES (?, ?, ?, ?)", row_batch)

        new_conn.execute("CREATE INDEX idx_page ON scraped_rows(endpoint_id, page_number)")
        new_conn.commit()
        new_conn.close()
        del new_conn
        seen_pdfs_count = len(seen_pdfs)

    old_conn.close()
    del old_conn

    import gc
    gc.collect()

    new_mb = os.path.getsize(SLIM_DB_PATH) / (1024 * 1024)
    savings_pct = (1 - (new_mb / old_mb)) * 100
    print(f"\n🎉 SUCCESS!")
    print(f"  • Old Size: {old_mb:.2f} MB")
    print(f"  • New Size: {new_mb:.2f} MB ({savings_pct:.1f}% smaller!)")
    print(f"  • Total Rows Preserved: {processed}")
    print(f"  • Unique PDFs Deduplicated: {seen_pdfs_count}")

    # Replace old 867MB DB with the new slim DB
    if DB_PATH.exists():
        DB_PATH.unlink()
    SLIM_DB_PATH.rename(DB_PATH)

    # Remove any old part files
    for old_part in DB_PATH.parent.glob("web2api.db.part*"):
        old_part.unlink()

    # If still > 85 MB, split into 45 MB chunks so GitHub never blocks a single file!
    if new_mb > 85.0:
        print(f"\n[*] Splitting {new_mb:.2f} MB into 45 MB chunks for GitHub's 100MB per-file rule...")
        with open(DB_PATH, "rb") as f:
            idx = 0
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                part_file = DB_PATH.parent / f"web2api.db.part{idx:02d}"
                part_file.write_bytes(chunk)
                print(f"  -> Created {part_file.name} ({len(chunk)/(1024*1024):.2f} MB)")
                idx += 1
        print("[+] Done! Make sure 'data_cache/web2api.db' is in .gitignore so Git only pushes the .part files!")
    else:
        print("\n[+] Your database is under 85 MB! You can push data_cache/web2api.db directly to GitHub!")


if __name__ == "__main__":
    build_deduplicated_slim_db()
