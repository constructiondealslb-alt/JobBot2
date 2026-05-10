#!/usr/bin/env python3
"""
LinkedIn Job Search Bot
=======================
Purpose : Daily LinkedIn job search for Civil/Structural Engineer
Schedule: 8:00 AM Beirut (05:00 UTC) via GitHub Actions
Stack   : Gemini 2.0 Flash (free) + Google Search grounding → Telegram Bot
Output  : 4 regional job tables + 1 summary message to Telegram
"""

# ─── HERE START: IMPORTS & CONFIG ─────────────────────────────────────────────
import os
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta

import fitz  # PyMuPDF — PDF text extraction
from google import genai
from google.genai.types import Tool, GenerateContentConfig, GoogleSearch
import requests

# ── API KEYS ──────────────────────────────────────────────────────────────────
# Gemini API Key:
#   Do NOT paste your key here directly.
#   Go to: GitHub repo → Settings → Secrets and variables → Actions
#   Add secret named exactly: GEMINI_API_KEY  → paste your key as the value.
#   The line below reads it automatically at runtime.
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Telegram credentials — same process, add as GitHub Secrets:
#   TELEGRAM_BOT_TOKEN   (from @BotFather)
#   TELEGRAM_CHAT_ID     (from @userinfobot)
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# ──────────────────────────────────────────────────────────────────────────────

BEIRUT_TZ  = timezone(timedelta(hours=3))
MODEL      = "gemini-2.5-flash"
TELE_URL   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
TELE_LIMIT = 4096
STATE_FILE = "state.json"
CVS_DIR    = "cvs"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)
# ─── HERE END: IMPORTS & CONFIG ───────────────────────────────────────────────


# ─── HERE START: CV SLOTS ─────────────────────────────────────────────────────
# CV files are auto-detected from the cvs/ folder, sorted alphabetically.
# Place exactly 4 PDF files in cvs/ and name them so they sort in the order
# you want. Slot assignment is strictly positional (alphabetical = slot order).
#
# CV A SLOT → 1st file alphabetically   ← PUT CV A HERE  (e.g. CV_A_Structural.pdf)
# CV B SLOT → 2nd file alphabetically   ← PUT CV B HERE  (e.g. CV_B_Construction.pdf)
# CV C SLOT → 3rd file alphabetically   ← PUT CV C HERE  (e.g. CV_C_Civil.pdf)
# CV D SLOT → 4th file alphabetically   ← PUT CV D HERE  (e.g. CV_D_International.pdf)
#
# To reassign a slot: rename the file so it sorts in the desired position.
# Only the first 4 PDFs are used. Extra files beyond 4 are ignored.
# ─── HERE END: CV SLOTS ───────────────────────────────────────────────────────


# ─── HERE START: REGIONS & FLAGS ──────────────────────────────────────────────
REGIONS = [
    {
        "name": "CENTRAL & WEST AFRICA",
        "emoji": "🌍",
        "countries": "Côte d'Ivoire, Senegal, Cameroon, Gabon, Congo, DRC, Ghana, Nigeria, Angola, Rwanda, Kenya, Tanzania",
    },
    {
        "name": "CYPRUS",
        "emoji": "🏛️",
        "countries": "Cyprus",
    },
    {
        "name": "GCC",
        "emoji": "🌅",
        "countries": "UAE, Saudi Arabia, Qatar, Kuwait, Bahrain, Oman",
    },
    {
        "name": "EUROPE",
        "emoji": "🇪🇺",
        "countries": "Germany, Netherlands, Belgium, France, Switzerland, Austria, Sweden, Norway, Denmark, Ireland, Poland, Portugal, Spain, Italy",
    },
]

COUNTRY_FLAGS = {
    "UAE": "🇦🇪", "Saudi Arabia": "🇸🇦", "Qatar": "🇶🇦", "Kuwait": "🇰🇼",
    "Bahrain": "🇧🇭", "Oman": "🇴🇲", "Germany": "🇩🇪", "Netherlands": "🇳🇱",
    "Belgium": "🇧🇪", "France": "🇫🇷", "Switzerland": "🇨🇭", "Austria": "🇦🇹",
    "Sweden": "🇸🇪", "Norway": "🇳🇴", "Denmark": "🇩🇰", "Ireland": "🇮🇪",
    "Poland": "🇵🇱", "Portugal": "🇵🇹", "Spain": "🇪🇸", "Italy": "🇮🇹",
    "Cyprus": "🇨🇾", "Ghana": "🇬🇭", "Nigeria": "🇳🇬", "Kenya": "🇰🇪",
    "Tanzania": "🇹🇿", "Rwanda": "🇷🇼", "Angola": "🇦🇴", "Cameroon": "🇨🇲",
    "Senegal": "🇸🇳", "Gabon": "🇬🇦", "Congo": "🇨🇬", "DRC": "🇨🇩",
    "Côte d'Ivoire": "🇨🇮",
}
# ─── HERE END: REGIONS & FLAGS ────────────────────────────────────────────────


# ─── HERE START: STATE CHECK ──────────────────────────────────────────────────
def is_active() -> bool:
    """
    Read state.json to check if daily reports are enabled.
    Output: True = run normally | False = exit without sending
    Failure behavior: defaults to True if file is missing or unreadable
    """
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        active = state.get("active", True)
        log.info(f"State: active={active}")
        return active
    except Exception as e:
        log.warning(f"Could not read {STATE_FILE}: {e} — defaulting to active")
        return True
# ─── HERE END: STATE CHECK ────────────────────────────────────────────────────


# ─── HERE START: CV LOADING ───────────────────────────────────────────────────
def load_cv_texts(cvs_dir: str = CVS_DIR) -> dict[str, str]:
    """
    Auto-detect CV PDFs from cvs/ folder and extract text via PyMuPDF.
    Input : path to cvs/ directory
    Output: dict mapping slot name to extracted text e.g. {"CV A": "...", ...}

    Flow:
    1. List PDFs in cvs/ sorted alphabetically
    2. Assign first 4 to CV A → B → C → D
    3. Extract full text per PDF via PyMuPDF
    4. Truncate each to 2500 chars to stay within Gemini token limits
    """
    slots    = ["CV A", "CV B", "CV C", "CV D"]
    cv_texts = {}

    if not os.path.isdir(cvs_dir):
        log.warning(f"cvs/ folder not found — CV matching will use generic descriptions")
        return {}

    pdf_files = sorted(f for f in os.listdir(cvs_dir) if f.lower().endswith(".pdf"))

    if not pdf_files:
        log.warning(f"No PDF files found in {cvs_dir}/")
        return {}

    log.info(f"CV files detected: {pdf_files}")

    for i, filename in enumerate(pdf_files[:4]):
        slot = slots[i]
        path = os.path.join(cvs_dir, filename)
        try:
            doc  = fitz.open(path)
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            cv_texts[slot] = text[:2500]
            log.info(f"Loaded {slot} ← {filename} ({len(text)} chars)")
        except Exception as e:
            log.error(f"Failed to read {filename}: {e}")

    return cv_texts
# ─── HERE END: CV LOADING ─────────────────────────────────────────────────────


# ─── HERE START: PROMPT BUILDER ───────────────────────────────────────────────
def build_prompt(region: dict, cv_texts: dict[str, str]) -> str:
    """
    Build full Gemini prompt for one region, embedding actual CV content.
    Input : region dict, cv_texts from load_cv_texts()
    Output: complete prompt string
    """
    if cv_texts:
        cv_block = "\n".join(
            f"--- {slot} (actual CV content) ---\n{text}\n"
            for slot, text in cv_texts.items()
        )
        cv_instructions = (
            "Read the ACTUAL CV CONTENT above carefully. "
            "Match each job against the real skills, tools, and experience in each CV. "
            "Select the CV whose content best aligns with the job requirements."
        )
    else:
        cv_block = (
            "CV A — Structural Design: ETABS 21, SAP2000, ACI 318-19, structural calculations\n"
            "CV B — Construction Management: site supervision, project management, QA/QC\n"
            "CV C — Civil / Infrastructure: civil works, roads, drainage, utilities\n"
            "CV D — International / Expat: international EPC, multi-discipline, adaptability\n"
        )
        cv_instructions = "Match each job against the CV descriptions and select the best fit."

    return f"""
You are a LinkedIn job search assistant for a Lebanese Civil/Structural Engineer.

CANDIDATE CVs:
{cv_block}

CV MATCHING INSTRUCTIONS:
{cv_instructions}

SEARCH TASK — REGION: {region["name"]}
COUNTRIES: {region["countries"]}

Use Google Search to find REAL LinkedIn job posts. Run AT LEAST 6 searches:
  site:linkedin.com/jobs "structural engineer" [{region["countries"]}] -internship
  site:linkedin.com/jobs "senior structural engineer" [{region["countries"]}]
  site:linkedin.com/jobs "civil engineer" construction [{region["countries"]}] -internship
  site:linkedin.com/jobs "project manager" construction [{region["countries"]}]
  site:linkedin.com/jobs "construction manager" [{region["countries"]}]
  site:linkedin.com/jobs "resident engineer" [{region["countries"]}]
  site:linkedin.com/jobs "technical office engineer" [{region["countries"]}]
  site:linkedin.com/jobs "planning engineer" construction [{region["countries"]}]

DATE FILTER: Last 48 hours only.

EXCLUSIONS:
- Internships, draftsman, MEP, electrical, mechanical, IT/software roles
- Jobs requiring existing local license as hard barrier
- "Local candidates only" or "no relocation" jobs
- Duplicates (same title + company = one entry)
- Any job with no valid LinkedIn URL found in search results

LINKEDIN URL RULES:
- Search for and include the best available LinkedIn job URL
- Preferred format: https://www.linkedin.com/jobs/view/[JOB_ID]/
- Include your best available URL even if not 100% certain
- If truly no URL found, set "link": "" — do NOT exclude the job for missing URL
- Do not fabricate numeric job IDs you did not find in search results

SUITABILITY: 85-100% excellent | 70-84% good | 55-69% partial | below 55% exclude

EXPAT-FRIENDLY: Check for visa sponsorship, relocation, expat package, international firm.
Mark: Yes / Possibly / No / Unknown

PRIORITY:
HIGH     : Structural Engineer, Senior Structural Engineer, Project Manager, Construction Manager, Civil Project Engineer
SECONDARY: Site Engineer, Planning Engineer, Design Engineer, Resident Engineer, Technical Office Engineer

OUTPUT: Return ONLY valid JSON. No markdown. No code blocks. Nothing else.

{{
  "jobs": [
    {{
      "country": "UAE",
      "title": "Senior Structural Engineer",
      "company": "AECOM",
      "suitability": 85,
      "best_cv": "CV A",
      "gap": "PE license preferred",
      "expat_friendly": "Yes",
      "visa_sponsorship": "Likely",
      "posted": "Today",
      "link": "https://www.linkedin.com/jobs/view/3945678901/",
      "priority": "HIGH"
    }}
  ]
}}

If no valid jobs found: {{"jobs": []}}
"""
# ─── HERE END: PROMPT BUILDER ─────────────────────────────────────────────────


# ─── HERE START: URL VALIDATION ───────────────────────────────────────────────
def extract_job_id(url: str) -> str | None:
    """Extract numeric LinkedIn job ID (8+ digits) from URL."""
    match = re.search(r"/jobs/view/[^/?#]*?(\d{8,})", url)
    return match.group(1) if match else None


def validate_and_normalize_url(url: str, grounding_urls: set) -> tuple[bool, str]:
    """
    Validate URL format and normalize to canonical LinkedIn job URL.
    Input : raw URL, set of URLs confirmed by Google Search grounding
    Output: (is_valid, canonical_url)
    """
    if not url or "linkedin.com/jobs/view/" not in url:
        return False, url

    job_id = extract_job_id(url)
    if not job_id:
        return False, url

    canonical = f"https://www.linkedin.com/jobs/view/{job_id}/"

    if grounding_urls:
        if not any(job_id in g for g in grounding_urls):
            log.debug(f"Job ID {job_id} not in grounding sources — accepted on format")

    return True, canonical
# ─── HERE END: URL VALIDATION ─────────────────────────────────────────────────


# ─── HERE START: JSON PARSING ─────────────────────────────────────────────────
def parse_jobs_json(raw: str, region_name: str) -> list[dict]:
    """
    Parse Gemini JSON response into list of job dicts.
    Strips markdown fences, extracts outermost JSON object, parses jobs array.
    Returns empty list on any failure.
    """
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        log.error(f"[{region_name}] No JSON object found in response")
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        log.error(f"[{region_name}] JSON parse failed: {e}")
        return []
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        log.error(f"[{region_name}] 'jobs' is not a list")
        return []
    return jobs
# ─── HERE END: JSON PARSING ───────────────────────────────────────────────────


# ─── HERE START: GEMINI SEARCH ────────────────────────────────────────────────
def search_region(client: genai.Client, region: dict, cv_texts: dict) -> list[dict]:
    """
    Search LinkedIn jobs for one region via Gemini + Google Search grounding.
    Input : Gemini client, region dict, cv_texts dict
    Output: list of validated job dicts (empty on failure)

    Flow:
    1. Build prompt with region + CV content
    2. Call Gemini 2.0 Flash with Google Search tool
    3. Extract real URLs from grounding metadata
    4. Parse + validate job list
    5. Return only jobs with valid LinkedIn URLs
    """
    region_name = region["name"]
    log.info(f"[{region_name}] Searching...")

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=build_prompt(region, cv_texts),
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())],
                temperature=0.1,
                max_output_tokens=4096,
            ),
        )
    except Exception as e:
        log.error(f"[{region_name}] Gemini error: {e}")
        return []

    grounding_urls: set[str] = set()
    try:
        meta = response.candidates[0].grounding_metadata
        if meta and meta.grounding_chunks:
            for chunk in meta.grounding_chunks:
                if hasattr(chunk, "web") and chunk.web and chunk.web.uri:
                    grounding_urls.add(chunk.web.uri)
        log.info(f"[{region_name}] Grounding URLs: {len(grounding_urls)}")
    except (AttributeError, IndexError) as e:
        log.warning(f"[{region_name}] Grounding metadata unavailable: {e}")

    raw_text = (response.text or "").strip()
    log.info(f"[{region_name}] Raw response preview: {raw_text[:400]}")
    # Log search queries used by grounding (if any)
    try:
        wq = response.candidates[0].grounding_metadata.web_search_queries
        if wq:
            log.info(f"[{region_name}] Google queries used: {wq}")
    except Exception:
        pass
    if not raw_text:
        log.error(f"[{region_name}] Empty Gemini response")
        return []

    jobs      = parse_jobs_json(raw_text, region_name)
    validated = []
    excluded  = 0

    for job in jobs:
        ok, canonical = validate_and_normalize_url(job.get("link", ""), grounding_urls)
        if ok:
            job["link"] = canonical
            validated.append(job)
        else:
            excluded += 1
            log.warning(f"[{region_name}] Excluded: {job.get('title')} @ {job.get('company')} | url='{job.get('link')}'")

    log.info(f"[{region_name}] Result: {len(validated)} valid, {excluded} excluded")
    return validated
# ─── HERE END: GEMINI SEARCH ──────────────────────────────────────────────────


# ─── HERE START: TELEGRAM FORMATTING ─────────────────────────────────────────
def priority_emoji(p: str) -> str:
    return {"HIGH": "🟢", "SECONDARY": "🟡"}.get(p.upper(), "⚪")

def country_flag(c: str) -> str:
    return COUNTRY_FLAGS.get(c, "🏳️")

def format_single_job(job: dict) -> str:
    link = job.get("link", "")
    lines = [
        f"{priority_emoji(job.get('priority','SECONDARY'))} <b>{job.get('title','?')}</b> — {job.get('priority','?')}",
        f"🏢 {job.get('company','?')} | {country_flag(job.get('country',''))} {job.get('country','')}",
        f"📊 Match: {job.get('suitability',0)}% | Best CV: {job.get('best_cv','')}",
        f"⚠️ Gap: {job.get('gap','None')}",
        f"✈️ Expat: {job.get('expat_friendly','?')} | Visa: {job.get('visa_sponsorship','?')}",
        f"📅 {job.get('posted','')}" if job.get("posted") else "",
        f'🔗 <a href="{link}">Apply Now →</a>' if link else "🔗 Link unavailable",
    ]
    return "\n".join(l for l in lines if l)

def format_region_block(region: dict, jobs: list[dict]) -> str:
    header = f"{region['emoji']} <b>{region['name']} — {len(jobs)} Job(s)</b>\n{'─'*32}"
    if not jobs:
        return f"{header}\nNo suitable jobs found today."
    sorted_jobs = sorted(
        jobs,
        key=lambda j: (0 if j.get("priority","").upper() == "HIGH" else 1, -j.get("suitability", 0))
    )
    return "\n\n".join([header] + [format_single_job(j) for j in sorted_jobs])

def split_to_chunks(text: str, limit: int = TELE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text); break
        cut = text.rfind("\n", 0, limit)
        if cut == -1: cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks
# ─── HERE END: TELEGRAM FORMATTING ───────────────────────────────────────────


# ─── HERE START: TELEGRAM SENDING ─────────────────────────────────────────────
def send_telegram(text: str, retries: int = 3) -> bool:
    """
    Send HTML message to Telegram with chunking and retry.
    Returns True if all chunks sent, False if any failed.
    """
    all_ok = True
    for i, chunk in enumerate(split_to_chunks(text), 1):
        sent = False
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(
                    TELE_URL,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                          "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=15,
                )
                if r.status_code == 200:
                    log.info(f"Telegram chunk {i}: sent ({len(chunk)} chars)")
                    time.sleep(1); sent = True; break
                else:
                    log.warning(f"Telegram chunk {i} attempt {attempt}: HTTP {r.status_code}")
            except requests.RequestException as e:
                log.warning(f"Telegram chunk {i} attempt {attempt}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
        if not sent:
            log.error(f"Telegram chunk {i}: all retries failed")
            all_ok = False
    return all_ok
# ─── HERE END: TELEGRAM SENDING ───────────────────────────────────────────────


# ─── HERE START: SUMMARY ──────────────────────────────────────────────────────
def build_summary(results: list[tuple[dict, list[dict]]], run_time: str) -> str:
    """Build final summary message with stop/activate instructions."""
    total      = sum(len(j) for _, j in results)
    high_count = sum(1 for _, j in results for x in j if x.get("priority","").upper() == "HIGH")
    best       = max(results, key=lambda r: len(r[1]), default=(None, []))
    best_label = f"{best[0]['emoji']} {best[0]['name']} ({len(best[1])} jobs)" if best[0] and best[1] else "None"
    gaps       = [x.get("gap","") for _,j in results for x in j if x.get("gap","") not in ("","None")]
    common_gap = max(set(gaps), key=gaps.count) if gaps else "None detected"

    return "\n".join([
        f"📋 <b>Daily Job Report — {run_time}</b>",
        f"{'━'*30}",
        f"📌 Total jobs found: <b>{total}</b>",
        f"🟢 HIGH priority: <b>{high_count}</b>",
        f"🏆 Best region: {best_label}",
        f"⚠️ Common gap: {common_gap}",
        f"⏰ Beirut time: {run_time}",
        f"{'━'*30}",
        f"<i>Summary End.</i>",
        f"<i>To stop daily reports → send: <b>stop</b></i>",
        f"<i>To reactivate daily reports → send: <b>activate</b></i>",
    ])
# ─── HERE END: SUMMARY ────────────────────────────────────────────────────────


# ─── HERE START: MAIN ─────────────────────────────────────────────────────────
def main():
    """
    Flow:
    1. Check state.json → exit silently if stopped
    2. Load CV PDFs from cvs/
    3. Notify Telegram — search starting
    4. Search each of 4 regions via Gemini
    5. Send regional results to Telegram
    6. Send summary with stop/activate footer
    """
    log.info("=" * 50)
    log.info("LinkedIn Job Search Bot — Starting")
    log.info("=" * 50)

    if not is_active():
        log.info("Reports stopped by user. Exiting.")
        return

    now_beirut = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M")
    cv_texts   = load_cv_texts()
    cv_status  = f"{len(cv_texts)} CV(s) loaded" if cv_texts else "No CVs — using descriptions"
    client     = genai.Client(api_key=GEMINI_API_KEY)

    send_telegram(
        f"🔍 <b>Daily Job Search Started</b>\n"
        f"📅 {now_beirut} (Beirut)\n"
        f"📄 {cv_status}\n"
        f"🌐 Searching 4 regions..."
    )

    all_results: list[tuple[dict, list[dict]]] = []

    for region in REGIONS:
        try:
            jobs = search_region(client, region, cv_texts)
            all_results.append((region, jobs))
            send_telegram(format_region_block(region, jobs))
            time.sleep(6)
        except Exception as e:
            log.error(f"[{region['name']}] Error: {e}", exc_info=True)
            send_telegram(f"⚠️ {region['emoji']} {region['name']}: failed — {str(e)[:100]}")
            all_results.append((region, []))

    send_telegram(build_summary(all_results, now_beirut))
    log.info(f"Done — {sum(len(j) for _,j in all_results)} total jobs")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
# ─── HERE END: MAIN ───────────────────────────────────────────────────────────
