# -*- coding: utf-8 -*-
import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import pandas as pd
import time
import random
from urllib.parse import urljoin, urlencode
import tldextract

APP_VERSION = "1.0.1"
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
        # Trim separators (ASCII + Unicode em dash) — avoid raw Unicode literals that may break on some setups
        for sep in ["|", "-", "::", "·", "—"]:
            if sep in title:
                title = title.split(sep)[0].strip()
        if len(title) >= 2:
            return title
    # Fallback H1
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return fallback_domain
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        for sep in ["|", "-", "\u2014", "·"]:
            if sep in title:
                title = title.split(sep)[0].strip()
        if len(title) >= 2:
            return title
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return fallback_domain

def extract_emails_and_phone(html: str):
    emails = set(re.findall(EMAIL_REGEX, html or ""))
    phones = set(re.findall(PHONE_REGEX, html or ""))
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
    return list(dict.fromkeys(urls))

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

# Streamlit UI setup remains same as before
