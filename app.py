import re
import time
import json
import random
from urllib.parse import urlparse, quote

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# ---------------------- App setup ----------------------
st.set_page_config(page_title="Prospector + SERP/Unlocker", layout="wide")
st.title("Local Prospector — GC / Builders / Architects (API-ready)")

# Sender identity (fixed as requested)
SENDER_NAME = st.secrets.get("SENDER_NAME", "Miami Master Flooring")
SENDER_EMAIL = "info@miamimasterflooring.com"
REPLY_TO = st.secrets.get("REPLY_TO", "info@miamimasterflooring.com")
SENDGRID_API_KEY = st.secrets.get("SENDGRID_API_KEY", "")

EMAIL_RE  = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE  = re.compile(r"\+?1?[\s\-\.\(]?\d{3}[\)\s\-\.\)]?\s?\d{3}\s?[\-\.\s]?\d{4}")
SOCIAL_DOMAINS = ("facebook.com","instagram.com","linkedin.com","twitter.com","x.com",
                  "youtube.com","yelp.com","angieslist.com","houzz.com","pinterest.com","tiktok.com")

if "leads" not in st.session_state:
    st.session_state.leads = pd.DataFrame(columns=["Company","Email","Website","Phone","Source"])

# ---------------------- Robust HTTP session ----------------------
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

def domain_of(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def looks_like_business_site(u: str) -> bool:
    d = domain_of(u)
    if not d:
        return False
    if any(s in d for s in SOCIAL_DOMAINS):
        return False
    return d.endswith(".com") or d.endswith(".net") or d.endswith(".org")

# ---------------------- Providers ----------------------
# 1) Bing Web Search API (official)
def search_bing_api(query: str, key: str, count: int = 20):
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

# 2) Generic SERP API (any provider that returns { webPages: { value: [{url: ...}] } } or simple list)
def search_serp_api(query: str, base_url: str, key: str, method: str = "GET",
                    auth_header: str = "X-API-KEY", key_param: str | None = None,
                    count: int = 20):
    if not base_url or not key:
        return []
    try:
        headers = {"User-Agent": HTTP.headers.get("User-Agent")}
        if auth_header:
            headers[auth_header] = key

        params = {"q": query, "count": count}
        if key_param:  # some APIs expect ?api_key=
            params[key_param] = key

        if method.upper() == "POST":
            r = HTTP.post(base_url, headers=headers, json=params, timeout=25)
        else:
            r = HTTP.get(base_url, headers=headers, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()

        urls = []
        # Try a few common shapes
        if isinstance(data, dict):
            if "webPages" in data and "value" in data["webPages"]:
                urls = [v.get("url") for v in data["webPages"]["value"] if v.get("url")]
            elif "results" in data and isinstance(data["results"], list):
                for item in data["results"]:
                    u = item.get("url") or item.get("link")
                    if u: urls.append(u)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str): urls.append(item)
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("link")
                    if u: urls.append(u)

        urls = [u for u in urls if u and looks_like_business_site(u)]
        return urls[:count]
    except Exception:
        return []

# 3) Unlocker for page fetch (route target page via Unlocker proxy/API)
def unlocker_fetch(url: str, unlocker_base: str, key: str,
                   key_header: str = "X-API-KEY", key_param: str | None = None) -> str | None:
    """
    Examples:
      unlocker_base = "https://unlocker.example.com/fetch"
      - Header auth:   key_header="X-API-KEY"
      - Query string:  key_param="api_key"  (in that case pass key_header=None)
    """
    try:
        headers = {"User-Agent": HTTP.headers.get("User-Agent")}
        if key_header:
            headers[key_header] = key
        params = {"url": url}
        if key_param:
            params[key_param] = key
        r = HTTP.get(unlocker_base, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        # many unlockers return raw HTML; some wrap JSON { html: "..."}
        if r.headers.get("Content-Type","").startswith("application/json"):
            j = r.json()
            return j.get("html")
        return r.text
    except Exception:
        return None

# ---------------------- Extraction ----------------------
def extract_company_info_from_html(html: str):
    if not html:
        return None, None, None
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    emails = EMAIL_RE.findall(text)
    phones = PHONE_RE.findall(text)
    company = None
    if soup.title and soup.title.string:
        company = soup.title.string.split(" | ")[0].split(" – ")[0].strip()[:120]
    if not company:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            company = h1.get_text(strip=True)[:120]
    return company, (emails[0] if emails else None), (phones[0] if phones else None)

def extract_company_info(url: str, unlocker_base: str = "", unlocker_key: str = "",
                         key_header: str = "X-API-KEY", key_param: str | None = None):
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

def upsert_lead(name, email, website, phone, source):
    if not email:
        return
    df = st.session_state.leads
    lowers = set(df["Email"].str.lower())
    if email.lower() in lowers:
        return
    st.session_state.leads.loc[len(df)] = {
        "Company": name or "", "Email": email.strip(), "Website": website,
        "Phone": phone or "", "Source": source
    }

# ---------------------- Email sending ----------------------
def send_email_sendgrid(to_email: str, subject: str, html: str) -> int:
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
        BING_API_KEY = st.secrets.get("BING_API_KEY", st.text_input("BING_API_KEY (or add to Secrets)", type="password"))
        SERP_BASE_URL = ""
        SERP_KEY = ""
        SERP_METHOD = "GET"
        SERP_AUTH_HEADER = "X-API-KEY"
        SERP_KEY_PARAM = ""
    else:
        SERP_BASE_URL = st.text_input("SERP Base URL (endpoint that returns JSON)")
        SERP_KEY = st.secrets.get("SERP_API_KEY", st.text_input("SERP API Key (or add to Secrets)", type="password"))
        SERP_METHOD = st.selectbox("SERP HTTP Method", ["GET","POST"], index=0)
        SERP_AUTH_HEADER = st.text_input("Auth Header (blank if using query param)", value="X-API-KEY")
        SERP_KEY_PARAM = st.text_input("Key Query Param (e.g., api_key)", value="")

    st.markdown("---")
    st.subheader("Unlocker (optional)")
    UNLOCKER_BASE = st.text_input("Unlocker fetch endpoint (optional)")
    UNLOCKER_KEY  = st.secrets.get("UNLOCKER_KEY", st.text_input("UNLOCKER_KEY (or add to Secrets)", type="password"))
    UNLOCKER_AUTH_HEADER = st.text_input("Unlocker Auth Header (blank if query param)", value="X-API-KEY")
    UNLOCKER_KEY_PARAM   = st.text_input("Unlocker Key Param (e.g., api_key)", value="")

tab_search, tab_results, tab_email, tab_export = st.tabs(["Search", "Results", "Email", "Export/Import"])

with tab_search:
    st.subheader("Find GC / Builders / Architects near you")
    col1, col2 = st.columns(2)
    with col1:
        location = st.text_input("City / Area", value="Miami, FL")
        radius_phrase = st.select_slider("Radius phrase", ["5 miles","10 miles","25 miles","50 miles"], value="25 miles")
    with col2:
        categories = st.multiselect("Categories", ["General Contractors","Builders","Architects"],
                                    default=["General Contractors","Builders","Architects"])
        rate_delay = st.slider("Delay between requests (sec)", 0.0, 3.0, 1.0, 0.1)
    max_sites = st.slider("Max sites (total)", 10, 200, 60, 10)

    if st.button("Search & Extract"):
        # Build gentle queries (no "email" keyword)
        queries = []
        if "General Contractors" in categories:
            queries.append(f'General Contractors "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')
        if "Builders" in categories:
            queries.append(f'Home Builders "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')
        if "Archite
