import streamlit as st
import pandas as pd
import requests
from io import BytesIO

# ==========================================
# CONFIGURATION
# ==========================================
st.set_page_config(page_title="JS SEO Crawler", layout="wide")

API_BASE_URL = st.secrets.get("API_BASE_URL", "http://127.0.0.1:8080")

# Demo limits
MAX_DEMO_URLS_LIST = 10
MAX_DEMO_DOMAIN_PAGES = 10

# ==========================================
# SIDEBAR SETTINGS
# ==========================================
st.sidebar.header("Crawl Settings")

crawl_mode = st.sidebar.radio(
    "Crawl mode",
    ["URL list", "Domain crawl"],
    index=0,
    help=(
        "URL list: crawl exactly the URLs you provide.\n"
        "Domain crawl: use the first URL as a seed and follow internal links."
    ),
)

urls = st.sidebar.text_area(
    "Enter one or more URLs (one per line)",
    help=(
        "For 'Domain crawl', the first URL is used as the seed URL. "
        "Additional URLs are ignored."
    ),
)

# Domain crawl limits (restricted for demo stability)
max_pages = st.sidebar.slider(
    "Max pages (domain crawl)",
    min_value=1,
    max_value=MAX_DEMO_DOMAIN_PAGES,
    value=MAX_DEMO_DOMAIN_PAGES,
    step=1,
)
max_depth = st.sidebar.slider(
    "Max depth (domain crawl)", min_value=1, max_value=3, value=2
)

# Reserved for future use, still sent to API
depth = st.sidebar.slider("Crawl depth (reserved)", 1, 5, 2)
concurrency = st.sidebar.slider("Concurrent requests (reserved)", 1, 10, 3)
delay = st.sidebar.number_input(
    "Request delay (seconds, reserved)", 0.0, 5.0, 0.5, 0.1
)

headless = st.sidebar.checkbox(
    "Run as headless browser",
    value=True,
    help=(
        "Simulates how a real browser renders a page (including JavaScript). "
        "Useful for diagnosing content differences or client-side rendering issues."
    ),
)

emulate_googlebot = st.sidebar.checkbox(
    "Run as Googlebot",
    value=False,
    help=(
        "Uses a Googlebot-like User-Agent to simulate Google’s own crawler. "
        "Helpful for verifying how search engines see and index your pages."
    ),
)

export_format = st.sidebar.selectbox("Export format", ["CSV", "JSON", "Parquet"])
run_button = st.sidebar.button("🚀 Start Crawl")

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def call_crawl_api(url_list):
    """
    Send crawl request to FastAPI backend.
    Can be used for a single URL or a list (list mode or domain mode).
    """
    mode = "domain" if crawl_mode == "Domain crawl" else "list"

    payload = {
        "urls": url_list,
        "mode": mode,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "depth": depth,
        "concurrency": concurrency,
        "delay": delay,
        "headless": headless,
        "emulate_googlebot": emulate_googlebot,
    }

    try:
        resp = requests.post(f"{API_BASE_URL}/crawl", json=payload, timeout=600)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 502:
            st.warning(
                "⚠️ **502!** This usually indicates that the machine was sleeping or restarting. "
                "Please wait a few seconds and try running the crawl again."
            )
        elif status and 500 <= status < 600:
            st.error(
                f"Crawler API error (server-side, {status}). "
                "This is likely an internal error or resource limit on the crawler."
            )
        elif status and 400 <= status < 500:
            st.error(
                f"Crawler API error (client-side, {status}). "
                "Please check the URLs and crawl settings."
            )
        else:
            st.error(f"API HTTP error ({status}): {e}")
        return None
    except requests.exceptions.RequestException as e:
        st.error(f"Network error when calling API: {e}")
        return None
    except Exception as e:
        st.error(f"Unexpected API error: {e}")
        return None


# ==========================================
# MAIN HEADER & CRAWL TRIGGER
# ==========================================
st.title("JS SEO Crawler & CWV Reporter")

if run_button:
    if not urls.strip():
        st.warning("Please enter at least one URL.")
    else:
        url_list = [u.strip() for u in urls.splitlines() if u.strip()]

        if crawl_mode == "Domain crawl":
            # Only the first URL is used as seed
            seed_url = url_list[0]
            with st.spinner(f"Domain crawl starting from {seed_url}..."):
                crawl_data = call_crawl_api([seed_url])

            if crawl_data:
                st.session_state["crawl_results"] = crawl_data
                df = pd.DataFrame(crawl_data.get("pages", []))
                st.session_state["df"] = df
                st.success("✅ Domain crawl complete.")
            else:
                st.error("Domain crawl failed or returned no data.")
        else:
            # URL list mode: cap at 10 for demo stability
            if len(url_list) > MAX_DEMO_URLS_LIST:
                st.warning(
                    f"For demo purposes, a maximum of {MAX_DEMO_URLS_LIST} URLs is allowed per crawl. "
                    f"Only the first {MAX_DEMO_URLS_LIST} URLs will be used."
                )
                url_list = url_list[:MAX_DEMO_URLS_LIST]

            total = len(url_list)
            progress = st.progress(0)
            status_placeholder = st.empty()

            all_pages = []
            all_structured = []
            all_internal = set()
            all_external = set()

            for i, url in enumerate(url_list, start=1):
                status_placeholder.info(f"Crawling {i}/{total}: {url}")
                crawl_data = call_crawl_api([url])

                if crawl_data:
                    all_pages.extend(crawl_data.get("pages", []))
                    all_structured.extend(crawl_data.get("structured_data", []))
                    all_internal.update(crawl_data.get("links", {}).get("internal", []))
                    all_external.update(crawl_data.get("links", {}).get("external", []))

                progress.progress(i / total)

            if all_pages:
                crawl_results = {
                    "pages": all_pages,
                    "structured_data": all_structured,
                    "links": {
                        "internal": sorted(all_internal),
                        "external": sorted(all_external),
                    },
                }
                st.session_state["crawl_results"] = crawl_results
                df = pd.DataFrame(all_pages)
                st.session_state["df"] = df
                status_placeholder.success("✅ URL list crawl complete.")
            else:
                status_placeholder.error("No data returned for any URL. Crawl failed.")

# Ensure df is always created if crawl_results exists
if "crawl_results" in st.session_state and "df" not in st.session_state:
    st.session_state["df"] = pd.DataFrame(
        st.session_state["crawl_results"].get("pages", [])
    )

# ==========================================
# MAIN TABS
# ==========================================
tabs = st.tabs(
    [
        "Pages",
        "SEO Data",
        "Structured Data",
        "Links",
        "Core Web Vitals",
    ]
)
pages_tab, seo_tab, sd_tab, links_tab, cwv_tab = tabs

# ==========================================
# TAB: PAGES
# ==========================================
with pages_tab:
    st.header("Crawled Pages Overview")

    if "df" in st.session_state and not st.session_state["df"].empty:
        df = st.session_state["df"]
        st.dataframe(df, use_container_width=True)
        st.caption(
            "🟡 **Diff score** shows how much the rendered HTML differs from the raw HTML. "
            "0% = identical, 100% = completely different."
        )
    else:
        st.info("Run a crawl to view results.")

# ==========================================
# TAB: SEO DATA
# ==========================================
with seo_tab:
    st.header("SEO Signals & Indexability")

    if "df" in st.session_state and not st.session_state["df"].empty:
        df = st.session_state["df"]

        main_cols = [
            "url",
            "status",
            "indexable",
            "canonical",
            "robots",
            "diff_score",
            "error_type",
        ]
        cols = [c for c in main_cols if c in df.columns]

        st.subheader("SEO overview")
        st.dataframe(df[cols], use_container_width=True)

        st.subheader("Non-indexable pages (fix first)")
        if "indexable" in df.columns:
            non_indexable = df[df["indexable"] == False]
            if not non_indexable.empty:
                st.dataframe(
                    non_indexable[
                        [
                            c
                            for c in ["url", "status", "robots", "error_type"]
                            if c in non_indexable.columns
                        ]
                    ],
                    use_container_width=True,
                )
            else:
                st.info("All crawled pages appear indexable.")
        else:
            st.info("No indexability data available.")

        st.subheader("Crawl errors by type")
        if "error_type" in df.columns:
            err_counts = df["error_type"].value_counts(dropna=True)
            if not err_counts.empty:
                st.table(err_counts.rename("count"))
            else:
                st.info("No crawl errors recorded.")
        else:
            st.info("No error information available.")
    else:
        st.info("Run a crawl to display SEO data.")

# ==========================================
# TAB: STRUCTURED DATA
# ==========================================
with sd_tab:
    st.header("Structured Data (JSON-LD)")

    if "crawl_results" in st.session_state:
        structured = st.session_state["crawl_results"].get("structured_data", [])
        df_pages = st.session_state.get("df", pd.DataFrame())

        if structured:
            sddf = pd.DataFrame(structured)
            st.subheader("Detected structured data objects")
            st.dataframe(sddf, use_container_width=True)

            st.subheader("Invalid / incomplete structured data")
            if "valid" in sddf.columns:
                invalid = sddf[sddf["valid"] == False]
                if not invalid.empty:
                    st.dataframe(invalid, use_container_width=True)
                else:
                    st.info("All detected structured data objects have required properties.")
            else:
                st.info("No validation info available.")

            if (
                not df_pages.empty
                and "url" in df_pages.columns
                and "url" in sddf.columns
            ):
                st.subheader("Pages with no structured data")
                pages_with_sd = set(sddf["url"])
                no_sd = df_pages[~df_pages["url"].isin(pages_with_sd)]
                if not no_sd.empty:
                    st.dataframe(no_sd[["url"]], use_container_width=True)
                else:
                    st.info("All crawled pages have at least one structured data object.")
        else:
            st.warning("No structured data detected on any crawled page.")
    else:
        st.info("Run a crawl first to analyze structured data.")

# ==========================================
# TAB: LINKS
# ==========================================
with links_tab:
    st.header("Links Overview (Internal / External)")

    if "crawl_results" in st.session_state:
        links_data = st.session_state["crawl_results"].get("links", {})
        internal = links_data.get("internal", []) or []
        external = links_data.get("external", []) or []

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Total Internal Links", len(internal))
        with col2:
            st.metric("Total External Links", len(external))

        col3, col4 = st.columns(2)
        with col3:
            st.subheader("Sample internal links")
            st.write(internal[:50] if internal else "No internal links found.")
        with col4:
            st.subheader("Sample external links")
            st.write(external[:50] if external else "No external links found.")
    else:
        st.info("Run a crawl to view link data.")

# ==========================================
# TAB: CORE WEB VITALS
# ==========================================
with cwv_tab:
    st.header("Core Web Vitals (from crawl results)")

    if "df" in st.session_state and not st.session_state["df"].empty:
        df = st.session_state["df"]
        if all(col in df.columns for col in ["lcp_ms", "cls", "inp_ms"]):
            st.subheader("Per-URL CWV metrics")
            st.caption(
                "ℹ️ CWV values are fetched from the PageSpeed Insights API on a per-URL basis. "
                "In domain crawls, only the first few pages receive CWV data to keep the demo fast."
            )
            st.dataframe(df[["url", "lcp_ms", "cls", "inp_ms"]], use_container_width=True)

            st.subheader("Worst LCP (slowest pages)")
            worst_lcp = df.sort_values("lcp_ms", ascending=False).head(10)
            st.dataframe(worst_lcp[["url", "lcp_ms"]], use_container_width=True)

            st.subheader("Worst CLS (layout shifts)")
            worst_cls = df.sort_values("cls", ascending=False).head(10)
            st.dataframe(worst_cls[["url", "cls"]], use_container_width=True)
        else:
            st.info("CWV metrics are not present in the current crawl results.")
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
