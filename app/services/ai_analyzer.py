import json
import re
from urllib.parse import urlparse
from app.core.config import (
    BLUEPRINT_CACHE_DIR,
    NVIDIA_MODEL,
    nvidia_client,
    nvidia_limiter,
)
from app.schemas.models import (
    DetailModalInstruction,
    ExtractionBlueprint,
    FieldInstruction,
    PaginationInstruction,
)
from app.services.dom_probe import probe_website_samples
from app.services.pdf_extractor import normalize_key


def _extract_clean_json(raw_text: str) -> dict:
    """Strips <think>...</think> reasoning blocks and markdown fences before parsing JSON."""
    if not raw_text:
        raise ValueError("Empty response from LLM")

    # Remove <think>...</think> blocks produced by Nemotron / DeepSeek reasoning models
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()

    # Extract JSON inside ```json ... ``` if present
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)

    # Find first '{' and last '}'
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1:
        cleaned = cleaned[start_idx : end_idx + 1]

    return json.loads(cleaned)


def _build_fallback_blueprint(probe_data: dict) -> ExtractionBlueprint:
    """
    Deterministic fallback builder: Uses the headers and modal structure discovered by
    Playwright in dom_probe.py if the NVIDIA model times out or outputs invalid schema keys.
    """
    summary_fields: list[FieldInstruction] = []
    headers = probe_data.get("detected_headers", [])

    if headers:
        for idx, h_text in enumerate(headers, start=1):
            # Skip empty header column (e.g. column 1 which holds the "Detalii" button)
            if not h_text or h_text.lower() in {"detalii", "actions", "actiuni"}:
                continue
            summary_fields.append(
                FieldInstruction(
                    field_name=normalize_key(h_text),
                    css_selector=f"td:nth-child({idx})",
                    extract_type="text",
                )
            )
    else:
        # Default 6 columns if table has no <th> headers
        for idx in range(2, 8):
            summary_fields.append(
                FieldInstruction(
                    field_name=f"column_{idx}",
                    css_selector=f"td:nth-child({idx})",
                    extract_type="text",
                )
            )

    return ExtractionBlueprint(
        domain=probe_data["domain"],
        repeating_row_selector="table tbody tr",
        pagination=PaginationInstruction(
            url_template=probe_data["url_template"],
            rows_per_page=probe_data["rows_per_page_hint"],
            total_pages_detected=probe_data["max_page_hint"],
        ),
        summary_fields=summary_fields,
        detail_modal=DetailModalInstruction(
            has_detail_trigger=probe_data["has_modal"],
            trigger_selector="td:nth-child(1) button, button:has-text('Detalii'), a:has-text('Detalii')",
            container_selector=".modal-content",
            close_selector=".modal-footer button, button.close, button:has-text('Închide')",
            kv_row_selector="table tr",
            label_selector="td:nth-child(1), th:nth-child(1)",
            value_selector="td:nth-child(2)",
        ),
    )


async def generate_or_load_blueprint(url: str, force_reanalyze: bool = False) -> ExtractionBlueprint:
    """
    Loads cached blueprint or analyzes 5-10 sample rows + modal with NVIDIA NIM,
    with automatic fallback so /discover never throws a 500 error.
    """
    domain = urlparse(url).netloc.replace(":", "_")
    cache_file = BLUEPRINT_CACHE_DIR / f"{domain}.json"

    if cache_file.exists() and not force_reanalyze:
        print(f"[AI Analyzer] Loaded cached blueprint for {domain}")
        cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
        return ExtractionBlueprint.model_validate(cached_data)

    probe_data = await probe_website_samples(url)
    schema_json = json.dumps(ExtractionBlueprint.model_json_schema(), indent=2)

    prompt = f"""
Analyze the 5-10 sample rows and modal HTML below and return ONLY a valid JSON object matching the ExtractionBlueprint schema.
Target URL: {url}
Domain: {probe_data['domain']}
Pagination URL Template: {probe_data['url_template']}
Detected Max Page: {probe_data['max_page_hint']}
Detected Rows Per Page: {probe_data['rows_per_page_hint']}
Detected Column Headers: {probe_data['detected_headers']}

Main Table HTML Sample:
{probe_data['main_samples_html']}

Clicked Detail Modal HTML Sample:
{json.dumps(probe_data['modal_samples_html'], ensure_ascii=False)}

JSON Schema Required:
{schema_json}
"""

    blueprint = None
    try:
        print(f"[AI Analyzer] Invoking NVIDIA NIM model ({NVIDIA_MODEL}) ...")
        async with nvidia_limiter:
            response = await nvidia_client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Return ONLY valid JSON matching the schema. Do not include markdown or explanations.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=4096,
            )
        raw_content = response.choices[0].message.content or ""
        blueprint_dict = _extract_clean_json(raw_content)
        blueprint = ExtractionBlueprint.model_validate(blueprint_dict)
        # Ensure accurate pagination math from DOM probe
        if blueprint.pagination.total_pages_detected < probe_data["max_page_hint"]:
            blueprint.pagination.total_pages_detected = probe_data["max_page_hint"]
        print("[AI Analyzer] Successfully generated AI Blueprint!")
    except Exception as exc:
        print(f"[AI Analyzer] LLM output fallback triggered ({exc}) -> Using verified DOM Probe Blueprint.")
        blueprint = _build_fallback_blueprint(probe_data)

    cache_file.write_text(
        json.dumps(blueprint.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return blueprint