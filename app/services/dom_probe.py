import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from bs4 import BeautifulSoup, Comment
from playwright.async_api import async_playwright


def clean_html_snippet(raw_html: str, max_length: int = 14000) -> str:
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script", "style", "svg", "noscript", "iframe", "link", "meta"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    allowed_attrs = {"class", "id", "href", "role", "type", "data-toggle", "data-target"}
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}
    cleaned = re.sub(r"\s+", " ", str(soup)).strip()
    return cleaned[:max_length]


def detect_url_page_number(url: str) -> int:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ["page", "p", "pagina", "pg"]:
        if key in qs and qs[key][0].isdigit():
            return int(qs[key][0])
    return 1


def build_pagination_template(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["page"] = ["__PAGE_PLACEHOLDER__"]
    new_query = urlencode(qs, doseq=True).replace("__PAGE_PLACEHOLDER__", "{page}")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))


async def probe_website_samples(url: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        print(f"[DOM Probe] Navigating to {url} ...")
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(500)

        # 1. Detect Max Page Number from Pagination Bar
        pagination_links = await page.locator("ul.pagination a, nav a, .pagination a, a[href*='page=']").all_inner_texts()
        numeric_pages = [int(t.strip()) for t in pagination_links if t.strip().isdigit()]
        max_page_detected = max(numeric_pages) if numeric_pages else 1

        # 2. Strictly target ONLY the first main data table (excluding hidden modal tables!)
        main_table = page.locator("table").first
        rows_per_page = await main_table.locator("tbody tr").count()
        if rows_per_page == 0:
            rows_per_page = 10

        # Use JS evaluation for robust header extraction (handles JS-rendered headers)
        raw_headers: list[str] = await page.evaluate("""
            () => {
                const table = document.querySelector('table');
                if (!table) return [];
                const ths = table.querySelectorAll('thead th, tr:first-child th');
                return Array.from(ths).map(th => th.innerText.trim());
            }
        """)
        cleaned_headers = [
            h.replace("\u25b2", "").replace("\u25bc", "").strip()
            for h in raw_headers
            if h.strip()
        ]

        table_html = await main_table.evaluate("el => el.outerHTML")
        cleaned_main_samples = clean_html_snippet(table_html, max_length=14000)

        # 3. Probe-Click the First Row's "Detalii" Button
        modal_samples = []
        has_modal = False
        candidate_buttons = main_table.locator("tbody tr button, tbody tr a.btn")
        if await candidate_buttons.count() > 0:
            try:
                await candidate_buttons.first.click(timeout=4000)
                await page.wait_for_timeout(800)
                modal_loc = page.locator(".modal-content:visible, .modal:visible, [role='dialog']:visible").first
                if await modal_loc.count() > 0:
                    has_modal = True
                    raw_modal = await modal_loc.evaluate("el => el.outerHTML")
                    modal_samples.append(clean_html_snippet(raw_modal, max_length=7000))
            except Exception as exc:
                print(f"[DOM Probe] Modal click note: {exc}")

        await browser.close()

    return {
        "url": url,
        "domain": urlparse(url).netloc,
        "url_template": build_pagination_template(url),
        "max_page_hint": max_page_detected,
        "rows_per_page_hint": rows_per_page,
        "detected_headers": cleaned_headers,
        "has_modal": has_modal,
        "main_samples_html": cleaned_main_samples,
        "modal_samples_html": modal_samples,
    }