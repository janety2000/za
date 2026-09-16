#!/usr/bin/env python3
import os
import re
import csv
import time
import base64
import hashlib
import logging
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════
BASE_URL     = "https://www.myjobmag.co.za"
START_PAGE   = 6400      # scrape starts here on a *fresh* run (no saved state)
END_PAGE     = 20         # and goes down to (and including) here, eventually

# How many list-pages to walk in a SINGLE invocation of this script.
# 10,750 pages cannot be scraped in one 60-minute GitHub Actions run, so each
# run only chews through a bounded chunk and saves where it stopped. The next
# scheduled run picks up right after that — see load_current_page/save_current_page.
PAGES_PER_RUN = int(os.environ.get("PAGES_PER_RUN", "15"))

# Wall-clock safety valve. PAGES_PER_RUN caps things in the *normal* case,
# but a slow page (big company logo, slow response, many subjobs) can still
# make one run overrun. Rather than let GitHub's timeout-minutes cancel the
# process (which risks the commit step being skipped, and does not save
# progress from a page that was only half-processed when killed), the
# script itself checks elapsed time and stops cleanly — finishing the
# current page, saving state, and exiting normally — well before the
# workflow's outer timeout. Keep this comfortably below timeout-minutes.
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "18") ) * 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36"
    ),
    "Accept-Charset": "utf-8",
    "Accept": "text/html,application/xhtml+xml",
}
REQUEST_TIMEOUT = 20
PROCESSED_IDS_FILE = "processed.csv"
PAGE_STATE_FILE = "current_page.txt"

# ── WordPress ─────────────────────────────────────────────────────────────────
WP_URL      = os.environ.get("WP_BASE_URL", "")
WP_USER     = os.environ.get("WP_USERNAME", "")
WP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

WP_BASE        = WP_URL.rstrip("/")
WP_JOBS_URL    = f"{WP_BASE}/job-listings"
WP_COMPANY_URL = f"{WP_BASE}/companies"
WP_MEDIA_URL   = f"{WP_BASE}/media"

JOB_TYPE_MAPPING = {
    "full-time": "full-time", "full time": "full-time", "fulltime": "full-time",
    "part-time": "part-time", "part time": "part-time", "parttime": "part-time",
    "contract": "contract", "contractor": "contract", "contracting": "contract",
    "temporary": "temporary", "temp": "temporary",
    "freelance": "freelance",
    "internship": "internship", "intern": "internship",
    "volunteer": "volunteer",
}

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
_fh = logging.FileHandler("debug.log")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_ch)


def require_wp_config():
    missing = [n for n, v in
               [("WP_BASE_URL", WP_URL), ("WP_USERNAME", WP_USER), ("WP_APP_PASSWORD", WP_PASSWORD)]
               if not v]
    if missing:
        raise EnvironmentError(
            f"\n❌ Missing required environment variable(s): {', '.join(missing)}\n"
            f"   Fix: GitHub → Settings → Secrets and variables → Actions → New secret\n"
        )


# ════════════════════════════════════════════════════════════════════════════
# SANITIZATION
# ════════════════════════════════════════════════════════════════════════════
_MOJIBAKE = [
    ("Â", ""), ("â€™", "'"), ("â€œ", '"'), ("â€\x9d", '"'), ("â€", '"'),
    ("â€¢", "•"), ("Ã©", "é"), ("Ã ", "à"), ("Ã¨", "è"), ("Ã¯", "ï"),
    ("\u00a0", " "), ("\u200b", ""), ("\ufeff", ""),
]


def sanitize(value: str) -> str:
    if not isinstance(value, str):
        return value
    text = value
    for pattern, repl in _MOJIBAKE:
        text = text.replace(pattern, repl)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = re.sub(r"^N/A$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bN/A\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalise_job_type(raw: str) -> str:
    return JOB_TYPE_MAPPING.get((raw or "").lower().strip(), "full-time")


def add_three_months(posted_date: datetime) -> str:
    month = posted_date.month - 1 + 3
    year = posted_date.year + month // 12
    month = month % 12 + 1
    day = min(posted_date.day, 28)
    return datetime(year, month, day).strftime("%Y-%m-%d")


def make_job_id(job_url: str) -> str:
    return hashlib.md5(job_url.encode()).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════════════════
# DEDUP TRACKER
# ════════════════════════════════════════════════════════════════════════════
def _init_tracker():
    if not os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Job ID", "Job URL", "Job Title", "Status", "Timestamp"])


def load_processed_ids() -> set:
    _init_tracker()
    with open(PROCESSED_IDS_FILE, newline="", encoding="utf-8") as f:
        return {row["Job ID"] for row in csv.DictReader(f)}


def mark_processed(job_id: str, job_url: str, title: str, status: str):
    _init_tracker()
    with open(PROCESSED_IDS_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([job_id, job_url, title, status, datetime.now().isoformat()])


# ════════════════════════════════════════════════════════════════════════════
# PAGE PROGRESS TRACKER
# ════════════════════════════════════════════════════════════════════════════
def load_current_page() -> int:
    """
    Returns the page number this run should START at. Reads whatever was
    saved by the previous run; falls back to START_PAGE if this is the very
    first run (or the state file is missing/corrupt).
    """
    if os.path.exists(PAGE_STATE_FILE):
        try:
            with open(PAGE_STATE_FILE, encoding="utf-8") as f:
                saved = int(f.read().strip())
            if saved < END_PAGE:
                # Previous run already reached the bottom of the range.
                # Wrap back around to the top so the scraper keeps picking
                # up newly-posted jobs on future scheduled runs instead of
                # sitting idle forever.
                logger.info(f"📄 Saved page {saved} is below END_PAGE={END_PAGE} — "
                            f"full range complete, wrapping back to START_PAGE={START_PAGE}.")
                return START_PAGE
            return saved
        except (ValueError, OSError) as e:
            logger.warning(f"Could not read {PAGE_STATE_FILE} ({e}) — using START_PAGE.")
    return START_PAGE


def save_current_page(next_page: int):
    with open(PAGE_STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(next_page))


# ════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ════════════════════════════════════════════════════════════════════════════
def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def text_of(soup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el else ""


# ════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ════════════════════════════════════════════════════════════════════════════
def scrape_job_list_page(page_num: int) -> list:
    url = f"{BASE_URL}/page/{page_num}"
    logger.info(f"Fetching page {page_num}: {url}")
    try:
        soup = get_soup(url)
    except Exception as e:
        logger.error(f"Error fetching page {page_num}: {e}")
        return []

    urls = []
    for a in soup.select("li.mag-b > h2 > a"):
        href = a.get("href")
        if href:
            urls.append(BASE_URL + href if href.startswith("/") else href)
    logger.info(f"Found {len(urls)} job URLs on page {page_num}")
    return urls


def scrape_company_details(company_url: str) -> dict:
    company = {
        "name": "", "logo": "", "industry": "", "founded": "",
        "type": "", "website": "", "address": "", "details": "",
    }
    if not company_url:
        return company
    try:
        soup = get_soup(company_url)
        company["name"] = text_of(soup, "#wrap-comp-jobs > div.company-jobs > h1").replace("Recruitment", "").strip()
        logo_el = soup.select_one("#wrap-comp-jobs > div.company-jobs > div.company-logo > img")
        if logo_el and logo_el.get("src"):
            src = logo_el["src"]
            company["logo"] = BASE_URL + src if src.startswith("/") else src
        company["industry"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(1) > span.comp-info-desc")
        company["founded"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(2) > span.comp-info-desc")
        company["type"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(3) > span.comp-info-desc")
        company["website"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(4) > span.comp-info-desc")
        company["address"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(5) > span.comp-info-desc")
        company["details"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.mag-b.fl-r.ts-13.tc-b6.bm-b-35")
        logger.info(f"Company scraped: {company['name']}")
    except Exception as e:
        logger.error(f"Company fetch failed for {company_url}: {e}")
    return company


def _extract_date_pair(soup) -> tuple:
    """
    Returns (posted_str, deadline_str) with the 'Posted:'/'Deadline:' <b> labels
    stripped out first. Doing .get_text(strip=True) directly on the container
    (as the old code did) concatenates "Posted:" + "May 15, 2013" into
    "Posted:May 15, 2013" with no space, which fails every strptime() call —
    this was why EVERY job, old and new, was being skipped. Removing the <b>
    label before reading the text fixes it for both old and new page layouts,
    since both use the same div.read-date-sec-li markup.
    """
    posted_str, deadline_str = "", ""
    date_lis = soup.select("div.read-date-sec div.read-date-sec-li")
    if len(date_lis) >= 1:
        li = date_lis[0]
        b = li.find("b")
        if b:
            b.extract()
        posted_str = li.get_text(strip=True)
    if len(date_lis) >= 2:
        li = date_lis[1]
        b = li.find("b")
        if b:
            b.extract()
        deadline_str = li.get_text(strip=True)
    return posted_str, deadline_str


def _extract_company_url(soup) -> str:
    # New-style pages: "View Jobs at X" link next to the job title.
    a = soup.select_one("li.job-industry a[href^='/jobs-at/']")
    if a and a.get("href"):
        href = a["href"]
        return BASE_URL + href if href.startswith("/") else href
    # Old-style pages: a bare anchor as a direct child of #printable.
    for a in soup.select("#printable > a"):
        href = a.get("href")
        if href:
            return BASE_URL + href if href.startswith("/") else href
    return ""


def _extract_application(soup) -> str:
    """Email address if one is present in the Method of Application block,
    otherwise the apply link's href (works for both old mailto-style blocks
    and new /apply-now/ or external form links)."""
    app_block = soup.select_one("#printable > div.mag-b.bm-b-30")
    if not app_block:
        h2 = soup.select_one("#application-method")
        app_block = h2.find_next_sibling("div") if h2 else None

    if not app_block:
        return ""

    text = app_block.get_text(" ", strip=True)
    email_match = re.search(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}", text)
    if email_match:
        return email_match.group(0)

    link = app_block.select_one("a")
    if link and link.get("href"):
        href = link["href"]
        return BASE_URL + href if href.startswith("/") else href

    return ""


def scrape_job_details(job_url: str) -> list:
    """
    Returns a LIST of job dicts. Most pages have exactly one job; older
    /readjob/.../ pages bundle several positions under one shared company +
    application block (e.g. "Accountant", "Customer Relations Officer",
    "Marketers" all under one "Jobs in a Dry Cleaning..." page) — each of
    those becomes its own job dict here, instead of only the first one
    being captured.
    """
    soup = get_soup(job_url)

    # Every job heading is a direct child <h2 class="mag-b" id="job...">
    # of #printable, on both old multi-job pages and new single-job pages.
    job_headings = soup.select("#printable > h2.mag-b")
    if not job_headings:
        h2 = soup.select_one("h2.mag-b")
        if h2:
            job_headings = [h2]

    subjob_blocks = []
    for h2 in job_headings:
        link_el = h2.select_one("a")
        if link_el and link_el.get("href"):
            title = link_el.get_text(strip=True)
            href = link_el["href"]
            subjob_url = BASE_URL + href if href.startswith("/") else href
        else:
            span_el = h2.select_one("span.subjob-title")
            title = span_el.get_text(strip=True) if span_el else h2.get_text(strip=True)
            subjob_url = job_url
        title = title.replace("Method of Application", "").strip()
        if not title:
            continue

        key_info_ul = h2.find_next_sibling("ul", class_="job-key-info")
        details_div = h2.find_next_sibling("div", class_="job-details")

        def kv(label):
            if not key_info_ul:
                return ""
            for li in key_info_ul.select("li"):
                t = li.select_one("span.jkey-title")
                if t and t.get_text(strip=True) == label:
                    info = li.select_one("span.jkey-info")
                    return info.get_text(strip=True) if info else ""
            return ""

        subjob_blocks.append({
            "title": title,
            "url": subjob_url,
            "job_type": kv("Job Type"),
            "qualifications": kv("Qualification"),
            "experience": kv("Experience"),
            "location": kv("Location"),
            "field": kv("Job Field"),
            "salary": kv("Salary Range"),
            "description": details_div.get_text("\n", strip=True) if details_div else "",
        })

    if not subjob_blocks:
        logger.warning(f"No job blocks found on page — skipping: {job_url}")
        return []

    date_posted_str, deadline_raw = _extract_date_pair(soup)
    date_posted = None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            date_posted = datetime.strptime(date_posted_str, fmt)
            break
        except ValueError:
            continue
    if date_posted is None:
        logger.warning(f"Invalid/unparseable date '{date_posted_str}' — skipping page: {job_url}")
        return []

    estimated_deadline = add_three_months(date_posted)
    deadline = deadline_raw.strip()
    if not deadline or deadline.lower() == "not specified":
        deadline = estimated_deadline

    application = _extract_application(soup)
    company_url = _extract_company_url(soup)
    company = scrape_company_details(company_url)
    if not company["details"] and company["name"]:
        company["details"] = search_company_details_fallback(company["name"])
    if not application:
        application = company["website"]

    jobs = []
    for blk in subjob_blocks:
        jobs.append({
            "job_title": sanitize(blk["title"]),
            "job_type": sanitize(blk["job_type"]),
            "job_qualifications": sanitize(blk["qualifications"]),
            "job_experience": sanitize(blk["experience"]),
            "job_location": sanitize(blk["location"]),
            "job_field": sanitize(blk["field"]),
            "date_posted": sanitize(date_posted_str),
            "deadline": sanitize(deadline),
            "job_description": sanitize(blk["description"]),
            "application": sanitize(application),
            "company_url": sanitize(company_url),
            "company_name": sanitize(company["name"]),
            "company_logo": sanitize(company["logo"]),
            "company_industry": sanitize(company["industry"]),
            "company_founded": sanitize(company["founded"]),
            "company_type": sanitize(company["type"]),
            "company_website": sanitize(company["website"]),
            "company_address": sanitize(company["address"]),
            "company_details": sanitize(company["details"]),
            "job_url": sanitize(blk["url"]),
            "estimated_deadline": sanitize(estimated_deadline),
            "salary_range": sanitize(blk["salary"]),
        })
    return jobs


def search_company_details_fallback(company_name: str) -> str:
    try:
        url = "https://www.google.com/search?q=" + requests.utils.quote(company_name + " company about")
        soup = get_soup(url)
        snippet = (soup.select_one("div.BNeawe") or
                   soup.select_one("span.aCOpRe") or
                   soup.select_one("div.VwiC3b"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
    except Exception as e:
        logger.error(f"Google fallback failed for {company_name}: {e}")
    try:
        slug = re.sub(r"[^a-z0-9-]", "-", company_name.lower())
        url = f"https://www.linkedin.com/company/{slug}"
        soup = get_soup(url)
        snippet = (soup.select_one("p.core-section-container__info") or
                   soup.select_one("section.summary p"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
        meta = soup.find("meta", {"name": "description"})
        if meta and meta.get("content") and len(meta["content"]) > 20:
            return meta["content"]
    except Exception as e:
        logger.error(f"LinkedIn fallback failed for {company_name}: {e}")
    return ""


# ════════════════════════════════════════════════════════════════════════════
# WORDPRESS
# ════════════════════════════════════════════════════════════════════════════
def wp_headers() -> dict:
    token = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def upload_logo(logo_url: str):
    if not logo_url or not logo_url.startswith("http"):
        return None
    ext = logo_url.lower().rsplit(".", 1)[-1]
    if ext not in ("png", "jpg", "jpeg", "webp"):
        return None
    try:
        img = requests.get(logo_url, timeout=15)
        img.raise_for_status()
        headers = wp_headers()
        headers["Content-Disposition"] = f"attachment; filename={logo_url.split('/')[-1]}"
        headers["Content-Type"] = img.headers.get("content-type", "image/jpeg")
        r = requests.post(WP_MEDIA_URL, headers=headers, data=img.content,
                           auth=(WP_USER, WP_PASSWORD), timeout=20)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        logger.error(f"Logo upload error: {e}")
        return None


def get_or_create_term(taxonomy_url: str, name: str):
    if not name or not name.strip():
        return None
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    try:
        r = requests.get(f"{taxonomy_url}?slug={slug}", headers=wp_headers(), timeout=10)
        terms = r.json()
        if isinstance(terms, list) and terms:
            return terms[0]["id"]
    except Exception:
        pass
    try:
        r = requests.post(taxonomy_url, json={"name": name, "slug": slug},
                           headers=wp_headers(), auth=(WP_USER, WP_PASSWORD), timeout=10)
        return r.json().get("id")
    except Exception as e:
        logger.error(f"Term create error '{name}': {e}")
        return None


def save_company(job: dict):
    name = job["company_name"]
    if not name:
        return None
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    try:
        r = requests.get(f"{WP_COMPANY_URL}?slug={slug}", headers=wp_headers(), timeout=10)
        posts = r.json()
        if isinstance(posts, list) and posts:
            logger.info(f"⏭ Company exists: {name}")
            return posts[0]["id"]
    except Exception:
        pass

    attachment_id = upload_logo(job["company_logo"])
    payload = {
        "title": name,
        "content": job["company_details"],
        "status": "publish",
        "featured_media": attachment_id or 0,
        "meta": {
            "_company_name": name,
            "_company_logo": str(attachment_id) if attachment_id else "",
            "_company_industry": job["company_industry"],
            "_company_website": job["company_website"],
        },
    }
    try:
        r = requests.post(WP_COMPANY_URL, json=payload, headers=wp_headers(),
                           auth=(WP_USER, WP_PASSWORD), timeout=20)
        r.raise_for_status()
        post = r.json()
        logger.info(f"✅ Company posted: {name} → ID {post.get('id')}")
        return post.get("id")
    except Exception as e:
        logger.error(f"Company post error '{name}': {e}")
        return None


def save_job(job: dict):
    for jt_label in ["Full Time", "Part Time", "Contract", "Temporary", "Freelance", "Internship", "Volunteer"]:
        get_or_create_term(f"{WP_BASE}/job_listing_type", jt_label)

    title       = job["job_title"]
    description = job["job_description"]
    location    = job["job_location"] or "Nigeria"
    job_type_s  = normalise_job_type(job["job_type"])
    company     = job["company_name"]
    application = job["application"]
    deadline    = job["deadline"] or job["estimated_deadline"]

    is_email = bool(re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", application))
    is_url_v = bool(re.match(r"^https?://[^\s]+$", application))
    if not (is_email or is_url_v):
        application = ""

    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())[:80]
    try:
        r = requests.get(f"{WP_JOBS_URL}?slug={slug}", headers=wp_headers(), timeout=10)
        posts = r.json()
        if isinstance(posts, list) and posts:
            logger.info(f"⏭ Job already on WP: {title}")
            return posts[0]["id"], posts[0].get("link")
    except Exception:
        pass

    attachment_id     = upload_logo(job["company_logo"])
    region_term_id    = get_or_create_term(f"{WP_BASE}/job_listing_region", location)
    job_type_term_id  = get_or_create_term(f"{WP_BASE}/job_listing_type", job_type_s.replace("-", " ").title())

    payload = {
        "title": title,
        "content": description,
        "status": "publish",
        "featured_media": attachment_id or 0,
        "meta": {
            "_job_title":          title,
            "_job_location":       location,
            "_job_type":           job_type_s,
            "_job_description":    description,
            "_application":        application,
            "_job_expires":        deadline,
            "_company_name":       company,
            "_company_website":    job["company_website"],
            "_company_logo":       str(attachment_id) if attachment_id else "",
            "_company_industry":   job["company_industry"],
            "_company_address":    job["company_address"],
            "_company_founded":    job["company_founded"],
            "_company_type":       job["company_type"],
            "_job_qualifications": job["job_qualifications"],
            "_job_experiences":    job["job_experience"],
            "_job_field":          job["job_field"],
            "_job_source_url":     job["job_url"],
            "_job_salary":         job["salary_range"],
        },
    }
    if region_term_id:
        payload["job_listing_region"] = [region_term_id]
    if job_type_term_id:
        payload["job_listing_type"] = [job_type_term_id]

    for attempt in range(3):
        try:
            r = requests.post(WP_JOBS_URL, json=payload, headers=wp_headers(),
                               auth=(WP_USER, WP_PASSWORD), timeout=25)
            r.raise_for_status()
            post = r.json()
            logger.info(f"✅ Job posted: '{title}' → WP ID {post.get('id')}")
            return post.get("id"), post.get("link")
        except Exception as e:
            logger.error(f"Job post attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, None


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def run():
    require_wp_config()
    processed_ids = load_processed_ids()
    logger.info(f"📋 {len(processed_ids)} jobs already in tracker.")

    start_page = load_current_page()
    stop_page = max(start_page - PAGES_PER_RUN, END_PAGE - 1)
    page_range = range(start_page, stop_page, -1)
    logger.info(f"📄 This run: pages {start_page} → {stop_page + 1} "
                f"({PAGES_PER_RUN} pages max), full range ends at {END_PAGE}.")

    posted = skipped = failed = 0
    last_page_done = start_page
    run_started = time.monotonic()
    stopped_early = False

    for page_num in page_range:
        if time.monotonic() - run_started > MAX_RUN_SECONDS:
            logger.info(f"⏱ Time budget ({MAX_RUN_SECONDS}s) reached — "
                        f"stopping cleanly before page {page_num} instead of "
                        f"risking a hard timeout cancellation.")
            stopped_early = True
            break

        last_page_done = page_num
        job_urls = scrape_job_list_page(page_num)

        for j, job_url in enumerate(job_urls, start=1):
            logger.info(f"── Page {page_num} | Job {j}/{len(job_urls)}: {job_url}")

            # Cheap pre-check: on single-job pages the list URL IS the final
            # job URL used for the tracker ID, so we can skip the network
            # round-trip to fetch+parse the detail page entirely if it's
            # already been processed. (Multi-job /readjob/ pages won't match
            # here since their subjob URLs differ — they still get fetched
            # and are de-duped individually further down, just not for free.)
            if make_job_id(job_url) in processed_ids:
                logger.info("⏭ SKIP (pre-check) — already processed, not re-fetching.")
                skipped += 1
                continue

            try:
                subjobs = scrape_job_details(job_url)
            except Exception as e:
                logger.error(f"Error scraping job {job_url}: {e}")
                failed += 1
                continue

            if not subjobs:
                logger.info("⏭ SKIP — no parsable job blocks on this page.")
                skipped += 1
                continue

            # One page can hold several positions (old /readjob/ layout) —
            # post each one, skipping any already in the tracker.
            for job in subjobs:
                job_id = make_job_id(job["job_url"])

                if job_id in processed_ids:
                    logger.info(f"⏭ SKIP — already processed: {job['job_title']}")
                    skipped += 1
                    continue

                if not job["job_title"] or not job["job_description"]:
                    logger.info("⏭ SKIP — missing title/description.")
                    mark_processed(job_id, job["job_url"], "", "skipped_incomplete")
                    processed_ids.add(job_id)
                    skipped += 1
                    continue

                if job["company_name"]:
                    save_company(job)

                post_id, post_url = save_job(job)
                if post_id:
                    mark_processed(job_id, job["job_url"], job["job_title"],
                                    f"posted|wp_id={post_id}|{post_url or ''}")
                    posted += 1
                    logger.info(f"✅ SUCCESS — '{job['job_title']}' → WP ID={post_id} 🔗 {post_url}")
                else:
                    mark_processed(job_id, job["job_url"], job["job_title"], "wp_post_failed")
                    failed += 1
                    logger.info(f"❌ WordPress post failed: {job['job_title']}")

                processed_ids.add(job_id)

            time.sleep(1)  # be polite to the source site

        # Save progress after EVERY page, not just at the end — if this run
        # itself gets killed (network hiccup, runner issue), the next run
        # resumes from the last fully-completed page instead of from
        # START_PAGE again.
        save_current_page(page_num - 1)

    logger.info(f"\n{'#'*60}")
    logger.info(f" RUN COMPLETE ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
                f"{' — stopped early on time budget' if stopped_early else ''}")
    logger.info(f" 📄 Pages this run : {start_page} → {last_page_done}")
    logger.info(f" ▶️  Resumes next at: {last_page_done - 1}")
    logger.info(f" ✅ Posted  : {posted}")
    logger.info(f" ⏭ Skipped : {skipped}")
    logger.info(f" ❌ Failed  : {failed}")
    logger.info(f"{'#'*60}")


if __name__ == "__main__":
    logger.info("🚀 MyjobMag scraper — resumable chunked run — starting…")
    run()
    logger.info("✅ Done.")
