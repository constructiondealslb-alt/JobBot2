#!/usr/bin/env python3
"""
LinkedIn + Adzuna Job Search Bot
================================
Purpose : Daily job search for Civil/Structural Engineer
Schedule: 8:00 AM Beirut via GitHub Actions (configurable cron)
Stack   : DuckDuckGo (LinkedIn search) + Adzuna API (job aggregator)
          → Gemini 2.5 Flash for filtering/scoring → Telegram Bot
Sources : DuckDuckGo returns real LinkedIn URLs (works for all regions)
          Adzuna returns real jobs from multiple boards (Europe-only countries)
"""

# ─── HERE START: IMPORTS & CONFIG ─────────────────────────────────────────────
import os
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta

from google import genai
from google.genai.types import GenerateContentConfig
from json_repair import repair_json
from duckduckgo_search import DDGS
import requests

# ── REQUIRED API KEYS (GitHub Secrets) ────────────────────────────────────────
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── OPTIONAL: Adzuna keys — bot still works without them, just no Adzuna data
ADZUNA_APP_ID  = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
# ──────────────────────────────────────────────────────────────────────────────

BEIRUT_TZ      = timezone(timedelta(hours=3))
MODEL          = "gemini-2.5-flash"
TELE_URL       = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
TELE_LIMIT     = 4096
STATE_FILE     = "state.json"
DDG_DELAY      = 2.0    # Seconds between DDG queries (rate limit safety)
ADZUNA_DELAY   = 1.5    # Seconds between Adzuna calls
MAX_JOBS_TO_AI = 60     # Max jobs sent to Gemini per region (token budget)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)
# ─── HERE END: IMPORTS & CONFIG ───────────────────────────────────────────────


# ─── HERE START: REGIONS, COUNTRIES, FLAGS ────────────────────────────────────
REGIONS = [
    {
        "name": "CENTRAL & WEST AFRICA",
        "emoji": "🌍",
        "countries": "Côte d'Ivoire, Senegal, Cameroon, Gabon, Congo, DRC, Ghana, Nigeria, Angola, Rwanda, Kenya, Tanzania",
    },
    {"name": "CYPRUS", "emoji": "🏛️", "countries": "Cyprus"},
    {"name": "GCC", "emoji": "🌅", "countries": "UAE, Saudi Arabia, Qatar, Kuwait, Bahrain, Oman"},
    {
        "name": "EUROPE",
        "emoji": "🇪🇺",
        "countries": "Germany, Netherlands, Belgium, France, Switzerland, Austria, Sweden, Norway, Denmark, Ireland, Poland, Portugal, Spain, Italy",
    },
]

# Adzuna only supports these countries from our list — others handled by DDG only
ADZUNA_COUNTRIES = {
    "Germany": "de", "Netherlands": "nl", "Belgium": "be", "France": "fr",
    "Switzerland": "ch", "Austria": "at", "Spain": "es", "Italy": "it",
    "Poland": "pl",
}

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
# ─── HERE END: REGIONS, COUNTRIES, FLAGS ──────────────────────────────────────


# ─── HERE START: STATE CHECK ──────────────────────────────────────────────────
def is_active() -> bool:
    """Read state.json. Returns True if reports active, False if stopped."""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f).get("active", True)
    except Exception as e:
        log.warning(f"Could not read {STATE_FILE}: {e} — defaulting to active")
        return True
# ─── HERE END: STATE CHECK ────────────────────────────────────────────────────


# ─── HERE START: DUCKDUCKGO SEARCH ────────────────────────────────────────────
def search_ddg_for_region(region: dict) -> list[dict]:
    """
    Search LinkedIn via DuckDuckGo for one region.
    Input : region dict with name + countries
    Output: list of {url, title, snippet, source}

    Returns ONLY real LinkedIn job-view URLs from DDG's index.
    Deduplicates by URL across all queries.
    """
    region_name = region["name"]
    countries   = region["countries"]

    queries = [
        f'site:linkedin.com/jobs "structural engineer" {countries}',
        f'site:linkedin.com/jobs "senior structural engineer" {countries}',
        f'site:linkedin.com/jobs "civil engineer" construction {countries}',
        f'site:linkedin.com/jobs "project manager" construction {countries}',
        f'site:linkedin.com/jobs "construction manager" {countries}',
        f'site:linkedin.com/jobs "site engineer" {countries}',
        f'site:linkedin.com/jobs "resident engineer" {countries}',
        f'site:linkedin.com/jobs "planning engineer" {countries}',
    ]

    results: list[dict] = []
    seen_urls: set[str] = set()

    for q_idx, query in enumerate(queries, 1):
        try:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, max_results=10, timelimit="m"))
        except Exception as e:
            log.warning(f"[{region_name}] DDG query {q_idx} failed: {e}")
            time.sleep(DDG_DELAY * 2)
            continue

        kept_this_query = 0
        for hit in hits:
            url = hit.get("href", "")
            if not url or "linkedin.com/jobs/view/" not in url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({
                "source":  "DDG/LinkedIn",
                "url":     url,
                "title":   hit.get("title", "")[:200],
                "snippet": hit.get("body", "")[:400],
            })
            kept_this_query += 1

        log.info(f"[{region_name}] DDG q{q_idx}: {kept_this_query} kept "
                 f"(total unique: {len(results)})")
        time.sleep(DDG_DELAY)

    log.info(f"[{region_name}] DDG total unique LinkedIn URLs: {len(results)}")
    return results
# ─── HERE END: DUCKDUCKGO SEARCH ──────────────────────────────────────────────


# ─── HERE START: ADZUNA SEARCH ────────────────────────────────────────────────
def search_adzuna_for_country(country_name: str, country_code: str) -> list[dict]:
    """
    Query Adzuna API for one country.
    Input : display country name (e.g. "Germany"), Adzuna code (e.g. "de")
    Output: list of {url, title, company, country, snippet, posted, source}
    Failure behavior: returns empty list if API fails or keys missing
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []

    url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
    keywords = (
        '"structural engineer" OR "senior structural engineer" OR '
        '"civil engineer" OR "construction manager" OR '
        '"project manager" OR "site engineer" OR "resident engineer"'
    )
    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "what":             keywords,
        "max_days_old":     7,
        "results_per_page": 30,
    }

    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            log.warning(f"Adzuna {country_code}: HTTP {r.status_code} — {r.text[:120]}")
            return []
        data = r.json()
    except Exception as e:
        log.warning(f"Adzuna {country_code} request failed: {e}")
        return []

    results = []
    for item in data.get("results", []):
        results.append({
            "source":  "Adzuna",
            "url":     item.get("redirect_url", ""),
            "title":   (item.get("title") or "")[:200],
            "company": (item.get("company") or {}).get("display_name", "")[:120],
            "country": country_name,
            "snippet": (item.get("description") or "")[:400],
            "posted":  (item.get("created") or "")[:10],
        })

    log.info(f"Adzuna {country_code}: {len(results)} jobs")
    return results


def search_adzuna_for_region(region: dict) -> list[dict]:
    """
    Search Adzuna for all Adzuna-supported countries in this region.
    Returns combined results. Empty list if region has no Adzuna countries.
    """
    region_countries = [c.strip() for c in region["countries"].split(",")]
    results = []
    for country in region_countries:
        if country in ADZUNA_COUNTRIES:
            results.extend(search_adzuna_for_country(country, ADZUNA_COUNTRIES[country]))
            time.sleep(ADZUNA_DELAY)
    return results
# ─── HERE END: ADZUNA SEARCH ──────────────────────────────────────────────────


# ─── HERE START: JSON PARSING & NORMALIZATION ────────────────────────────────
def parse_jobs_json(raw: str, region_name: str) -> list[dict]:
    """Parse Gemini JSON response using json-repair for robustness."""
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        log.error(f"[{region_name}] No JSON object found")
        return []
    try:
        data = json.loads(repair_json(text[start:end + 1]))
    except Exception as e:
        log.error(f"[{region_name}] JSON repair/parse failed: {e}")
        return []
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _to_int(val, default: int = 0) -> int:
    try: return int(val)
    except (TypeError, ValueError): return default

def _to_str(val, default: str = "") -> str:
    if val is None or isinstance(val, (dict, list)):
        return default
    return str(val)


def normalize_job(job: dict) -> dict:
    """Coerce all job fields to expected types — bulletproof against Gemini quirks."""
    return {
        "country":          _to_str(job.get("country"), ""),
        "title":            _to_str(job.get("title"), "Unknown Role"),
        "company":          _to_str(job.get("company"), "Unknown Company"),
        "suitability":      _to_int(job.get("suitability"), 0),
        "gap":              _to_str(job.get("gap"), "None"),
        "expat_friendly":   _to_str(job.get("expat_friendly"), "Unknown"),
        "visa_sponsorship": _to_str(job.get("visa_sponsorship"), "Unknown"),
        "posted":           _to_str(job.get("posted"), ""),
        "link":             _to_str(job.get("link"), ""),
        "priority":         _to_str(job.get("priority"), "SECONDARY"),
    }
# ─── HERE END: JSON PARSING & NORMALIZATION ──────────────────────────────────


# ─── HERE START: GEMINI ANALYSIS ──────────────────────────────────────────────
def analyze_with_gemini(client: genai.Client, region: dict, raw_jobs: list[dict]) -> list[dict]:
    """
    Send pre-fetched real job listings to Gemini for filtering and scoring.
    Gemini does NOT search — it only analyzes the provided listings.
    Crucially: Gemini is instructed to KEEP URLs exactly as given.
    """
    region_name = region["name"]
    if not raw_jobs:
        return []

    # Trim job list to fit token budget
    capped = raw_jobs[:MAX_JOBS_TO_AI]
    log.info(f"[{region_name}] Sending {len(capped)}/{len(raw_jobs)} jobs to Gemini")

    compact = [
        {
            "url":     j.get("url", ""),
            "title":   j.get("title", ""),
            "company": j.get("company", ""),
            "country": j.get("country", ""),
            "snippet": j.get("snippet", "")[:250],
            "posted":  j.get("posted", ""),
            "source":  j.get("source", ""),
        }
        for j in capped
    ]

    prompt = f"""
You are filtering job listings for a Lebanese Civil/Structural Engineer seeking international roles.

CANDIDATE PROFILE:
- Civil/Structural Engineer with reinforced concrete buildings experience
- Software: ETABS, SAP2000, AutoCAD
- Codes: ACI 318, ASCE 7, UBC 97
- Available for full international relocation

TARGET ROLES:

HIGH PRIORITY:
- Structural Engineer / Senior Structural Engineer
- Construction Manager
- Civil Project Engineer
- Project Manager (construction/buildings)
- Resident Engineer

SECONDARY:
- Site Engineer / Planning Engineer / Technical Office Engineer
- Design Engineer (structural/civil)
- UN/NGO/International Organization Engineer

EXCLUDE:
- Internships, draftsman, MEP, electrical, mechanical, IT/software roles
- Roles requiring local license as hard barrier
- Local-candidates-only roles

JOB LISTINGS TO ANALYZE (these are REAL search results — URLs are valid):
{json.dumps(compact, indent=1)}

INSTRUCTIONS:
1. For each job, decide if it matches target roles (use title + snippet)
2. Score suitability 0-100 based on title/role match to candidate profile
3. KEEP THE INPUT URL EXACTLY — do not modify, shorten, or fabricate
4. Use country from input "country" field, or extract from snippet/title if missing
5. Filter out jobs scoring below 55%
6. Filter out internships and excluded role types
7. Mark expat_friendly: Yes/Possibly/No/Unknown based on company type and snippet
8. Mark visa_sponsorship: Likely/Possibly/Unknown
9. Set priority: HIGH or SECONDARY based on title match
10. Limit to top 15 jobs per response

OUTPUT: Return ONLY valid JSON. No markdown. No code fences. No prose.

{{
  "jobs": [
    {{
      "country": "UAE",
      "title": "Senior Structural Engineer",
      "company": "AECOM",
      "suitability": 85,
      "gap": "Bridge experience preferred",
      "expat_friendly": "Yes",
      "visa_sponsorship": "Likely",
      "posted": "Recent",
      "link": "<URL EXACTLY AS PROVIDED IN INPUT>",
      "priority": "HIGH"
    }}
  ]
}}

If no relevant jobs: {{"jobs": []}}
"""

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=4096,
            ),
        )
    except Exception as e:
        log.error(f"[{region_name}] Gemini error: {e}")
        return []

    raw = (response.text or "").strip()
    log.info(f"[{region_name}] Gemini response preview: {raw[:200]}")

    return parse_jobs_json(raw, region_name)
# ─── HERE END: GEMINI ANALYSIS ────────────────────────────────────────────────


# ─── HERE START: SEARCH REGION (orchestrator) ────────────────────────────────
def search_region(client: genai.Client, region: dict) -> list[dict]:
    """
    Top-level region search.
    Flow:
    1. Search DDG for real LinkedIn URLs (all regions)
    2. Search Adzuna for supported countries (mostly Europe)
    3. Combine raw results
    4. Send to Gemini for relevance filtering + scoring
    5. Normalize types
    6. Return final job list
    """
    region_name = region["name"]
    log.info(f"[{region_name}] Starting search...")

    # ── Step 1+2: Real search APIs in sequence ──
    ddg_jobs    = search_ddg_for_region(region)
    adzuna_jobs = search_adzuna_for_region(region)
    raw_jobs    = ddg_jobs + adzuna_jobs

    log.info(f"[{region_name}] Raw: DDG={len(ddg_jobs)} + "
             f"Adzuna={len(adzuna_jobs)} = {len(raw_jobs)} total")

    if not raw_jobs:
        log.warning(f"[{region_name}] No raw jobs found from any source")
        return []

    # ── Step 3: Gemini filters + scores ──
    analyzed = analyze_with_gemini(client, region, raw_jobs)

    # ── Step 4: Normalize ──
    final = [normalize_job(j) for j in analyzed if isinstance(j, dict)]

    log.info(f"[{region_name}] Final after Gemini filter: {len(final)} jobs")
    return final
# ─── HERE END: SEARCH REGION ──────────────────────────────────────────────────


# ─── HERE START: TELEGRAM FORMATTING ─────────────────────────────────────────
def priority_emoji(p: str) -> str:
    return {"HIGH": "🟢", "SECONDARY": "🟡"}.get(p.upper(), "⚪")

def country_flag(c: str) -> str:
    return COUNTRY_FLAGS.get(c, "🏳️")

def format_single_job(job: dict) -> str:
    link = job.get("link", "")
    pri  = job.get("priority", "SECONDARY")
    lines = [
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
        f"📋 <b>Job Report — {run_time}</b>",
        f"{'━'*30}",
        f"📌 Total jobs: <b>{total}</b>",
        f"🟢 HIGH priority: <b>{high_count}</b>",
        f"🏆 Best region: {best_label}",
        f"⚠️ Common gap: {common_gap}",
        f"⏰ Beirut time: {run_time}",
        f"{'━'*30}",
        f"<i>Summary End.</i>",
        f"<i>To stop reports → send: <b>stop</b></i>",
        f"<i>To reactivate reports → send: <b>activate</b></i>",
    ])
# ─── HERE END: SUMMARY ────────────────────────────────────────────────────────


# ─── HERE START: MAIN ─────────────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("Job Search Bot — Starting (DDG + Adzuna + Gemini)")
    log.info("=" * 50)

    if not is_active():
        log.info("Reports stopped by user. Exiting.")
        return

    now_beirut    = datetime.now(BEIRUT_TZ).strftime("%Y-%m-%d %H:%M")
    client        = genai.Client(api_key=GEMINI_API_KEY)
    adzuna_status = "✅ enabled" if (ADZUNA_APP_ID and ADZUNA_APP_KEY) else "⚠️ disabled (no keys)"

    send_telegram(
        f"🔍 <b>Job Search Started</b>\n"
        f"📅 {now_beirut} (Beirut)\n"
        f"🌐 Sources: DDG/LinkedIn + Adzuna ({adzuna_status})\n"
        f"⏳ This will take ~3-5 minutes..."
    )

    all_results: list[tuple[dict, list[dict]]] = []

    for region in REGIONS:
        try:
            jobs = search_region(client, region)
            all_results.append((region, jobs))
            send_telegram(format_region_block(region, jobs))
            time.sleep(4)
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
