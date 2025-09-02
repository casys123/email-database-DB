import re
import time
import json
import random
from urllib.parse import urlparse
from typing import Optional, Tuple, List

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Optional email dependency (won't crash if missing) ---
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    SENDGRID_AVAILABLE = True
except Exception:
    SENDGRID_AVAILABLE = False

# ---------------------- App setup ----------------------
st.set_page_config(page_title="Prospector + SERP/Unlocker", layout="wide")
st.title("Local Prospector — GC / Builders / Architects (API‑ready)")

# Sender identity (fixed as requested)
SENDER_NAME = st.secrets.get("SENDER_NAME", "Miami Master Flooring")
SENDER_EMAIL = "info@miamimasterflooring.com"
REPLY_TO = st.secrets.get("REPLY_TO", "info@miamimasterflooring.com")
SENDGRID_API_KEY = st.secrets.get("SENDGRID_API_KEY", "")

EMAIL_RE  = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE  = re.compile(r"(?:\+1[\s\-.]?)?(?:\(?\d{3}\)?[\s\-.]?)\d{3}[\s\-.]?\d{4}")
SOCIAL_DOMAINS = (
    "facebook.com","instagram.com","linkedin.com","twitter.com","x.com",
    "youtube.com","yelp.com","angieslist.com","houzz.com","pinterest.com","tiktok.com"
)
EXCLUDE_DOMAINS = ("google.com", "maps.google.com", "duckduckgo.com", "bing.com")

if "leads" not in st.session_state:
    st.session_state.leads = pd.DataFrame(columns=["Company","Email","Website","Phone","Source"]) 

# ---------------------- Robust HTTP session ----------------------
@st.cache_resource(show_spinner=False)
def _session_with_retries():
    s = requests.Session()
    r = Retry(
        total=6, connect=3, read=3, status=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET","POST"]),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=r))
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.headers.update({
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
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
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

# Email helpers
GENERIC_PREFIXES = {"info", "contact", "sales", "hello", "admin", "support", "office", "team"}

def is_generic_email(email: str) -> bool:
    try:
        local, _ = email.split("@", 1)
        return local.lower() in GENERIC_PREFIXES
    except Exception:
        return False

# Optional MX check (requires dnspython)
@st.cache_data(show_spinner=False)
def verify_email_mx(email: str) -> bool:
    """Return True if domain has MX records. If dnspython missing or lookup fails, be permissive (True)."""
    try:
        import dns.resolver  # type: ignore
        domain = email.split("@", 1)[1]
        answers = dns.resolver.resolve(domain, 'MX', lifetime=3.0)
        return len(answers) > 0
    except Exception:
        # If we can't verify, don't block
        return True


def looks_like_business_site(u: str) -> bool:
    d = domain_of(u)
    if not d:
        return False
    if any(s in d for s in SOCIAL_DOMAINS):
        return False
    if any(d.endswith(tld) for tld in (".com", ".net", ".org", ".io", ".co")) and not any(d.endswith(x) or d == x for x in EXCLUDE_DOMAINS):
        return True
    return False

# ---------------------- Providers ----------------------
# 1) Bing Web Search API (official)
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

# 2) Generic SERP API (any JSON shape w/ urls)
@st.cache_data(show_spinner=False, ttl=3600)
def search_serp_api(query: str, base_url: str, key: str, method: str = "GET",
                    auth_header: Optional[str] = "X-API-KEY", key_param: Optional[str] = None,
                    count: int = 20) -> List[str]:
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
                    u = item.get("url") or item.get("link")
                    if u:
                        urls.append(u)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("link")
                    if u:
                        urls.append(u)
        urls = [u for u in urls if u and looks_like_business_site(u)]
        return urls[:count]
    except Exception:
        return []

# 3) Unlocker for page fetch
@st.cache_data(show_spinner=False, ttl=3600)
def unlocker_fetch(url: str, unlocker_base: str, key: str,
                   key_header: Optional[str] = "X-API-KEY", key_param: Optional[str] = None) -> Optional[str]:
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
    text = soup.get_text(" ", strip=True)
    emails = EMAIL_RE.findall(text)
    phones = PHONE_RE.findall(text)
    company = None
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    if title:
        company = title.split(" | ")[0].split(" – ")[0].split(" - ")[0].strip()[:120]
    if not company:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            company = h1.get_text(strip=True)[:120]
    return company, (emails[0] if emails else None), (phones[0] if phones else None)


def extract_company_info(url: str, unlocker_base: str = "", unlocker_key: str = "",
                         key_header: Optional[str] = "X-API-KEY", key_param: Optional[str] = None):
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

# ---------------------- Lead insert with filters ----------------------
# Quality / verification toggles live in session state for global access
st.session_state.setdefault('skip_generic', False)
st.session_state.setdefault('verify_mx', False)

def upsert_lead(name, email, website, phone, source):
    if not email:
        return
    # Apply user-selected filters
    if st.session_state.get('skip_generic') and is_generic_email(email):
        return
    if st.session_state.get('verify_mx') and not verify_email_mx(email):
        return
    df = st.session_state.leads
    lowers = set(df["Email"].dropna().str.lower())
    if email.lower() in lowers:
        return
    st.session_state.leads.loc[len(df)] = {
        "Company": name or "", "Email": email.strip(), "Website": website,
        "Phone": phone or "", "Source": source
    }

# ---------------------- Email sending ----------------------
def send_email_sendgrid(to_email: str, subject: str, html: str) -> int:
    if not SENDGRID_AVAILABLE:
        raise RuntimeError("sendgrid package not installed. Remove email sending or install sendgrid.")
    if not SENDGRID_API_KEY:
        raise RuntimeError("SENDGRID_API_KEY not set in Secrets.")
    msg = Mail(
        from_email=(SENDER_EMAIL, SENDER_NAME),
        to_emails=[to_email],
        subject=subject,
        html_content=html,
    )
    msg.reply_to = REPLY_TO
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    resp = sg.send(msg)
    return resp.status_code

# ---------------------- UI ----------------------
with st.sidebar:
    st.subheader("Provider Settings")
    provider = st.selectbox(
        "Search provider",
        ["Bing API (recommended)", "Generic SERP API"],
        index=0
    )
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
        SERP_BASE_URL = st.text_input("SERP Base URL (endpoint that returns JSON)")
        SERP_KEY = st.secrets.get("SERP_API_KEY", "")
        if not SERP_KEY:
            SERP_KEY = st.text_input("SERP API Key (or add to Secrets)", type="password")
        SERP_METHOD = st.selectbox("SERP HTTP Method", ["GET","POST"], index=0)
        SERP_AUTH_HEADER = st.text_input("Auth Header (blank if using query param)", value="X-API-KEY")
        SERP_KEY_PARAM = st.text_input("Key Query Param (e.g., api_key)", value="")

    st.markdown("---")
    st.subheader("Unlocker (optional)")
    UNLOCKER_BASE = st.text_input("Unlocker fetch endpoint (optional)")
    UNLOCKER_KEY  = st.secrets.get("UNLOCKER_KEY", "")
    if not UNLOCKER_KEY:
        UNLOCKER_KEY = st.text_input("UNLOCKER_KEY (or add to Secrets)", type="password")
    UNLOCKER_AUTH_HEADER = st.text_input("Unlocker Auth Header (blank if query param)", value="X-API-KEY")
    UNLOCKER_KEY_PARAM   = st.text_input("Unlocker Key Param (e.g., api_key)", value="")

    st.markdown("---")
    st.subheader("Quality filters")
    st.session_state['skip_generic'] = st.checkbox("Skip generic inboxes (info@, sales@, admin@)", value=st.session_state.get('skip_generic', False))
    st.session_state['verify_mx'] = st.checkbox("Verify email domains via MX lookup (requires dnspython)", value=st.session_state.get('verify_mx', False))


# Tabs
tab_search, tab_results, tab_email, tab_export = st.tabs(["Search", "Results", "Email", "Export/Import"]) 

with tab_search:
    st.subheader("Find GC / Builders / Architects near you")
    # Area presets
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
        radius_phrase = st.select_slider("Radius phrase", ["5 miles","10 miles","25 miles","50 miles"], value="25 miles")
    with col2:
        categories = st.multiselect("Categories", ["General Contractors","Builders","Architects"],
                                    default=["General Contractors","Builders","Architects"])
        rate_delay = st.slider("Delay between requests (sec)", 0.0, 3.0, 1.0, 0.1)
    max_sites = st.slider("Max sites (total)", 10, 200, 60, 10)

    if st.button("Search & Extract"):
        try:
            # Build gentle queries (no "email" keyword)
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
                        q, base_url=SERP_BASE_URL, key=SERP_KEY, method=SERP_METHOD,
                        auth_header=(SERP_AUTH_HEADER or None),
                        key_param=(SERP_KEY_PARAM or None),
                        count=per_q
                    )
                all_urls += urls
                progress.progress(int(((i+1)/max(len(queries),1))*100))
                time.sleep(rate_delay or 1.0)

            # Deduplicate by domain, keep first
            by_domain = {}
            for u in all_urls:
                d = domain_of(u) or u
                if d not in by_domain and not any(d.endswith(x) or d == x for x in EXCLUDE_DOMAINS):
                    by_domain[d] = u

            urls = list(by_domain.values())[:max_sites]
            st.write(f"Unique candidate sites: **{len(urls)}**")

            added = 0
            for j, base in enumerate(urls, start=1):
                # Try contact/about first (fallback to homepage)
                for path in ["", "/contact", "/contact-us", "/about", "/team"]:
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
                    time.sleep(rate_delay or 1.0)
                progress.progress(int((j/len(urls))*100))

            st.success(f"Added {added} contacts. Check **Results** tab.")
        except Exception as e:
            st.exception(e)

with tab_results:
    st.subheader("Leads")
    df = st.session_state.leads.copy()
    if df.empty:
        st.info("No leads yet. Run a search first.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"Total leads: {len(df)}")

with tab_email:
    st.subheader("Email campaign (SendGrid)")
    st.caption("Sender: info@miamimasterflooring.com (fixed). Set SENDGRID_API_KEY in Secrets to enable sending. Tip: Enable MX verification in sidebar to reduce bounces.")
    if not SENDGRID_AVAILABLE:
        st.warning("SendGrid not installed. Run `pip install sendgrid` or skip sending.")
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

    c1, c2 = st.columns(2)
    if c1.button("Send test to preview"):
        if not SENDGRID_AVAILABLE:
            st.error("SendGrid not installed.")
        elif not SENDGRID_API_KEY:
            st.error("SENDGRID_API_KEY not set in Secrets.")
        elif preview and preview != "no-data":
            try:
                code = send_email_sendgrid(preview, subject, render_html(greeting, body, signature))
                st.success(f"Sent to {preview} (HTTP {code})")
            except Exception as e:
                st.error(f"Send failed: {e}")

    if c2.button("Send campaign now (up to cap)"):
        if not SENDGRID_AVAILABLE:
            st.error("SendGrid not installed.")
        elif not SENDGRID_API_KEY:
            st.error("SENDGRID_API_KEY not set in Secrets.")
        else:
            sent = 0
            for e in emails:
                if sent >= daily_cap:
                    break
                try:
                    send_email_sendgrid(e, subject, render_html(greeting, body, signature))
                    sent += 1
                    time.sleep(0.3)
                except Exception:
                    continue
            st.success(f"Sent {sent} emails.")

with tab_export:
    st.subheader("Export / Import")
    df = st.session_state.leads.copy()
    colX, colY = st.columns(2)
    with colX:
        if not df.empty:
            st.download_button(
                "Download leads.csv", data=df.to_csv(index=False),
                file_name="leads.csv", mime="text/csv"
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
                    email = str(row.get("Email","") or "").strip()
                    if not email or not EMAIL_RE.match(email):
                        continue
                    if st.session_state.get('skip_generic') and is_generic_email(email):
                        continue
                    if st.session_state.get('verify_mx') and not verify_email_mx(email):
                        continue
                    if email.lower() in existing:
                        continue
                    st.session_state.leads.loc[len(st.session_state.leads)] = {
                        "Company": str(row.get("Company","") or "")[:120],
                        "Email": email,
                        "Website": str(row.get("Website","") or ""),
                        "Phone": str(row.get("Phone","") or ""),
                        "Source": "import",
                    }
                    existing.add(email.lower())
                    imported += 1
                st.success(f"Imported {imported} leads.")
            except Exception as e:
                st.error(f"Import failed: {e}")
