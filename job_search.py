#!/usr/bin/env python3
"""
LinkedIn Job Search Bot
=======================
Purpose : Daily LinkedIn job search for Civil/Structural Engineer
Schedule: 8:00 AM Beirut (05:00 UTC) via GitHub Actions
Stack   : Gemini 2.5 Flash (paid) + Google Search grounding → Telegram Bot
Output  : 4 regional job tables + 1 summary message to Telegram
"""

# ─── HERE START: IMPORTS & CONFIG ─────────────────────────────────────────────
import os
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta

from google import genai
from google.genai.types import Tool, GenerateContentConfig, GoogleSearch
import requests
from json_repair import repair_json

# ── API KEYS ──────────────────────────────────────────────────────────────────
# Gemini API Key:
#   Do NOT paste your key here directly.
#   Go to: GitHub repo → Settings → Secrets and variables → Actions
#   Add secret named exactly: GEMINI_API_KEY  → paste your key as the value.
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

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)
# ─── HERE END: IMPORTS & CONFIG ───────────────────────────────────────────────


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
    Failure behavior: defaults to True if file missing or unreadable
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


# ─── HERE START: PROMPT BUILDER ───────────────────────────────────────────────
def build_prompt(region: dict) -> str:
    """
    Build Gemini prompt for one region using fixed target role list.
    No CV content — role-match based suitability only.
    Input : region dict
    Output: complete prompt string
    """
    return f"""
You are a LinkedIn job search assistant for a Lebanese Civil/Structural Engineer seeking international roles.

CANDIDATE BACKGROUND:
- Civil/Structural Engineer with experience in reinforced concrete buildings
- Software: ETABS, SAP2000, AutoCAD, Excel
- Codes: ACI 318, ASCE 7, UBC 97
- Open to full relocation internationally

TARGET ROLES — match jobs against these only:

HIGH PRIORITY:
- Structural Engineer / Senior Structural Engineer
- Construction Manager
- Civil Project Engineer
- Project Manager (construction or buildings)
- Resident Engineer

SECONDARY:
- Site Engineer
- Planning Engineer
- Technical Office Engineer
- Design Engineer (structural or civil)
- Engineer at UN / NGO / International Organization

SEARCH TASK — REGION: {region["name"]}
COUNTRIES: {region["countries"]}

Use Google Search to find real LinkedIn job posts. Run at least 6 searches using these queries
(replace COUNTRIES with relevant country names from the list above):

  site:linkedin.com/jobs "structural engineer" COUNTRIES -internship
  site:linkedin.com/jobs "senior structural engineer" COUNTRIES
  site:linkedin.com/jobs "civil engineer" construction COUNTRIES -internship
  site:linkedin.com/jobs "project manager" construction COUNTRIES
  site:linkedin.com/jobs "construction manager" COUNTRIES
  site:linkedin.com/jobs "resident engineer" COUNTRIES
  site:linkedin.com/jobs "site engineer" COUNTRIES
  site:linkedin.com/jobs "planning engineer" COUNTRIES

IMPORTANT: Do NOT add date strings like "posted 24 hours" to search queries.
Prioritize recent jobs naturally. Include jobs posted in the last 7 days.

EXCLUSIONS:
- Internships, draftsman, MEP, electrical, mechanical, IT/software roles
- Jobs requiring existing local license as hard non-negotiable barrier
- Jobs marked "local candidates only" or no relocation
- Duplicate jobs (same title + company = one entry only)

SUITABILITY SCORING — match job title against target roles:
- 85-100%: Exact match to HIGH PRIORITY role, strong fit
- 70-84%: Good match to HIGH PRIORITY or excellent match to SECONDARY
- 55-69%: Partial match, some gap
- Below 55%: Exclude — do not include in output

EXPAT-FRIENDLY DETECTION:
Check for: visa sponsorship, relocation package, international company, EPC/consultant firm.
Mark: Yes / Possibly / No / Unknown

LINKEDIN URL:
- Include the best LinkedIn job URL you found in search results
- Format: https://www.linkedin.com/jobs/view/[JOB_ID]/
- If no URL found, set "link": "" — do NOT exclude the job for missing URL

OUTPUT: Return ONLY valid JSON. No markdown fences. No code blocks. No text before or after.
Every string value must be on a single line — no literal newlines inside string values.

{{
  "jobs": [
    {{
      "country": "UAE",
      "title": "Senior Structural Engineer",
      "company": "AECOM",
      "suitability": 85,
      "gap": "Bridge design experience preferred",
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


def validate_and_normalize_url(url: str) -> tuple[bool, str]:
    """
    Validate URL format and normalize to canonical LinkedIn job URL.
    Empty URL is allowed — job is kept but marked as no link.
    Input : raw URL string
    Output: (has_valid_url, canonical_url_or_empty)
    """
    if not url:
        return False, ""

    if "linkedin.com/jobs/view/" not in url:
        return False, ""

    job_id = extract_job_id(url)
    if not job_id:
        return False, ""

    return True, f"https://www.linkedin.com/jobs/view/{job_id}/"
# ─── HERE END: URL VALIDATION ─────────────────────────────────────────────────


# ─── HERE START: JSON PARSING ─────────────────────────────────────────────────
def parse_jobs_json(raw: str, region_name: str) -> list[dict]:
    """
    Parse Gemini JSON response using json-repair to handle malformed output.
    Input : raw response text, region name for logging
    Output: list of job dicts (empty list on any failure)

    json-repair handles: unescaped quotes inside strings, trailing commas,
    missing commas, smart quotes, truncated JSON, control characters.
    """
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()

    # Extract outermost JSON object
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        log.error(f"[{region_name}] No JSON object found in response")
        return []

    candidate = text[start:end + 1]

    try:
        # repair_json fixes malformed JSON, then standard json.loads parses it
        repaired = repair_json(candidate)
        data = json.loads(repaired)
    except Exception as e:
        log.error(f"[{region_name}] JSON repair/parse failed: {e}")
        log.debug(f"[{region_name}] Candidate preview: {candidate[:200]}")
        return []

    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        log.error(f"[{region_name}] 'jobs' is not a list")
        return []

    return jobs
# ─── HERE END: JSON PARSING ───────────────────────────────────────────────────


# ─── HERE START: GEMINI SEARCH ────────────────────────────────────────────────
def search_region(client: genai.Client, region: dict) -> list[dict]:
    """
    Search LinkedIn jobs for one region via Gemini + Google Search grounding.
    Input : Gemini client, region dict
    Output: list of validated job dicts (empty on failure)

    Flow:
    1. Build prompt for region
    2. Call Gemini 2.5 Flash with Google Search tool
    3. Log raw response and search queries used
    4. Parse JSON response
    5. Validate and normalize URLs (jobs with no URL are kept)
    6. Return all valid jobs
    """
    region_name = region["name"]
    log.info(f"[{region_name}] Searching...")

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=build_prompt(region),
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())],
                temperature=0.1,
                max_output_tokens=4096,
            ),
        )
    except Exception as e:
        log.error(f"[{region_name}] Gemini error: {e}")
        return []

    # ── Log raw response and search queries for diagnostics ──
    raw_text = (response.text or "").strip()
    log.info(f"[{region_name}] Raw response preview: {raw_text[:300]}")

    try:
        wq = response.candidates[0].grounding_metadata.web_search_queries
        if wq:
            log.info(f"[{region_name}] Google queries used: {len(wq)} queries")
    except Exception:
        pass

    if not raw_text:
        log.error(f"[{region_name}] Empty Gemini response")
        return []

    # ── Parse + validate ──
    jobs      = parse_jobs_json(raw_text, region_name)
    validated = []

    for job in jobs:
        url = job.get("link", "")
        has_url, canonical = validate_and_normalize_url(url)
        job["link"] = canonical if has_url else ""
        validated.append(job)

    log.info(f"[{region_name}] Result: {len(validated)} jobs, "
             f"{sum(1 for j in validated if j['link'])} with links")
    return validated
# ─── HERE END: GEMINI SEARCH ──────────────────────────────────────────────────


# ─── HERE START: TELEGRAM FORMATTING ─────────────────────────────────────────
def priority_emoji(p: str) -> str:
    return {"HIGH": "🟢", "SECONDARY": "🟡"}.get(p.upper(), "⚪")

def country_flag(c: str) -> str:
    return COUNTRY_FLAGS.get(c, "🏳️")

def format_single_job(job: dict) -> str:
    """Format one job as Telegram HTML block."""
    link   = job.get("link", "")
    pri    = job.get("priority", "SECONDARY")
    lines  = [
        f"{priority_emoji(pri)} <b>{job.get('title','?')}</b> — {pri}",
        f"🏢 {job.get('company','?')} | {country_flag(job.get('country',''))} {job.get('country','')}",
        f"📊 Match: {job.get('suitability',0)}%",
        f"⚠️ Gap: {job.get('gap','None')}",
        f"✈️ Expat: {job.get('expat_friendly','?')} | Visa: {job.get('visa_sponsorship','?')}",
    ]
    if job.get("posted"):
        lines.append(f"📅 {job.get('posted')}")
    lines.append(
        f'🔗 <a href="{link}">Apply Now →</a>' if link
        else "🔗 Search manually on LinkedIn"
    )
    return "\n".join(lines)

def format_region_block(region: dict, jobs: list[dict]) -> str:
    """Format all jobs for one region. HIGH priority first, then by suitability."""
    header = f"{region['emoji']} <b>{region['name']} — {len(jobs)} Job(s)</b>\n{'─'*32}"
    if not jobs:
        return f"{header}\nNo suitable jobs found today."
    sorted_jobs = sorted(
        jobs,
        key=lambda j: (0 if j.get("priority","").upper() == "HIGH" else 1,
                       -j.get("suitability", 0))
    )
    return "\n\n".join([header] + [format_single_job(j) for j in sorted_jobs])

def split_to_chunks(text: str, limit: int = TELE_LIMIT) -> list[str]:
    """Split long message into Telegram-safe chunks at newline boundaries."""
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
    Returns True if all chunks sent, False if any chunk failed all retries.
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
    """Build final summary Telegram message with stop/activate instructions."""
    total      = sum(len(j) for _, j in results)
    high_count = sum(1 for _, j in results for x in j
                     if x.get("priority","").upper() == "HIGH")
    best       = max(results, key=lambda r: len(r[1]), default=(None, []))
    best_label = (f"{best[0]['emoji']} {best[0]['name']} ({len(best[1])} jobs)"
                  if best[0] and best[1] else "None")
    gaps       = [x.get("gap","") for _, j in results for x in j
                  if x.get("gap","") not in ("", "None")]
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
    2. Notify Telegram search is starting
    3. Search each of 4 regions via Gemini
    4. Send regional results to Telegram
    5. Send summary with stop/activate footer
    """
    log.info("=" * 50)
    log.info("LinkedIn Job Search Bot — Starting")
    log.info("=" * 50)

    if not is_active():
        log.info("Reports stopped by user. Exiting.")
        return

    now_beirut = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M")
    client     = genai.Client(api_key=GEMINI_API_KEY)

    send_telegram(
        f"🔍 <b>Daily Job Search Started</b>\n"
        f"📅 {now_beirut} (Beirut)\n"
        f"🌐 Searching 4 regions..."
    )

    all_results: list[tuple[dict, list[dict]]] = []

    for region in REGIONS:
        try:
            jobs = search_region(client, region)
            all_results.append((region, jobs))
            send_telegram(format_region_block(region, jobs))
            time.sleep(6)
        except Exception as e:
            log.error(f"[{region['name']}] Error: {e}", exc_info=True)
            send_telegram(f"⚠️ {region['emoji']} {region['name']}: failed — {str(e)[:100]}")
            all_results.append((region, []))

    send_telegram(build_summary(all_results, now_beirut))
    log.info(f"Done — {sum(len(j) for _, j in all_results)} total jobs")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
# ─── HERE END: MAIN ───────────────────────────────────────────────────────────
