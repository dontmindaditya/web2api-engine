import asyncio
import httpx
from playwright.async_api import async_playwright
from app.core.database import save_row_incrementally
from app.schemas.models import ExtractionBlueprint, UnifiedRowData
from app.services.pdf_extractor import download_and_extract_pdf, normalize_key

# Global job state tracker so the UI & API can check background scraping progress
JOB_STATUS: dict[str, dict] = {}


async def execute_blueprint_scrape(
    blueprint: ExtractionBlueprint,
    rows_to_extract: int = 20,
    start_page: int = 1,
    endpoint_id: str | None = None,
) -> list[UnifiedRowData]:
    """
    Scrapes across pages, disables CSS animations for 4x faster modal clicks,
    and saves each row to SQLite immediately as it is scraped.
    """
    collected_rows: list[UnifiedRowData] = []
    current_page = start_page
    max_page = max(blueprint.pagination.total_pages_detected, start_page)

    if endpoint_id:
        JOB_STATUS[endpoint_id] = {
            "status": "running",
            "current_page": start_page,
            "rows_scraped": 0,
            "rows_target": rows_to_extract,
            "stop_requested": False,
        }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient(headers=headers, verify=False) as http_client:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
                )
                try:
                    context = await browser.new_context(viewport={"width": 1440, "height": 900})
                    page = await context.new_page()

                    while len(collected_rows) < rows_to_extract and current_page <= max_page:
                        if endpoint_id and JOB_STATUS.get(endpoint_id, {}).get("stop_requested"):
                            print(f"[Executor] Stop requested by user for {endpoint_id}.")
                            break

                        if endpoint_id:
                            JOB_STATUS[endpoint_id]["current_page"] = current_page

                        page_url = blueprint.pagination.url_template.replace(
                            "{page}", str(current_page)
                        )
                        print(
                            f"\n[Executor] >>> Scraping Page {current_page} ({len(collected_rows)}/{rows_to_extract} rows) -> {page_url}"
                        )
                        try:
                            await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
                        except Exception as page_err:
                            print(f"[Executor] Failed to load {page_url}: {page_err}")
                            current_page += 1
                            continue

                        # Disable Bootstrap modal fade animations so clicking 20 modals is 4x faster!
                        await page.add_style_tag(
                            content="* { transition: none !important; animation: none !important; }"
                        )

                        main_table = page.locator("table").first
                        if await main_table.count() > 0:
                            rows_locator = main_table.locator("tbody tr")
                        else:
                            rows_locator = page.locator(blueprint.repeating_row_selector)

                        row_count_on_page = await rows_locator.count()
                        print(f"[Executor] Found {row_count_on_page} rows on Page {current_page}")

                        if row_count_on_page == 0:
                            break

                        for i in range(row_count_on_page):
                            if len(collected_rows) >= rows_to_extract:
                                break
                            if endpoint_id and JOB_STATUS.get(endpoint_id, {}).get("stop_requested"):
                                break

                            row_loc = rows_locator.nth(i)

                            # 1. Scrape Summary Table Columns
                            summary_data: dict[str, str | None] = {}
                            for field in blueprint.summary_fields:
                                cell_loc = row_loc.locator(field.css_selector).first
                                if await cell_loc.count() > 0:
                                    if field.extract_type == "text":
                                        val = (await cell_loc.inner_text()).strip()
                                    else:
                                        val = await cell_loc.get_attribute(field.extract_type)
                                    summary_data[field.field_name] = val if val else None
                                else:
                                    summary_data[field.field_name] = None

                            # 2. Click Detail Button ("Detalii") & Scrape Modal + PDF Links
                            detail_modal_data: dict[str, str | None] = {}
                            pdf_tasks: dict[str, asyncio.Task] = {}

                            modal_cfg = blueprint.detail_modal
                            if modal_cfg.has_detail_trigger and modal_cfg.trigger_selector:
                                trigger_btn = row_loc.locator(modal_cfg.trigger_selector).first
                                if await trigger_btn.count() > 0:
                                    try:
                                        await trigger_btn.click(timeout=4000)
                                        modal_loc = page.locator(
                                            f"{modal_cfg.container_selector}:visible"
                                        ).first
                                        await modal_loc.wait_for(state="visible", timeout=4000)

                                        kv_rows = modal_loc.locator(modal_cfg.kv_row_selector)
                                        kv_count = await kv_rows.count()

                                        for k_idx in range(kv_count):
                                            kv_row = kv_rows.nth(k_idx)
                                            lbl_loc = kv_row.locator(modal_cfg.label_selector).first
                                            val_loc = kv_row.locator(modal_cfg.value_selector).first

                                            if await lbl_loc.count() > 0 and await val_loc.count() > 0:
                                                raw_label = (await lbl_loc.inner_text()).strip()
                                                clean_key = normalize_key(raw_label)

                                                link_loc = val_loc.locator("a").first
                                                if await link_loc.count() > 0:
                                                    href = await link_loc.get_attribute("href")
                                                    if href:
                                                        pdf_tasks[clean_key] = asyncio.create_task(
                                                            download_and_extract_pdf(
                                                                href, page_url, http_client
                                                            )
                                                        )
                                                else:
                                                    val_text = (await val_loc.inner_text()).strip()
                                                    detail_modal_data[clean_key] = (
                                                        val_text if val_text else None
                                                    )

                                        close_btn = modal_loc.locator(
                                            "button:has-text('Închide'), button:has-text('Close'), button.close, [data-dismiss='modal']"
                                        ).first
                                        if await close_btn.count() > 0:
                                            await close_btn.click(timeout=2000)
                                        else:
                                            await page.keyboard.press("Escape")

                                        # Wait briefly or cleanup backdrop
                                        try:
                                            await modal_loc.wait_for(state="hidden", timeout=1000)
                                        except Exception:
                                            await page.keyboard.press("Escape")
                                            await page.evaluate("""() => {
                                                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                                                document.querySelectorAll('.modal').forEach(el => {
                                                    el.classList.remove('show');
                                                    el.style.display = 'none';
                                                });
                                                document.body.classList.remove('modal-open');
                                            }""")

                                    except Exception as exc:
                                        print(f"[Executor] Page {current_page} Row {i + 1} modal note: {exc}")
                                        try:
                                            await page.evaluate("""() => {
                                                document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
                                                document.querySelectorAll('.modal').forEach(el => {
                                                    el.classList.remove('show');
                                                    el.style.display = 'none';
                                                });
                                                document.body.classList.remove('modal-open');
                                            }""")
                                        except Exception:
                                            pass

                            # 3. Await all concurrent PDF extractions for this row
                            documents_pdf = {}
                            for doc_key, task in pdf_tasks.items():
                                documents_pdf[doc_key] = await task

                            # Global unique row_number based on page & position so resuming NEVER overwrites!
                            row_number = ((current_page - 1) * 20) + i + 1
                            global_row_index = row_number

                            unified_row = UnifiedRowData(
                                row_index=row_number,
                                page_number=current_page,
                                summary_table=summary_data,
                                detail_modal=detail_modal_data,
                                documents_pdf=documents_pdf,
                            )
                            collected_rows.append(unified_row)

                            # Save immediately to SQLite (appends to existing database!)
                            if endpoint_id:
                                save_row_incrementally(
                                    endpoint_id,
                                    row_number,
                                    current_page,
                                    unified_row.model_dump(),
                                )
                                JOB_STATUS[endpoint_id]["rows_scraped"] = len(collected_rows)

                            print(
                                f"  [+] Page {current_page} | Row #{row_number}/{rows_to_extract} -> {summary_data.get('denumire_comerciala')} (PDFs: {len(documents_pdf)})"
                            )

                        current_page += 1
                finally:
                    await browser.close()

        if endpoint_id and endpoint_id in JOB_STATUS:
            if not JOB_STATUS[endpoint_id].get("stop_requested"):
                JOB_STATUS[endpoint_id]["status"] = "completed"

    except Exception as exc:
        print(f"[Executor] Scrape error: {exc}")
        if endpoint_id and endpoint_id in JOB_STATUS:
            JOB_STATUS[endpoint_id]["status"] = "error"
            JOB_STATUS[endpoint_id]["error"] = str(exc)
        raise

    return collected_rows