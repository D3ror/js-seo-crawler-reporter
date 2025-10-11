import streamlit as st
import pandas as pd
import requests
import json
import time
from io import BytesIO

# ==========================================
# CONFIGURATION
# ==========================================
st.set_page_config(page_title="JS SEO Crawler", layout="wide")

# Example local or remote API base
API_BASE_URL = st.secrets.get("API_BASE_URL", "http://127.0.0.1:8080")

# ==========================================
# SIDEBAR SETTINGS
# ==========================================
st.sidebar.header("Crawl Settings")
urls = st.sidebar.text_area("Enter one or more URLs (one per line)")
depth = st.sidebar.slider("Crawl depth", 1, 5, 2)
concurrency = st.sidebar.slider("Concurrent requests", 1, 10, 3)
delay = st.sidebar.number_input("Request delay (seconds)", 0.0, 5.0, 0.5, 0.1)
headless = st.sidebar.checkbox("Run headless browser", value=True)

export_format = st.sidebar.selectbox("Export format", ["CSV", "JSON", "Parquet"])
run_button = st.sidebar.button("🚀 Start Crawl")

# ==========================================
# MAIN TABS
# ==========================================
tabs = st.tabs([
    "Crawl", "Pages", "SEO Data", "Structured Data", "Links", "Core Web Vitals"
])

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def call_crawl_api(url_list):
    """Send crawl request to FastAPI backend."""
    payload = {
        "urls": url_list,
        "depth": depth,
        "concurrency": concurrency,
        "delay": delay,
        "headless": headless
    }
    try:
        response = requests.post(f"{API_BASE_URL}/crawl", json=payload, timeout=600)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        st.error(f"API error: {e}")
        return None

def call_cwv_api(url):
    """Fetch CWV data via FastAPI (calls PSI API)."""
    try:
        resp = requests.get(f"{API_BASE_URL}/cwv", params={"url": url}, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        st.warning(f"Failed to fetch CWV for {url}: {e}")
        return None

# ==========================================
# TAB: CRAWL
# ==========================================
with tabs[0]:
    st.header("Crawl Progress")

    if run_button:
        if not urls.strip():
            st.warning("Please enter at least one URL.")
        else:
            url_list = [u.strip() for u in urls.splitlines() if u.strip()]
            st.info(f"Starting crawl for {len(url_list)} URL(s)...")
            progress = st.progress(0)
            status = st.empty()

            # --- CALL FASTAPI ---
            crawl_data = call_crawl_api(url_list)
            progress.progress(100)
            status.text("✅ Crawl complete.")

            if crawl_data:
                st.session_state["crawl_results"] = crawl_data
                st.success("Crawl data received from API.")

# ==========================================
# TAB: PAGES
# ==========================================
with tabs[1]:
    st.header("Crawled Pages Overview")
    if "crawl_results" in st.session_state:
        df = pd.DataFrame(st.session_state["crawl_results"].get("pages", []))
        st.session_state["df"] = df
        st.dataframe(df, use_container_width=True)
    else:
        st.info("Run a crawl to view results.")

# ==========================================
# TAB: SEO DATA
# ==========================================
with tabs[2]:
    st.header("SEO Elements (Meta, Canonical, Robots, Headings)")
    if "df" in st.session_state:
        seo_cols = ["url", "status", "title", "meta_description", "canonical", "robots", "hreflang"]
        cols = [c for c in seo_cols if c in st.session_state["df"].columns]
        st.dataframe(st.session_state["df"][cols])
    else:
        st.info("Run a crawl to display SEO data.")

# ==========================================
# TAB: STRUCTURED DATA
# ==========================================
with tabs[3]:
    st.header("Structured Data (JSON-LD, Microdata, RDFa)")
    if "crawl_results" in st.session_state:
        structured = st.session_state["crawl_results"].get("structured_data", [])
        if structured:
            st.json(structured)
        else:
            st.info("No structured data detected.")
    else:
        st.info("Run a crawl first.")

# ==========================================
# TAB: LINKS
# ==========================================
with tabs[4]:
    st.header("Links Overview (Internal / External)")
    if "crawl_results" in st.session_state:
        links_data = st.session_state["crawl_results"].get("links", {})
        if links_data:
            st.metric("Internal Links", len(links_data.get("internal", [])))
            st.metric("External Links", len(links_data.get("external", [])))
            st.json(links_data)
        else:
            st.info("No link data available.")
    else:
        st.info("Run a crawl to view link data.")

# ==========================================
# TAB: CORE WEB VITALS
# ==========================================
with tabs[5]:
    st.header("Core Web Vitals (CWV)")
    if "df" in st.session_state:
        selected_url = st.selectbox("Select a URL to get CWV metrics", st.session_state["df"]["url"])
        if st.button("Fetch CWV"):
            cwv_data = call_cwv_api(selected_url)
            if cwv_data:
                st.json(cwv_data)
            else:
                st.warning("No CWV data found.")
    else:
        st.info("Run a crawl first to see CWV metrics.")

# ==========================================
# EXPORT RESULTS
# ==========================================
if "df" in st.session_state and not st.session_state["df"].empty:
    st.sidebar.markdown("### Export Results")
    df = st.session_state["df"]
    if export_format == "CSV":
        csv = df.to_csv(index=False).encode("utf-8")
        st.sidebar.download_button("⬇️ Download CSV", csv, "crawl_results.csv")
    elif export_format == "JSON":
        json_bytes = df.to_json(orient="records").encode("utf-8")
        st.sidebar.download_button("⬇️ Download JSON", json_bytes, "crawl_results.json")
    elif export_format == "Parquet":
        buffer = BytesIO()
        df.to_parquet(buffer, index=False)
        st.sidebar.download_button("⬇️ Download Parquet", buffer.getvalue(), "crawl_results.parquet")
