import streamlit as st
import requests
import re
import pandas as pd
from bs4 import BeautifulSoup
import time

# --- Config ---
st.set_page_config(page_title="Miami Flooring Prospector", layout="wide")

# Regex patterns
EMAIL_PATTERN = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
PHONE_PATTERN = r'\(?\b[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}\b'

# --- Functions ---
def search_duckduckgo(query):
    url = "https://duckduckgo.com/html/"
    params = {"q": query}
    res = requests.post(url, data=params, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")
    results = []
    for link in soup.find_all("a", class_="result__a", href=True):
        results.append(link["href"])
    return results

def search_bing(query):
    url = "https://www.bing.com/search"
    params = {"q": query}
    res = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")
    results = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.startswith("http") and ".bing.com" not in href:
            results.append(href)
    return results

def extract_contact_info(url):
    try:
        res = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        text = res.text
        emails = list(set(re.findall(EMAIL_PATTERN, text)))
        phones = list(set(re.findall(PHONE_PATTERN, text)))
        return emails, phones
    except:
        return [], []

# --- UI ---
st.title("🏗 Miami Flooring Prospector")
st.markdown("Find **General Contractors, Builders, and Architects** in South Florida and collect their contact info.")

query_location = st.text_input("Enter target location:", "Miami, FL")
search_btn = st.button("Search")

if "leads" not in st.session_state:
    st.session_state.leads = []

if search_btn:
    st.session_state.leads.clear()
    search_terms = [
        f"General Contractors {query_location}",
        f"Construction Companies {query_location}",
        f"Architecture Firms {query_location}",
        f"Flooring Installation Contractors {query_location}",
        f"Commercial Flooring Companies {query_location}",
        f"Tile Installation Specialists {query_location}"
    ]

    urls_found = set()
    for term in search_terms:
        st.write(f"Searching: {term}")
        urls_found.update(search_duckduckgo(term))
        urls_found.update(search_bing(term))
        time.sleep(1)

    st.write(f"🔍 Found {len(urls_found)} unique URLs. Extracting contact info...")

    for url in urls_found:
        emails, phones = extract_contact_info(url)
        if emails or phones:
            st.session_state.leads.append({
                "URL": url,
                "Emails": ", ".join(emails),
                "Phones": ", ".join(phones)
            })

    st.success(f"✅ Collected {len(st.session_state.leads)} leads.")

if st.session_state.leads:
    df = pd.DataFrame(st.session_state.leads)
    st.dataframe(df)

    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Download CSV", csv, "leads.csv", "text/csv")

    uploaded_csv = st.file_uploader("Upload CSV to merge leads", type="csv")
    if uploaded_csv:
        new_df = pd.read_csv(uploaded_csv)
        merged_df = pd.concat([df, new_df]).drop_duplicates()
        st.session_state.leads = merged_df.to_dict(orient="records")
        st.success("CSV merged successfully!")
