# api/crawl.py
import asyncio, json, base64, hashlib, os
from urllib.parse import (
    urlparse,
    urlunparse,
    parse_qsl,
    urlencode,
    urljoin,
)
from typing import List, Dict, Any, Optional, Set, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from difflib import SequenceMatcher


# -----------------------------------------
# URL normalization
# -----------------------------------------
def normalize_url(url: str) -> str:
    p = urlparse(url)
    scheme = p.scheme or "https"
    host = (p.hostname or "").lower()
    path = p.path or "/"
    qs = [
        (k, v)
        for k, v in parse_qsl(p.query)
        if not k.startswith("utm_") and k not in ("fbclid", "gclid")
    ]
    qs_sorted = sorted(qs)
    query = urlencode(qs_sorted)
    return urlunparse((scheme, host, path, "", query, ""))


# -----------------------------------------
# Simple schema.org "required fields" map
# -----------------------------------------
SCHEMA_REQUIRED: Dict[str, List[str]] = {
    "Article": ["headline"],
    "NewsArticle": ["headline"],
    "BlogPosting": ["headline"],
    "Product": ["name", "offers"],
    "BreadcrumbList": ["itemListElement"],
    "FAQPage": ["mainEntity"],
}

# -----------------------------------------
# PSI API key (for Core Web Vitals)
# -----------------------------------------
PSI_API_KEY = os.getenv("PSI_API_KEY")


# -----------------------------------------
# Crawler service (browser reused across requests)
# -----------------------------------------
class CrawlerService:
    def __init__(self, concurrency: int = 3):
        self.concurrency = concurrency
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._browser = None
        self._worker_tasks: List[asyncio.Task] = []
        self._client = httpx.AsyncClient(timeout=30)

    async def start(self):
        pw = await async_playwright().start()
        # Headless chromium
        self._browser = await pw.chromium.launch(headless=True)
        # Workers are only needed if you keep the job-queue API
        for _ in range(self.concurrency):
            t = asyncio.create_task(self._worker())
            self._worker_tasks.append(t)

    async def stop(self):
        for t in self._worker_tasks:
            t.cancel()
        if self._browser:
            await self._browser.close()
        await self._client.aclose()

    # -------- job-based helpers (kept for future use) --------
    async def enqueue(self, url: str, options: Dict[str, Any]):
        job_id = hashlib.sha1(url.encode()).hexdigest()[:10]
        await self._queue.put((job_id, url, options))
        self._tasks[job_id] = {"status": "queued"}
        return job_id

    async def _worker(self):
        while True:
            job_id, url, options = await self._queue.get()
            self._tasks[job_id]["status"] = "running"
            try:
                result = await self._crawl_single(url, options)
                self._tasks[job_id]["status"] = "done"
                self._tasks[job_id]["result"] = result
            except Exception as e:
                self._tasks[job_id]["status"] = "error"
                self._tasks[job_id]["error"] = str(e)
            finally:
                self._queue.task_done()

    async def get_status(self, job_id: str) -> Dict[str, Any]:
        return self._tasks.get(job_id, {"status": "unknown"})

    async def get_results(self, job_id: str) -> Dict[str, Any]:
        task = self._tasks.get(job_id)
        if not task:
            return {"status": "unknown"}
        return task

    async def get_cwv(self, url: str, strategy: str = "mobile") -> Dict[str, Any]:
        """
        Fetch Core Web Vitals using the PageSpeed Insights API.

        If PSI_API_KEY is not set or the request fails, fall back to dummy_cwv.
        """
        if not PSI_API_KEY:
            # No key configured – fall back to stub
            return await dummy_cwv(url)

        api = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        params = {
            "url": url,
            "category": "performance",
            "strategy": strategy,  # "mobile" or "desktop"
            "key": PSI_API_KEY,
        }

        try:
            r = await self._client.get(api, params=params)
            r.raise_for_status()
            data = r.json()

            # Try to pull CWV from loadingExperience metrics (field data)
            metrics = data.get("loadingExperience", {}).get("metrics", {})

            lcp = metrics.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile")
            cls = metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile")
            inp = metrics.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile")

            return {
                "url": url,
                "lcp_ms": lcp if lcp is not None else 2500,
                "cls": cls if cls is not None else 0.1,
                "inp_ms": inp if inp is not None else 150,
            }

        except Exception as e:
            # Log and fall back to dummy CWV
            print("PSI API error for URL", url, ":", repr(e))
            return await dummy_cwv(url)

    # -----------------------------------------
    # HTTP + rendering
    # -----------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def _fetch_raw(self, url: str, user_agent: Optional[str] = None):
        headers: Dict[str, str] = {}
        if user_agent:
            headers["User-Agent"] = user_agent
        r = await self._client.get(url, headers=headers)
        return r.status_code, r.text, r.headers

    async def _render(
        self,
        url: str,
        wait_until: str = "networkidle",
        timeout: int = 20000,
        user_agent: Optional[str] = None,
    ):
        ctx_kwargs: Dict[str, Any] = {
            "viewport": {"width": 1366, "height": 768},
        }
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent

        ctx = await self._browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        content = await page.content()
        screenshot = await page.screenshot(full_page=True)
        await page.close()
        await ctx.close()
        return content, screenshot

    # -----------------------------------------
    # Single-page crawl
    # -----------------------------------------
    async def _crawl_single(
        self,
        url: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        options = options or {}

        norm = normalize_url(url)
        parsed = urlparse(norm)
        base_host = parsed.hostname or ""

        # Emulate Googlebot if requested
        ua = None
        if options.get("emulate_googlebot"):
            ua = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

        # robots.txt checks are TODO; we just fetch page for now
        status, raw_html, headers = await self._fetch_raw(norm, user_agent=ua)
        rendered_html, screenshot = await self._render(norm, user_agent=ua)

        # --- Text diff between raw & rendered ---
        diff_score = compute_diff_score(raw_html, rendered_html)

        # --- Parse rendered DOM ---
        soup = BeautifulSoup(rendered_html, "lxml")

        # Title
        title_tag = soup.title.string.strip() if soup.title and soup.title.string else None

        # Meta description
        meta_desc = None
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            meta_desc = md["content"].strip()

        # Canonical
        canonical_tag = None
        canon = soup.find("link", rel=lambda v: v and "canonical" in v.lower())
        if canon and canon.get("href"):
            canonical_tag = canon["href"].strip()

        # Meta robots
        meta_robots = None
        mr = soup.find("meta", attrs={"name": "robots"})
        if mr and mr.get("content"):
            meta_robots = mr["content"].strip().lower()

        # X-Robots-Tag (from headers)
        x_robots = headers.get("x-robots-tag") or headers.get("X-Robots-Tag")

        # Hreflang
        hreflangs: List[Dict[str, str]] = []
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v.lower()):
            lang = link.get("hreflang")
            href = link.get("href")
            if lang and href:
                hreflangs.append({"lang": lang, "href": href})

        # Links
        internal_links: Set[str] = set()
        external_links: Set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            abs_url = urljoin(norm, href)
            parsed_link = urlparse(abs_url)
            if not parsed_link.scheme.startswith("http"):
                continue
            if (parsed_link.hostname or "").lower() == base_host.lower():
                internal_links.add(normalize_url(abs_url))
            else:
                external_links.add(normalize_url(abs_url))

        # Structured data
        jsonld = extract_jsonld(rendered_html)
        schema_validations = validate_jsonld(jsonld)

        # CWV (now page-level via PSI where possible)
        psi = await self.get_cwv(norm)

        # Indexability heuristic
        indexable_flag = is_indexable(status, meta_robots, x_robots)

        # build detailed single-page result
        return {
            "url": norm,
            "status": status,
            "headers": dict(headers),
            "diff_score": diff_score,
            "jsonld": jsonld,
            "schema_validations": schema_validations,
            "psi": psi,
            "screenshot": base64.b64encode(screenshot).decode("ascii"),
            "title": title_tag,
            "meta_description": meta_desc,
            "canonical": canonical_tag or norm,
            "meta_robots": meta_robots,
            "x_robots": x_robots,
            "hreflangs": hreflangs,
            "internal_links": list(internal_links),
            "external_links": list(external_links),
            "indexable": indexable_flag,
        }


# -----------------------------------------
# Helper functions for diffs & structured data
# -----------------------------------------
def compute_diff_score(raw_html: str, rendered_html: str) -> float:
    """
    Compute a simple text-based diff score between raw and rendered HTML.

    Returns:
        float: Percentage difference between 0 and 100.
               0.0  = identical
               100.0 = completely different
    """
    raw_text = " ".join(BeautifulSoup(raw_html, "lxml").get_text().split())
    ren_text = " ".join(BeautifulSoup(rendered_html, "lxml").get_text().split())
    if not raw_text and not ren_text:
        return 0.0
    ratio = SequenceMatcher(None, raw_text, ren_text).ratio()
    # convert fraction of difference to percentage
    return round((1.0 - ratio) * 100.0, 2)


def extract_jsonld(html: str):
    """Extract JSON-LD blocks from the rendered HTML."""
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
            if isinstance(data, list):
                blocks.extend(data)
            else:
                blocks.append(data)
        except Exception:
            # ignore malformed JSON-LD
            continue
    return blocks


def validate_jsonld(jsonld_blocks):
    """
    Minimal validation:
    - Check a few required properties for common schema.org types.
    """
    results = []
    for obj in jsonld_blocks:
        t = obj.get("@type", "Unknown")
        required = SCHEMA_REQUIRED.get(t, [])
        missing = [prop for prop in required if prop not in obj]
        results.append(
            {
                "type": t,
                "valid": len(missing) == 0,
                "missing_props": missing,
            }
        )
    return results


def is_indexable(status: int, meta_robots: Optional[str], x_robots: Optional[str]) -> bool:
    """
    Very rough indexability heuristic:
    - Non-4xx/5xx
    - No 'noindex' in meta robots or X-Robots-Tag
    """
    if status >= 400:
        return False
    txt = " ".join(filter(None, [meta_robots or "", x_robots or ""])).lower()
    if "noindex" in txt:
        return False
    return True


async def dummy_cwv(url: str) -> Dict[str, Any]:
    """
    Temporary CWV stub.
    Used when PSI_API_KEY is not configured or PSI call fails.
    """
    return {
        "url": url,
        "lcp_ms": 2500,
        "cls": 0.1,
        "inp_ms": 150,
    }


# -----------------------------------------
# Immediate crawl helper (URL list mode)
# -----------------------------------------
async def crawl_urls_immediate(
    service: CrawlerService,
    urls: List[str],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Crawl a list of URLs *immediately* (no job queue),
    and return results in the shape expected by the Streamlit UI:
    {
      "pages": [...],
      "structured_data": [...],
      "links": {"internal": [...], "external": [...]}
    }
    """
    if options is None:
        options = {}

    pages: List[Dict[str, Any]] = []
    structured_data: List[Dict[str, Any]] = []
    all_internal_links: Set[str] = set()
    all_external_links: Set[str] = set()

    for u in urls:
        single = await service._crawl_single(u, options)

        psi = single.get("psi", {})

        pages.append(
            {
                "url": single["url"],
                "status": single["status"],
                "title": single.get("title"),
                "meta_description": single.get("meta_description"),
                "canonical": single.get("canonical"),
                "robots": single.get("meta_robots") or single.get("x_robots"),
                "hreflang": ", ".join([h["lang"] for h in single.get("hreflangs", [])]) or None,
                "diff_score": single.get("diff_score"),
                "indexable": single.get("indexable"),
                "lcp_ms": psi.get("lcp_ms"),
                "cls": psi.get("cls"),
                "inp_ms": psi.get("inp_ms"),
            }
        )

        for block, validation in zip(single["jsonld"], single["schema_validations"]):
            structured_data.append(
                {
                    "url": single["url"],
                    "type": block.get("@type", "Unknown"),
                    "valid": validation.get("valid", True),
                    "missing_props": validation.get("missing_props", []),
                }
            )

        all_internal_links.update(single.get("internal_links", []))
        all_external_links.update(single.get("external_links", []))

    return {
        "pages": pages,
        "structured_data": structured_data,
        "links": {
            "internal": sorted(all_internal_links),
            "external": sorted(all_external_links),
        },
    }


# -----------------------------------------
# Domain crawl helper (start URL + internal links)
# -----------------------------------------
async def crawl_domain_immediate(
    service: CrawlerService,
    start_url: str,
    max_pages: int = 50,
    max_depth: int = 2,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Crawl a domain starting from start_url, following internal links
    up to max_pages and max_depth (BFS), and return the same structure
    as crawl_urls_immediate.
    """
    if options is None:
        options = {}

    start_norm = normalize_url(start_url)
    parsed = urlparse(start_norm)
    base_host = (parsed.hostname or "").lower()

    visited: Set[str] = set()
    queue: asyncio.Queue[Tuple[str, int]] = asyncio.Queue()
    await queue.put((start_norm, 0))

    pages: List[Dict[str, Any]] = []
    structured_data: List[Dict[str, Any]] = []
    all_internal_links: Set[str] = set()
    all_external_links: Set[str] = set()

    while not queue.empty() and len(visited) < max_pages:
        current_url, depth = await queue.get()
        if current_url in visited:
            continue
        visited.add(current_url)

        try:
            single = await service._crawl_single(current_url, options)
        except Exception:
            continue  # skip failures

        psi = single.get("psi", {})

        pages.append(
            {
                "url": single["url"],
                "status": single["status"],
                "title": single.get("title"),
                "meta_description": single.get("meta_description"),
                "canonical": single.get("canonical"),
                "robots": single.get("meta_robots") or single.get("x_robots"),
                "hreflang": ", ".join([h["lang"] for h in single.get("hreflangs", [])]) or None,
                "diff_score": single.get("diff_score"),
                "indexable": single.get("indexable"),
                "lcp_ms": psi.get("lcp_ms"),
                "cls": psi.get("cls"),
                "inp_ms": psi.get("inp_ms"),
            }
        )

        for block, validation in zip(single["jsonld"], single["schema_validations"]):
            structured_data.append(
                {
                    "url": single["url"],
                    "type": block.get("@type", "Unknown"),
                    "valid": validation.get("valid", True),
                    "missing_props": validation.get("missing_props", []),
                }
            )

        internal_links = single.get("internal_links", [])
        external_links = single.get("external_links", [])
        all_internal_links.update(internal_links)
        all_external_links.update(external_links)

        # enqueue internal links for BFS
        if depth + 1 <= max_depth:
            for link in internal_links:
                parsed_link = urlparse(link)
                if (parsed_link.hostname or "").lower() == base_host and link not in visited:
                    await queue.put((link, depth + 1))

    return {
        "pages": pages,
        "structured_data": structured_data,
        "links": {
            "internal": sorted(all_internal_links),
            "external": sorted(all_external_links),
        },
    }
