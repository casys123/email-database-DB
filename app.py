# -*- coding: utf-8 -*-
"""
MMF LeadHarvester — Streamlit app
Find General Contractors, Builders, and Architects in Miami-Dade & Broward,
extract public emails/phones from their sites, and export to CSV.

Works on first run — no API keys. Uses DuckDuckGo HTML + light crawling.
"""
import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import pandas as pd
import time
import random
from urllib.parse import urljoin, urlencode
import tldextract

APP_VERSION = "1.0.2"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_REGEX = re.compile(r"(?:(?:\+?1\s*[.-]?\s*)?(?:\(\d{3}\)|\d{3})\s*[.-]?\s*\d{3}\s*[.-]?\s*\d{4})")

MIAMI_DADE_CITIES = [
    "Aventura", "Bal Harbour", "Bay Harbor Islands", "Biscayne Park", "Coral Gables",
    "Cutler Bay", "Doral", "El Portal", "Florida City", "Golden Beach", "Hialeah",
    "Hialeah Gardens", "Homestead", "Indian Creek", "Key Biscayne", "Medley",
    "Miami", "Miami Beach", "Miami Gardens", "Miami Lakes", "Miami Shores",
    "Miami Springs", "North Bay Village", "North Miami", "North Miami Beach",
    "Opa-locka", "Palmetto Bay", "Pinecrest", "South Miami", "Sunny Isles Beach",
    "Surfside", "Sweetwater", "Virginia Gardens", "West Miami"
]

BROWARD_CITIES = [
    "Coconut Creek", "Cooper City", "Coral Springs", "Dania Beach", "Davie",
    "Deerfield Beach", "Fort Lauderdale", "Hallandale Beach", "Hollywood",
    "Lauderdale Lakes", "Lauderhill", "Lazy Lake", "Lighthouse Point", "Margate",
    "Miramar", "North Lauderdale", "Oakland Park", "Parkland", "Pembroke Pines",
    "Plantation", "Pompano Beach", "Sea Ranch Lakes", "Sunrise", "Tamarac",
    "West Park", "Weston", "Wilton Manors"
]

CATEGORIES = {
    "General Contractors": ["general contractor", "licensed contractor", "building contractor"],
    "Builders": ["builder", "home builder", "construction company"],
    "Architects": ["architect", "architecture firm", "architectural services"],
}

DEFAULT_CONTACT_PATHS = ["/contact", "/contact-us", "/contactus", "/about", "/about-us"]


def duckduckgo_search(query: str, max_results: int = 20, pause: float = 1.5):
    """Fetch organic results from DuckDuckGo HTML interface (no API key)."""
    results = []
    params = {"q": query}
    url = "https://duckduckgo.com/html/?" + urlencode(params)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.warning(f"Search error for '{query}': {e}")
        return results

    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.select("a.result__a"):
        title = a.get_text(strip=True)
        href = a.get("href")
        if href and href.startswith("http"):
            results.append({"title": title, "href": href})
        if len(results) >= max_results:
            break

    time.sleep(pause + random.uniform(0, 1.0))
    return results


def fetch_page(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""


def guess_company_name(html: str, fallback_domain: str) -> str:
    if not html:
        return fallback_domain
    soup = BeautifulSoup(html, "html.parser")
    # Prefer <title>
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        # Trim separators (ASCII + Unicode em dash via \u2014)
        for sep in ["|", "-", "::", "\u2014", "\u00b7"]:  # | - :: — ·
            if sep in title:
                title = title.split(sep)[0].strip()
        if len(title) >= 2:
            return title
    # Fallback H1
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return fallback_domain


def extract_emails_and_phone(html: str):
    emails = set(re.findall(EMAIL_REGEX, html or ""))
    phones = set(re.findall(PHONE_REGEX, html or ""))
    # Basic noise filtering
    emails = {e for e in emails if not any(bad in e.lower() for bad in ["example.com", "test@", ".png", ".jpg", ".svg"])}
    return emails, phones


def is_relevant_to_counties(text: str) -> str:
    t = (text or "").lower()
    md_kw = ["miami-dade", "miami dade", "miami, fl", "miami beach", "coral gables", "hialeah", "kendall", "dade county"]
    br_kw = ["broward", "fort lauderdale", "hollywood, fl", "pompano", "plantation, fl", "weston"]
    if any(k in t for k in md_kw):
        return "Miami-Dade"
    if any(k in t for k in br_kw):
        return "Broward"
    return ""


def candidate_contact_urls(base_url: str):
    urls = [base_url]
    for path in DEFAULT_CONTACT_PATHS:
        urls.append(urljoin(base_url, path))
    return list(dict.fromkeys(urls))  # dedupe preserve order


def crawl_site_for_leads(site_url: str, category_label: str, max_pages: int = 3):
    leads = []
    ext = tldextract.extract(site_url)
    domain = f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain

    for url in candidate_contact_urls(site_url)[:max_pages]:
        html = fetch_page(url)
        if not html:
            continue
        emails, phones = extract_emails_and_phone(html)
        company_name = guess_company_name(html, domain)
        county_guess = is_relevant_to_counties(html)
        for email in emails or {""}:
            leads.append({
                "company_name": company_name,
                "email": email,
                "phone": "; ".join(sorted(phones)) if phones else "",
                "website": site_url,
                "source_url": url,
                "category": category_label,
                "county_guess": county_guess,
            })
        time.sleep(0.8 + random.uniform(0, 0.8))
    return leads


def unique_sites_from_results(results):
    seen = set()
    unique = []
    for r in results:
        href = r.get("href", "")
        ext = tldextract.extract(href)
        if not ext.domain:
            continue
        root = f"https://{ext.registered_domain}" if ext.registered_domain else href
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def build_queries(selected_categories, city_scope, extra_terms):
    queries = []
    cities = []
    if city_scope == "Miami-Dade":
        cities = MIAMI_DADE_CITIES
    elif city_scope == "Broward":
        cities = BROWARD_CITIES
    else:
        cities = MIAMI_DADE_CITIES + BROWARD_CITIES

    for cat_label in selected_categories:
        terms = CATEGORIES[cat_label]
        for term in terms:
            for city in cities:
                q = f"{term} {city} Florida email"
                if extra_terms:
                    q += f" {extra_terms}"
                queries.append(q)
    return queries


# =============================
# Streamlit UI
# =============================

st.set_page_config(page_title="MMF LeadHarvester", page_icon="📇", layout="wide")
st.title("📇 MMF LeadHarvester — Miami-Dade & Broward")
st.caption(f"Version {APP_VERSION} · No API keys required · Uses DuckDuckGo + light crawling")

with st.sidebar:
    st.header("Search Settings")
    st.write("Focus: General Contractors, Builders, Architects in Miami-Dade & Broward.")

    categories = st.multiselect(
        "Categories", list(CATEGORIES.keys()), default=list(CATEGORIES.keys())
    )

    county_scope = st.selectbox(
        "County Scope", ["Miami-Dade", "Broward", "Both"], index=2
    )

    max_search_results = st.slider(
        "Max search results per query", min_value=3, max_value=30, value=8, step=1
    )

    max_sites_to_crawl = st.slider(
        "Max unique sites to crawl (total)", min_value=10, max_value=200, value=60, step=10
    )

    max_pages_per_site = st.slider(
        "Max pages per site (contact/about/home)", min_value=1, max_value=5, value=3, step=1
    )

    extra_terms = st.text_input(
        "Extra search terms (optional)", value="licensed, Florida, GC, architect"
    )

    dedupe_emails = st.checkbox("De-duplicate by email", value=True)

    st.markdown("---")
    st.subheader("Optional: Upload seed websites/CSV")
    st.write("Upload a CSV with a 'website' column to force-crawl those sites too.")
    seed_file = st.file_uploader("Seed CSV (optional)", type=["csv"])

run_button = st.button("🚀 Run Search & Crawl")

results_df = pd.DataFrame()

if run_button:
    if not categories:
        st.error("Please select at least one category.")
        st.stop()

    st.info("Starting searches… this can take a few minutes. Keep limits modest for best stability.")

    queries = build_queries(categories, county_scope, extra_terms)
    random.shuffle(queries)

    all_results = []
    progress = st.progress(0)
    status = st.empty()

    for i, q in enumerate(queries, start=1):
        status.write(f"🔎 Searching: {q}")
        hits = duckduckgo_search(q, max_results=max_search_results)
        all_results.extend(hits)
        progress.progress(i / len(queries))

    status.write("🔗 Consolidating sites…")
    unique_sites = unique_sites_from_results(all_results)

    # Add seed sites from upload
    if seed_file is not None:
        try:
            seed_df = pd.read_csv(seed_file)
            seed_sites = [u if str(u).startswith("http") else f"https://{u}" for u in seed_df.get("website", []) if pd.notna(u)]
            unique_sites = list(dict.fromkeys(unique_sites + seed_sites))
        except Exception as e:
            st.warning(f"Could not read seed CSV: {e}")

    # Limit crawl
    if len(unique_sites) > max_sites_to_crawl:
        unique_sites = unique_sites[:max_sites_to_crawl]

    st.write(f"Found **{len(unique_sites)}** unique sites to crawl.")

    lead_rows = []
    crawl_bar = st.progress(0)
    for idx, site in enumerate(unique_sites, start=1):
        label = "Unknown"
        host_text = site.lower()
        if any(x in host_text for x in ["build", "contract", "construct", "gc"]):
            label = "General Contractors/Builders"
        if any(x in host_text for x in ["archi"]):
            label = "Architects"

        site_rows = crawl_site_for_leads(site, label, max_pages=max_pages_per_site)
        lead_rows.extend(site_rows)
        crawl_bar.progress(idx / len(unique_sites))

    if not lead_rows:
        st.warning("No leads found. Try increasing limits or adjusting search terms.")
    else:
        results_df = pd.DataFrame(lead_rows)
        # Filter to counties if guessed
        if county_scope != "Both":
            results_df = results_df[(results_df["county_guess"] == county_scope) | (results_df["county_guess"] == "")]

        # Clean + dedupe
        results_df["email"] = results_df["email"].fillna("")
        if dedupe_emails:
            results_df = results_df.drop_duplicates(subset=["email"]).reset_index(drop=True)
        results_df = results_df.drop_duplicates().reset_index(drop=True)

        # Preferred columns ordering
        cols = ["company_name", "email", "phone", "website", "source_url", "category", "county_guess"]
        results_df = results_df.reindex(columns=cols)

        st.success(f"Leads found: {len(results_df)}")
        st.dataframe(results_df, use_container_width=True)

        csv = results_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download CSV",
            data=csv,
            file_name="mmf_leads_miami_broward.csv",
            mime="text/csv",
        )

st.markdown("---")
st.subheader("How to use the list")
st.markdown(
    """
- Use your sender email **info@miamimasterflooring.com** with a verified domain.
- Send small, thoughtful outreach sequences (2–3 messages) highlighting rental-unit refresh expertise, quick turnarounds, and competitive pricing.
- Always include an unsubscribe option (CAN-SPAM).
- Consider validating emails before sending to reduce bounces.
    """
)

st.caption("© Miami Master Flooring · MMF LeadHarvester · Research and outreach to publicly listed contacts only.")
