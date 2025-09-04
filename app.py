# app.py
import re
import time
import random
from urllib.parse import urlparse
from typing import Optional, Tuple, List

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------- Gmail SMTP (no SendGrid) -------------
import smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------- App setup ----------------------
st.set_page_config(page_title="Prospector + SERP/Unlocker", layout="wide")
st.title("Local Prospector — GC / Builders / Architects (Gmail SMTP)")

# ---------------------- Secrets / constants ----------------------
SENDER_NAME = st.secrets.get("SENDER_NAME", "Miami Master Flooring")
SENDER_EMAIL = "info@miamimasterflooring.com"  # fixed sender identity for signature
REPLY_TO = st.secrets.get("REPLY_TO", "info@miamimasterflooring.com")
GMAIL_USER = st.secrets.get("GMAIL_USER", SENDER_EMAIL)  # SMTP login user (can be same)
GMAIL_APP_PASSWORD = st.secrets.get("GMAIL_APP_PASSWORD", "")  # Google App Password

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?:\+1[\s\-.]?)?(?:\(?\d{3}\)?[\s\-.]?)\d{3}[\s\-.]?\d{4}")
SOCIAL_AGG_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "yelp.com", "angieslist.com", "houzz.com", "pinterest.com",
    "tiktok.com", "homeadvisor.com", "thumbtack.com", "porch.com", "bbb.org"
)
EXCLUDE_DOMAINS = (
    "google.com", "maps.google.com", "duckduckgo.com", "bing.com",
    "support.google.com", "developers.google.com", "webcache.googleusercontent.com",
)
GENERIC_PREFIXES = {"info", "contact", "sales", "hello", "admin", "support", "office", "team"}

# Session-state DF
if "leads" not in st.session_state:
    st.session_state.leads = pd.DataFrame(columns=["Company", "Email", "Website", "Phone", "Source"])

# ---------------------- Robust HTTP session ----------------------
@st.cache_resource(show_spinner=False)
def _session_with_retries():
    s = requests.Session()
    r = Retry(
        total=6, connect=3, read=3, status=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=r))
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.headers.update({
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123 Safari/537.36"
        ]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s

HTTP = _session_with_retries()

# ---------------------- Helpers ----------------------
def domain_of(u: str) -> str:
    try:
        d = urlparse(u).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""

def is_generic_email(email: str) -> bool:
    try:
        local, _ = email.split("@", 1)
        return local.lower() in GENERIC_PREFIXES
    except Exception:
        return False

def looks_like_business_site(u: str) -> bool:
    d = domain_of(u)
    if not d:
        return False
    if any(s in d for s in SOCIAL_AGG_DOMAINS):
        return False
    if any(d.endswith(x) or d == x for x in EXCLUDE_DOMAINS):
        return False
    if any(d.endswith(tld) for tld in (".com", ".net", ".org", ".io", ".co", ".us")):
        return True
    return False

@st.cache_data(show_spinner=False)
def verify_email_mx(email: str) -> bool:
    """DNS-over-HTTPS MX check via Google DNS; permissive on failure."""
    try:
        d = email.split("@", 1)[1]
        r = HTTP.get(f"https://dns.google/resolve?name={d}&type=MX", timeout=5)
        if not r.ok:
            return True  # don't over-block on transient errors
        j = r.json()
        return bool(j.get("Answer"))
    except Exception:
        return True

def _first_non_empty(*vals):
    for v in vals:
        if v:
            return v
    return None

# ---------------------- Search providers ----------------------
@st.cache_data(show_spinner=False, ttl=3600)
def search_bing_api(query: str, key: str, count: int = 20) -> List[str]:
    if not key:
        return []
    try:
        endpoint = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": key}
        params = {"q": query, "mkt": "en-US", "count": count}
        r = HTTP.get(endpoint, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        urls = [v["url"] for v in (data.get("webPages") or {}).get("value", []) if v.get("url")]
        return [u for u in urls if looks_like_business_site(u)][:count]
    except Exception:
        return []

@st.cache_data(show_spinner=False, ttl=3600)
def search_serp_api(
    query: str,
    base_url: str,
    key: str,
    method: str = "GET",
    auth_header: Optional[str] = "X-API-KEY",
    key_param: Optional[str] = None,
    count: int = 20,
) -> List[str]:
    if not base_url or not key:
        return []
    try:
        headers = {"User-Agent": HTTP.headers.get("User-Agent")}
        if auth_header:
            headers[auth_header] = key
        params = {"q": query, "count": count}
        if key_param:
            params[key_param] = key

        if method.upper() == "POST":
            r = HTTP.post(base_url, headers=headers, json=params, timeout=25)
        else:
            r = HTTP.get(base_url, headers=headers, params=params, timeout=25)

        r.raise_for_status()
        data = r.json()
        urls: List[str] = []

        if isinstance(data, dict):
            if "webPages" in data and isinstance(data["webPages"], dict) and "value" in data["webPages"]:
                urls = [v.get("url") for v in data["webPages"]["value"] if v.get("url")]
            elif "results" in data and isinstance(data["results"], list):
                for item in data["results"]:
                    u = (item.get("url") or item.get("link"))
                    if u:
                        urls.append(u)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict):
                    u = (item.get("url") or item.get("link"))
                    if u:
                        urls.append(u)

        return [u for u in urls if u and looks_like_business_site(u)][:count]
    except Exception:
        return []

# ---------------------- Unlocker fetch ----------------------
@st.cache_data(show_spinner=False, ttl=3600)
def unlocker_fetch(
    url: str,
    unlocker_base: str,
    key: str,
    key_header: Optional[str] = "X-API-KEY",
    key_param: Optional[str] = None,
) -> Optional[str]:
    try:
        headers = {"User-Agent": HTTP.headers.get("User-Agent")}
        if key_header:
            headers[key_header] = key
        params = {"url": url}
        if key_param:
            params[key_param] = key
        r = HTTP.get(unlocker_base, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        if r.headers.get("Content-Type", "").startswith("application/json"):
            j = r.json()
            return j.get("html")
        return r.text
    except Exception:
        return None

# ---------------------- Extraction ----------------------
def extract_company_info_from_html(html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not html:
        return None, None, None

    soup = BeautifulSoup(html, "html.parser")

    # Email: prefer explicit mailto: links, then page text
    mailto = [
        a.get("href", "").replace("mailto:", "").strip()
        for a in soup.select('a[href^="mailto:"]')
        if a.get("href")
    ]
    mailto = [e.split("?")[0] for e in mailto if EMAIL_RE.match(e)]

    # Phone: microdata or visible text
    phone_nodes = soup.select('[itemprop="telephone"], a[href^="tel:"], .phone, .tel')
    phone_candidates = [n.get_text(" ", strip=True) for n in phone_nodes] if phone_nodes else []
    text = soup.get_text(" ", strip=True)
    phones = phone_candidates + PHONE_RE.findall(text)

    # Company: title | h1 | schema.org Organization name
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    title_main = title.split(" | ")[0].split(" – ")[0].split(" - ")[0].strip()[:120] if title else None
    h1 = soup.find("h1")
    h1_txt = h1.get_text(strip=True)[:120] if h1 and h1.get_text(strip=True) else None
    org = soup.select_one('[itemtype*="schema.org/Organization"] [itemprop="name"]')
    org_txt = org.get_text(strip=True)[:120] if org else None

    company = _first_non_empty(title_main, h1_txt, org_txt)

    # Final picks
    email = _first_non_empty(*(mailto or []))
    if not email:
        emails_in_text = EMAIL_RE.findall(text)
        email = _first_non_empty(*(emails_in_text or []))

    phone = _first_non_empty(*(phones or []))

    return company, email, phone

def extract_company_info(
    url: str,
    unlocker_base: str = "",
    unlocker_key: str = "",
    key_header: Optional[str] = "X-API-KEY",
    key_param: Optional[str] = None,
):
    html = None
    if unlocker_base and unlocker_key:
        html = unlocker_fetch(url, unlocker_base, unlocker_key, key_header=key_header, key_param=key_param)
    if not html:
        try:
            r = HTTP.get(url, timeout=15)
            r.raise_for_status()
            html = r.text
        except Exception:
            return None, None, None
    return extract_company_info_from_html(html)

# ---------------------- Lead insert + filters ----------------------
st.session_state.setdefault("skip_generic", True)
st.session_state.setdefault("verify_mx", True)

def upsert_lead(name, email, website, phone, source):
    if not email:
        return
    if st.session_state.get("skip_generic") and is_generic_email(email):
        return
    if st.session_state.get("verify_mx") and not verify_email_mx(email):
        return

    df = st.session_state.leads
    lowers = set(df["Email"].dropna().str.lower())
    if email.lower() in lowers:
        return

    st.session_state.leads.loc[len(df)] = {
        "Company": (name or "").strip()[:120],
        "Email": email.strip(),
        "Website": website,
        "Phone": (phone or "").strip(),
        "Source": source,
    }

# ---------------------- Gmail sending ----------------------
def send_email_gmail(to_email: str, subject: str, html: str) -> int:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("Set GMAIL_USER and GMAIL_APP_PASSWORD in Streamlit Secrets.")
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SENDER_NAME} <{GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = REPLY_TO
    part = MIMEText(html, "html")
    msg.attach(part)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        failures = server.sendmail(GMAIL_USER, [to_email], msg.as_string())
        if failures:
            raise RuntimeError(f"Failed for: {failures}")
    return 250

# ---------------------- Sidebar ----------------------
with st.sidebar:
    st.subheader("Provider Settings")
    provider = st.selectbox("Search provider", ["Bing API (recommended)", "Generic SERP API"], index=0)

    if provider == "Bing API (recommended)":
        BING_API_KEY = st.secrets.get("BING_API_KEY", "")
        if not BING_API_KEY:
            BING_API_KEY = st.text_input("BING_API_KEY (or add to Secrets)", type="password")
        SERP_BASE_URL = ""
        SERP_KEY = ""
        SERP_METHOD = "GET"
        SERP_AUTH_HEADER = "X-API-KEY"
        SERP_KEY_PARAM = ""
    else:
        SERP_BASE_URL = st.text_input("SERP Base URL (endpoint that returns JSON)", value="")
        SERP_KEY = st.secrets.get("SERP_API_KEY", "")
        if not SERP_KEY:
            SERP_KEY = st.text_input("SERP API Key (or add to Secrets)", type="password")
        SERP_METHOD = st.selectbox("SERP HTTP Method", ["GET", "POST"], index=0)
        SERP_AUTH_HEADER = st.text_input("Auth Header (blank if query param)", value="X-API-KEY")
        SERP_KEY_PARAM = st.text_input("Key Query Param (e.g., api_key)", value="")

    st.markdown("---")
    st.subheader("Unlocker (optional)")
    UNLOCKER_BASE = st.text_input("Unlocker fetch endpoint (optional)", value="")
    UNLOCKER_KEY = st.secrets.get("UNLOCKER_KEY", "")
    if not UNLOCKER_KEY:
        UNLOCKER_KEY = st.text_input("UNLOCKER_KEY (or add to Secrets)", type="password")
    UNLOCKER_AUTH_HEADER = st.text_input("Unlocker Auth Header (blank if query param)", value="X-API-KEY")
    UNLOCKER_KEY_PARAM = st.text_input("Unlocker Key Param (e.g., api_key)", value="")

    st.markdown("---")
    st.subheader("Quality filters")
    st.session_state["skip_generic"] = st.checkbox(
        "Skip generic inboxes (info@, sales@, admin@)", value=st.session_state.get("skip_generic", True)
    )
    st.session_state["verify_mx"] = st.checkbox(
        "Verify email domains via MX lookup", value=st.session_state.get("verify_mx", True)
    )

# ---------------------- Tabs ----------------------
tab_search, tab_results, tab_email, tab_export = st.tabs(["Search", "Results", "Email", "Export/Import"])

# ====================== SEARCH TAB ======================
with tab_search:
    st.subheader("Find GC / Builders / Architects near you")
    presets = {
        "Miami-Dade County": "Miami-Dade County, FL",
        "Broward County": "Broward County, FL",
        "Palm Beach County": "Palm Beach County, FL",
        "Custom": None,
    }
    preset_choice = st.selectbox("Quick area preset", list(presets.keys()), index=0)
    col1, col2 = st.columns(2)
    with col1:
        if preset_choice != "Custom":
            preset_loc = presets[preset_choice]
            st.text_input("City / Area", value=preset_loc, disabled=True, key="loc_display")
            location = preset_loc
        else:
            location = st.text_input("City / Area", value="Miami, FL")

        radius_phrase = st.select_slider(
            "Radius phrase", ["5 miles", "10 miles", "25 miles", "50 miles"], value="25 miles"
        )
    with col2:
        categories = st.multiselect(
            "Categories", ["General Contractors", "Builders", "Architects"],
            default=["General Contractors", "Builders", "Architects"],
        )
        rate_delay = st.slider("Delay between requests (sec)", 0.0, 3.0, 1.0, 0.1)

    max_sites = st.slider("Max sites (total)", 10, 200, 60, 10)

    if st.button("Search & Extract"):
        try:
            queries: List[str] = []
            if "General Contractors" in categories:
                queries.append(f'General Contractors "{location}" {radius_phrase} (site:.com OR site:.net OR site:.org)')
            if "Builders" in categories:
                queries.append(f'Home Builders "{location}" {radius_phrase} (site:.com OR site:.net OR site:.org)')
            if "Architects" in categories:
                queries.append(f'Architecture Firms "{location}" {radius_phrase} (site:.com OR site:.net OR site:.org)')

            per_q = max(10, max_sites // max(len(queries), 1))
            all_urls: List[str] = []
            progress = st.progress(0)

            for i, q in enumerate(queries):
                if provider.startswith("Bing API"):
                    key = st.secrets.get("BING_API_KEY", "") or BING_API_KEY
                    urls = search_bing_api(q, key=key, count=per_q)
                else:
                    urls = search_serp_api(
                        q,
                        base_url=SERP_BASE_URL,
                        key=SERP_KEY,
                        method=SERP_METHOD,
                        auth_header=(SERP_AUTH_HEADER or None),
                        key_param=(SERP_KEY_PARAM or None),
                        count=per_q,
                    )

                all_urls += urls
                progress.progress(int(((i + 1) / max(len(queries), 1)) * 100))
                if rate_delay:
                    time.sleep(rate_delay)

            # Deduplicate by domain and drop excluded
            by_domain = {}
            for u in all_urls:
                d = domain_of(u) or u
                if d not in by_domain and not any(d.endswith(x) or d == x for x in EXCLUDE_DOMAINS):
                    by_domain[d] = u

            urls = list(by_domain.values())[:max_sites]
            st.write(f"Unique candidate sites: **{len(urls)}**")

            added = 0
            for j, base in enumerate(urls, start=1):
                for path in ["", "/contact", "/contact-us", "/contactus", "/about", "/team", "/contacts"]:
                    target = base.rstrip("/") + path
                    name, email, phone = extract_company_info(
                        target,
                        unlocker_base=UNLOCKER_BASE if UNLOCKER_BASE and UNLOCKER_KEY else "",
                        unlocker_key=UNLOCKER_KEY,
                        key_header=(UNLOCKER_AUTH_HEADER or None),
                        key_param=(UNLOCKER_KEY_PARAM or None),
                    )
                    if email:
                        upsert_lead(name, email, base, phone, source=("serp+unlocker" if UNLOCKER_BASE and UNLOCKER_KEY else "serp"))
                        added += 1
                        break
                    if rate_delay:
                        time.sleep(rate_delay)
                progress.progress(int((j / max(len(urls), 1)) * 100))

            st.success(f"Added {added} contacts. Check **Results** tab.")
        except Exception as e:
            st.exception(e)

# ====================== RESULTS TAB ======================
with tab_results:
    st.subheader("Leads")

    # ---------- Manual add (single) ----------
    st.markdown("### Add a lead (single)")
    with st.form("add_single_lead", clear_on_submit=True):
        c1, c2 = st.columns([3, 2])
        with c1:
            company_in = st.text_input("Company", "")
            website_in = st.text_input("Website (https://...)", "")
        with c2:
            email_in = st.text_input("Email", "")
            phone_in = st.text_input("Phone", "")

        source_in = st.text_input("Source (optional)", "manual")
        submitted_single = st.form_submit_button("Add lead")

    if submitted_single:
        email = (email_in or "").strip()
        company = (company_in or "").strip()[:120]
        website = (website_in or "").strip()
        phone = (phone_in or "").strip()

        if not email or not EMAIL_RE.match(email):
            st.error("Please provide a valid email.")
        elif st.session_state.get("skip_generic") and is_generic_email(email):
            st.warning("Skipped: generic inbox (info@ / sales@ / admin@). Uncheck the filter in the sidebar to allow.")
        elif st.session_state.get("verify_mx") and not verify_email_mx(email):
            st.warning("Skipped: email domain appears to have no MX record. Uncheck the MX filter in the sidebar to allow.")
        else:
            lowers = set(st.session_state.leads["Email"].dropna().str.lower())
            if email.lower() in lowers:
                st.info("Duplicate email — this lead already exists.")
            else:
                upsert_lead(company, email, website, phone, source_in or "manual")
                st.success(f"Added: {company or '(no company)'} — {email}")

    st.markdown("---")

    # ---------- Manual add (bulk paste) ----------
    st.markdown("### Bulk paste (CSV/TSV — one lead per line)")
    st.caption("Accepted columns in any order: Company, Email, Website, Phone, Source. First line may be a header.")

    example = "Company,Email,Website,Phone,Source\nAcme Builders,contact@acmebuilders.com,https://acmebuilders.com,(305) 123-4567,manual"
    bulk_text = st.text_area("Paste rows here", value=example, height=150)

    cA, cB = st.columns([1, 3])
    with cA:
        delimiter = st.radio("Delimiter", options=[",", "\t", "|", ";"], index=0)
    with cB:
        do_header = st.checkbox("First row is a header", value=True)

    if st.button("Add pasted leads"):
        raw = (bulk_text or "").strip()
        if not raw:
            st.error("Nothing to import.")
        else:
            rows = [r for r in raw.splitlines() if r.strip()]
            if do_header and rows:
                rows = rows[1:]  # drop header row

            added = 0
            skipped_invalid = 0
            skipped_generic = 0
            skipped_mx = 0
            skipped_dup = 0

            existing = set(st.session_state.leads["Email"].dropna().str.lower())
            delim = "\t" if delimiter == "\t" else delimiter

            header_map = {}
            if do_header and bulk_text:
                hdr_line = bulk_text.splitlines()[0]
                hdr_cols = [h.strip().lower() for h in hdr_line.split(delim)]
                for idx, name in enumerate(hdr_cols):
                    header_map[name] = idx

            def get_col(cols, name, default_idx):
                if header_map and name in header_map and header_map[name] < len(cols):
                    return cols[header_map[name]].strip()
                if default_idx < len(cols):
                    return cols[default_idx].strip()
                return ""

            for line in rows:
                cols = [c.strip() for c in line.split(delim)]
                company = get_col(cols, "company", 0)[:120]
                email = get_col(cols, "email", 1)
                website = get_col(cols, "website", 2)
                phone = get_col(cols, "phone", 3)
                source = get_col(cols, "source", 4) or "manual"

                if not email or not EMAIL_RE.match(email):
                    skipped_invalid += 1
                    continue
                if st.session_state.get("skip_generic") and is_generic_email(email):
                    skipped_generic += 1
                    continue
                if st.session_state.get("verify_mx") and not verify_email_mx(email):
                    skipped_mx += 1
                    continue
                if email.lower() in existing:
                    skipped_dup += 1
                    continue

                upsert_lead(company, email, website, phone, source)
                existing.add(email.lower())
                added += 1

            st.success(f"Imported {added} lead(s).")
            if skipped_invalid:
                st.info(f"Skipped {skipped_invalid} invalid email(s).")
            if skipped_generic:
                st.info(f"Skipped {skipped_generic} generic inbox(es). (See sidebar filter)")
            if skipped_mx:
                st.info(f"Skipped {skipped_mx} email(s) without MX. (See sidebar filter)")
            if skipped_dup:
                st.info(f"Skipped {skipped_dup} duplicate email(s).")

    st.markdown("---")

    # ---------- Editable grid ----------
    st.markdown("### Edit / Append in grid")
    st.caption("Add new rows at the bottom. Click **Save grid changes** to validate & apply filters/dedup.")

    # Ensure consistent column order & types
    base_cols = ["Company", "Email", "Website", "Phone", "Source"]
    df_now = st.session_state.leads.copy()
    for c in base_cols:
        if c not in df_now.columns:
            df_now[c] = ""

    edited = st.data_editor(
        df_now[base_cols],
        num_rows="dynamic",
        use_container_width=True,
        key="leads_editor",
    )

    if st.button("Save grid changes"):
        # Validate and rebuild dataset from editor
        added = 0
        kept = 0
        skipped_invalid = 0
        skipped_generic = 0
        skipped_mx = 0
        deduped = 0

        cleaned_rows = []
        seen = set()

        for _, row in edited.iterrows():
            company = (str(row.get("Company", "") or "")).strip()[:120]
            email = (str(row.get("Email", "") or "")).strip()
            website = (str(row.get("Website", "") or "")).strip()
            phone = (str(row.get("Phone", "") or "")).strip()
            source = (str(row.get("Source", "") or "manual")).strip() or "manual"

            if not email or not EMAIL_RE.match(email):
                skipped_invalid += 1
                continue
            if st.session_state.get("skip_generic") and is_generic_email(email):
                skipped_generic += 1
                continue
            if st.session_state.get("verify_mx") and not verify_email_mx(email):
                skipped_mx += 1
                continue
            if email.lower() in seen:
                deduped += 1
                continue

            cleaned_rows.append(
                {"Company": company, "Email": email, "Website": website, "Phone": phone, "Source": source}
            )
            seen.add(email.lower())

        prev_count = len(st.session_state.leads)
        st.session_state.leads = pd.DataFrame(cleaned_rows, columns=base_cols)
        kept = len(st.session_state.leads)
        added = max(0, kept - min(prev_count, kept))

        st.success(f"Saved grid. Total kept: {kept}.")
        if skipped_invalid:
            st.info(f"Skipped {skipped_invalid} invalid email row(s).")
        if skipped_generic:
            st.info(f"Skipped {skipped_generic} generic inbox row(s). (See sidebar)")
        if skipped_mx:
            st.info(f"Skipped {skipped_mx} MX-missing row(s). (See sidebar)")
        if deduped:
            st.info(f"Removed {deduped} duplicate email row(s) within the grid.")

    st.markdown("---")

    # ---------- Current table ----------
    df = st.session_state.leads.copy()
    if df.empty:
        st.info("No leads yet. Add manually above or run a search in the **Search** tab.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"Total leads: {len(df)}")

        c1, c2 = st.columns(2)
        with c1:
            emails_to_remove = st.text_input("Emails to remove (comma-separated)", key="remove_box")
            if st.button("Remove selected"):
                emails_rm = {e.strip().lower() for e in emails_to_remove.split(",") if e.strip()}
                before = len(st.session_state.leads)
                st.session_state.leads = st.session_state.leads[~st.session_state.leads["Email"].str.lower().isin(emails_rm)]
                after = len(st.session_state.leads)
                st.success(f"Removed {before - after} lead(s).")
        with c2:
            if st.button("Clear ALL leads"):
                st.session_state.leads = st.session_state.leads.iloc[0:0]
                st.warning("All leads cleared.")

# ====================== EMAIL TAB ======================
with tab_email:
    st.subheader("Email campaign (Gmail)")
    st.caption(
        "Sender: info@miamimasterflooring.com via Gmail SMTP. "
        "Set GMAIL_USER and GMAIL_APP_PASSWORD in Secrets. "
        "Use App Passwords (Google Account → Security → 2-Step Verification → App passwords)."
    )
    colA, colB = st.columns(2)
    with colA:
        subject = st.text_input("Subject", "Flooring Installations for Your Upcoming Projects")
        greeting = st.text_input("Greeting", "Dear Team,")
        body = st.text_area(
            "Body (HTML allowed)",
            value=(
                "<p>We specialize in high-quality flooring installations for commercial and residential projects in your area.</p>"
                "<ul><li>Luxury vinyl plank (LVP)</li><li>Waterproof flooring</li>"
                "<li>Custom tile & stone</li><li>10-year craftsmanship warranty</li></ul>"
                "<p>Could we schedule a brief call next week?</p>"
            ),
            height=180,
        )
    with colB:
        signature = st.text_area(
            "Signature (HTML allowed)",
            value=(
                f"<p>Best regards,<br>{SENDER_NAME}<br>{SENDER_EMAIL}<br>(305) 000-0000<br>"
                "<a href='https://www.miamimasterflooring.com' target='_blank'>www.miamimasterflooring.com</a></p>"
                "<p style='font-size:12px;color:#666'>If you prefer not to receive these emails, reply with 'unsubscribe'.</p>"
            ),
            height=180,
        )
        daily_cap = st.number_input("Daily send cap", min_value=10, max_value=500, value=100, step=10)

    emails = st.session_state.leads["Email"].dropna().tolist() if not st.session_state.leads.empty else []
    preview = st.selectbox("Preview recipient", options=(emails[:50] or ["no-data"]))

    def render_html(greeting, body, signature):
        return f"{greeting}<br/>{body}{signature}"

    c1, c2, c3 = st.columns(3)
    if c1.button("Check Gmail login"):
        try:
            if not GMAIL_APP_PASSWORD:
                st.error("GMAIL_APP_PASSWORD not set in Secrets.")
            else:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
                    server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                st.success("Gmail login OK.")
        except Exception as e:
            st.error(f"Gmail auth failed: {e}")

    if c2.button("Send test to preview (Gmail)"):
        if not GMAIL_APP_PASSWORD:
            st.error("GMAIL_APP_PASSWORD not set in Secrets.")
        elif preview and preview != "no-data":
            try:
                code = send_email_gmail(preview, subject, render_html(greeting, body, signature))
                st.success(f"Sent to {preview} (code {code})")
            except Exception as e:
                st.error(f"Send failed: {e}")

    if c3.button("Send campaign now (up to cap) — Gmail"):
        if not GMAIL_APP_PASSWORD:
            st.error("GMAIL_APP_PASSWORD not set in Secrets.")
        else:
            sent = 0
            for e in emails:
                if sent >= daily_cap:
                    break
                if st.session_state.get("skip_generic") and is_generic_email(e):
                    continue
                if st.session_state.get("verify_mx") and not verify_email_mx(e):
                    continue
                try:
                    send_email_gmail(e, subject, render_html(greeting, body, signature))
                    sent += 1
                    time.sleep(0.3)
                except Exception:
                    continue
            st.success(f"Sent {sent} emails via Gmail.")

# ====================== EXPORT / IMPORT TAB ======================
with tab_export:
    st.subheader("Export / Import")
    df = st.session_state.leads.copy()
    colX, colY = st.columns(2)
    with colX:
        if not df.empty:
            st.download_button(
                "Download leads.csv",
                data=df.to_csv(index=False),
                file_name="leads.csv",
                mime="text/csv",
            )
    with colY:
        up = st.file_uploader("Import leads.csv", type=["csv"])
        if up is not None:
            try:
                new = pd.read_csv(up)
                rename = {c: c.strip().title() for c in new.columns}
                new.rename(columns=rename, inplace=True)
                existing = set(st.session_state.leads["Email"].dropna().str.lower())
                imported = 0
                for _, row in new.iterrows():
                    email = str(row.get("Email", "") or "").strip()
                    if not email or not EMAIL_RE.match(email):
                        continue
                    if st.session_state.get("skip_generic") and is_generic_email(email):
                        continue
                    if st.session_state.get("verify_mx") and not verify_email_mx(email):
                        continue
                    if email.lower() in existing:
                        continue
                    st.session_state.leads.loc[len(st.session_state.leads)] = {
                        "Company": str(row.get("Company", "") or "")[:120],
                        "Email": email,
                        "Website": str(row.get("Website", "") or ""),
                        "Phone": str(row.get("Phone", "") or ""),
                        "Source": "import",
                    }
                    existing.add(email.lower())
                    imported += 1
                st.success(f"Imported {imported} leads.")
            except Exception as e:
                st.error(f"Import failed: {e}")
