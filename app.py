# prospector_app.py
import random
import re
import time
from urllib.parse import quote, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from urllib3.util.retry import Retry

# ---------------------- App setup ----------------------
st.set_page_config(page_title="Local Prospector (GC/Builders/Architects)", layout="wide")
st.title("Local Prospector — General Contractors, Builders, Architects")

# Sender config (can be overridden via Secrets)
SENDER_NAME = st.secrets.get("SENDER_NAME", "Miami Master Flooring")
SENDER_EMAIL = st.secrets.get("SENDER_EMAIL", "info@miamimasterflooring.com")
REPLY_TO = st.secrets.get("REPLY_TO", "info@miamimasterflooring.com")
SENDGRID_API_KEY = st.secrets.get("SENDGRID_API_KEY", "")

# Regex patterns
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\+?1?[\s\-\.\(]?\d{3}[\)\s\-\.\)]?\s?\d{3}\s?[\-\.\s]?\d{4}")

SOCIAL_BLOCKLIST = (
    "facebook.com","instagram.com","linkedin.com","twitter.com","x.com","youtube.com",
    "yelp.com","angieslist.com","houzz.com","pinterest.com","tiktok.com"
)

if "leads" not in st.session_state:
    st.session_state.leads = pd.DataFrame(columns=["Company", "Email", "Website", "Phone", "Source"])

# ---------------------- HTTP session w/ retries ----------------------
def http_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        ]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": "https://duckduckgo.com/",
    })
    retry = Retry(
        total=5, connect=3, read=3, status=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET","POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

_session = http_session()

def http_get(url, timeout=20):
    r = _session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

# ---------------------- Helpers ----------------------
def domain_of(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def looks_like_business_site(u: str) -> bool:
    d = domain_of(u)
    if not d:
        return False
    if any(s in d for s in SOCIAL_BLOCKLIST):
        return False
    return d.endswith(".com") or d.endswith(".net") or d.endswith(".org")

def try_candidate_pages(base_url: str):
    root = base_url.rstrip("/")
    return [base_url, f"{root}/contact", f"{root}/contact-us", f"{root}/about", f"{root}/team"]

# ---------------------- Search engines ----------------------
def search_ddg(query: str, max_links: int = 25):
    # Use GET on /html/, softer queries
    try:
        url = f"https://duckduckgo.com/html/?q={quote(query)}&kl=us-en"
        html = http_get(url)
        soup = BeautifulSoup(html, "html.parser")
        raw = [a.get("href") for a in soup.select("a.result__a") if a.get("href")]
        seen, out = set(), []
        for u in raw:
            if u.startswith("http") and looks_like_business_site(u) and u not in seen:
                seen.add(u); out.append(u)
                if len(out) >= max_links: break
        return out
    except Exception:
        return []

def search_bing_html(query: str, max_links: int = 25):
    try:
        url = f"https://www.bing.com/search?q={quote(query)}&count=50"
        html = http_get(url)
        soup = BeautifulSoup(html, "html.parser")
        raw = [a.get("href") for a in soup.select("li.b_algo h2 a") if a.get("href")]
        seen, out = set(), []
        for u in raw:
            if u.startswith("http") and looks_like_business_site(u) and u not in seen:
                seen.add(u); out.append(u)
                if len(out) >= max_links: break
        return out
    except Exception:
        return []

def search_bing_api(query: str, max_links: int = 15):
    key = st.secrets.get("BING_API_KEY")
    if not key:
        return []
    try:
        endpoint = "https://api.bing.microsoft.com/v7.0/search"
        params = {"q": query, "mkt": "en-US", "count": max_links}
        headers = {"Ocp-Apim-Subscription-Key": key}
        r = requests.get(endpoint, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        out = []
        for w in (data.get("webPages") or {}).get("value", []):
            u = w.get("url")
            if u and looks_like_business_site(u):
                out.append(u)
        return out[:max_links]
    except Exception:
        return []

# ---------------------- Extraction ----------------------
def extract_company_info(url: str):
    try:
        html = http_get(url)
    except Exception:
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

    email = emails[0] if emails else None
    phone = phones[0] if phones else None
    return company, email, phone

def upsert_lead(name, email, website, phone, source):
    if not email:
        return
    df = st.session_state.leads
    lowers = set(df["Email"].str.lower())
    if email.lower() in lowers:
        return
    st.session_state.leads.loc[len(df)] = {
        "Company": name or "",
        "Email": email.strip(),
        "Website": website,
        "Phone": phone or "",
        "Source": source,
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
tab_search, tab_results, tab_email, tab_export = st.tabs(["Search", "Results", "Email", "Export/Import"])

with tab_search:
    st.subheader("Find GC / Builders / Architects near you")
    col1, col2 = st.columns(2)
    with col1:
        location = st.text_input("City / Area", value="Miami, FL")
        radius_phrase = st.select_slider(
            "Radius phrase (used only in query text, not geo-filter)",
            options=["5 miles", "10 miles", "25 miles", "50 miles"],
            value="25 miles",
        )
    with col2:
        categories = st.multiselect(
            "Categories",
            ["General Contractors", "Builders", "Architects"],
            default=["General Contractors", "Builders", "Architects"],
        )
        rate_delay = st.slider("Rate-limit between requests (seconds)", 0.0, 3.0, 1.0, 0.1)
    max_sites = st.slider("Max sites to check (total)", 10, 200, 60, 10)
    st.caption("Tip: keep a small delay to be polite; some sites block rapid requests.")

    if st.button("Search & Extract"):
        st.info("Searching DuckDuckGo + Bing … (gentle mode)")
        # Build gentle queries (avoid the word 'email' which can trigger blocking)
        query_buckets = []
        if "General Contractors" in categories:
            query_buckets.append(f'General Contractors "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')
        if "Builders" in categories:
            query_buckets.append(f'Home Builders "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')
        if "Architects" in categories:
            query_buckets.append(f'Architecture Firms "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')

        per_query_cap = max(10, max_sites // max(len(query_buckets), 1))
        all_links = []

        for q in query_buckets:
            ddg = search_ddg(q, max_links=per_query_cap)
            time.sleep(rate_delay or 1.0)
            bing = search_bing_html(q, max_links=per_query_cap)
            time.sleep(rate_delay or 1.0)
            bing_api = search_bing_api(q, max_links=min(10, per_query_cap // 2))
            all_links += ddg + bing + bing_api

        # De-dup by domain
        by_domain = {}
        for u in all_links:
            d = domain_of(u) or u
            if d not in by_domain:
                by_domain[d] = u

        urls = list(by_domain.values())[:max_sites]
        st.write(f"Unique candidate sites: **{len(urls)}**")

        # Crawl contact/about/home for emails
        added = 0
        for base in urls:
            for page in try_candidate_pages(base):
                name, email, phone = extract_company_info(page)
                if email:
                    upsert_lead(name, email, base, phone, "scrape")
                    added += 1
                    break
                time.sleep(rate_delay or 1.0)

        st.success(f"Added {added} contacts with emails. Go to **Results** tab.")

with tab_results:
    st.subheader("Leads")
    df = st.session_state.leads.copy()
    if df.empty:
        st.info("No leads yet. Run a search first.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.write(f"Total leads: **{len(df)}**")

with tab_email:
    st.subheader("Email campaign (SendGrid)")
    st.caption("Set SENDGRID_API_KEY in Streamlit **Settings → Secrets** to enable sending.")
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
        if not SENDGRID_API_KEY:
            st.error("SENDGRID_API_KEY not set in Secrets.")
        elif preview and preview != "no-data":
            try:
                code = send_email_sendgrid(preview, subject, render_html(greeting, body, signature))
                st.success(f"Sent to {preview} (HTTP {code})")
            except Exception as e:
                st.error(f"Send failed: {e}")

    if c2.button("Send campaign now (up to cap)"):
        if not SENDGRID_API_KEY:
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

                existing = set(st.session_state.leads["Email"].str.lower())
                imported = 0
                for _, row in new.iterrows():
                    email = str(row.get("Email", "") or "").strip()
                    if not email or not EMAIL_RE.match(email):
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
