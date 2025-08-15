# app.py
import re
import time
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# ---- CONFIG ----
SENDER_EMAIL = "info@miamimasterflooring.com"
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# ---- STREAMLIT SETTINGS ----
st.set_page_config(page_title="Miami Flooring Prospector", layout="wide")
st.title("🏗 Miami Master Flooring Lead Finder")
st.caption(f"Sender email: **{SENDER_EMAIL}**")

# ---- SESSION STORAGE ----
if "leads" not in st.session_state:
    st.session_state.leads = pd.DataFrame(columns=["Company", "Email", "Website"])

# ---- FUNCTIONS ----
def get_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except:
        return None

def search_bing(query, api_key, max_results=20):
    """Search Bing API for sites matching query"""
    endpoint = "https://api.bing.microsoft.com/v7.0/search"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {"q": query, "count": max_results}
    try:
        r = requests.get(endpoint, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [item["url"] for item in data.get("webPages", {}).get("value", [])]
    except Exception as e:
        st.error(f"Search error: {e}")
        return []

def extract_email_from_site(url):
    """Fetch site and return first found email"""
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        emails = EMAIL_PATTERN.findall(text)
        return emails[0] if emails else None
    except:
        return None

# ---- UI ----
st.subheader("Search for Leads")
location = st.text_input("Location (City, State)", "Miami, FL")
category = st.selectbox("Category", ["General Contractor", "Builder", "Architect"])
max_sites = st.slider("Max sites to scan", 5, 50, 15)
api_key = st.text_input("Bing Search API Key", type="password")

if st.button("Find Leads"):
    if not api_key:
        st.error("Please enter your Bing Search API key.")
    else:
        query = f'{category} "{location}" site:.com'
        urls = search_bing(query, api_key, max_results=max_sites)
        found = 0
        for u in urls:
            email = extract_email_from_site(u)
            if email:
                domain = get_domain(u)
                company = domain.replace("www.", "") if domain else ""
                if email.lower() not in [e.lower() for e in st.session_state.leads["Email"]]:
                    st.session_state.leads.loc[len(st.session_state.leads)] = [company, email, u]
                    found += 1
            time.sleep(1)
        st.success(f"Found {found} new leads!")

# ---- RESULTS ----
st.subheader("Leads Found")
if not st.session_state.leads.empty:
    st.dataframe(st.session_state.leads, use_container_width=True)
    csv_data = st.session_state.leads.to_csv(index=False)
    st.download_button("Download Leads CSV", data=csv_data, file_name="leads.csv", mime="text/csv")
else:
    st.info("No leads yet.")

