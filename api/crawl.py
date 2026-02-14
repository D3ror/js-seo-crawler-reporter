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
        self._browser = await pw.chromium.launch(headless=True)
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
        If PSI_API_KEY is not set or PSI fails, fall back to dummy_cwv.
        """
        if not PSI_API_KEY:
            return await dummy_cwv(url)

        api = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        params = {
            "url": url,
            "category": "performance",
            "strategy": strategy,
            "key": PSI_API_KEY,
        }

        try:
            r = await self._client.get(api, params=params)
            r.raise_for_status()
            data = r.json()

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
        wait_until: str = "domcontentloaded",
        timeout: int = 30000,  # ↑ increase nav timeout
        user_agent: Optional[str] = None,
        block_resources: bool = True,
        full_page_screenshot: bool = False,
    ):
        """
        Render a page with Playwright and return (html, screenshot_bytes).

        Performance features:
        - optional resource blocking (images/fonts/media) to reduce timeouts
        - optional full-page screenshot (off by default to speed up)
        """
        ctx_kwargs: Dict[str, Any] = {
            "viewport": {"width": 1366, "height": 768},
        }
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent

        ctx = await self._browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        if block_resources:
            async def route_handler(route):
                try:
                    rt = route.request.resource_type
                    if rt in ("image", "media", "font"):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    # If something goes wrong, don't block the crawl
                    await route.continue_()

            await page.route("**/*", route_handler)

        await page.goto(url, wait_until=wait_until, timeout=timeout)
        content = await page.content()

        # Screenshot last (avoid wasting time if navigation fails)
        screenshot = await page.screenshot(full_page=full_page_screenshot)

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

        # options
        emulate_googlebot = bool(options.get("emulate_googlebot"))
        full_page_screenshot = bool(options.get("full_page_screenshot", False))
        block_resources = bool(options.get("block_resources", True))

        ua = None
        if emulate_googlebot:
            ua = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

        try:
            status, raw_html, headers = await self._fetch_raw(norm, user_agent=ua)

            # ---- Render with retry strategy ----
            render_ok = False
            render_attempts = 0
            render_error = None

            rendered_html = ""
            screenshot = b""

            # attempt 1: domcontentloaded
            # attempt 2 (if timeout): commit (faster fallback)
            nav_strategies = [("domcontentloaded", 30000), ("commit", 30000)]

            for wait_until, goto_timeout in nav_strategies:
                render_attempts += 1
                try:
                    rendered_html, screenshot = await asyncio.wait_for(
                        self._render(
                            norm,
                            wait_until=wait_until,
                            timeout=goto_timeout,
                            user_agent=ua,
                            block_resources=block_resources,
                            full_page_screenshot=full_page_screenshot,
                        ),
                        timeout=40,  # ↑ overall cap (must exceed goto timeout)
                    )
                    render_ok = True
                    render_error = None
                    break
                except Exception as e:
                    render_error = str(e)

            if not render_ok:
                # Give a meaningful timeout classification
                return {
                    "url": norm,
                    "status": status,
                    "error_type": "render_timeout",
                    "error_message": render_error or "Render failed (unknown)",
                    "render_ok": False,
                    "render_attempts": render_attempts,
                    "render_error": render_error,
                }

            # --- Text diff between raw & rendered ---
            diff_score = compute_diff_score(raw_html, rendered_html)

            # --- Parse rendered DOM ---
            soup = BeautifulSoup(rendered_html, "lxml")

            title_tag = soup.title.string.strip() if soup.title and soup.title.string else None

            meta_desc = None
            md = soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                meta_desc = md["content"].strip()

            canonical_tag = None
            canon = soup.find("link", rel=lambda v: v and "canonical" in v.lower())
            if canon and canon.get("href"):
                canonical_tag = canon["href"].strip()

            meta_robots = None
            mr = soup.find("meta", attrs={"name": "robots"})
            if mr and mr.get("content"):
                meta_robots = mr["content"].strip().lower()

            x_robots = headers.get("x-robots-tag") or headers.get("X-Robots-Tag")

            hreflangs: List[Dict[str, str]] = []
            for link in soup.find_all("link", rel=lambda v: v and "alternate" in v.lower()):
                lang = link.get("hreflang")
                href = link.get("href")
                if lang and href:
                    hreflangs.append({"lang": lang, "href": href})

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

            jsonld = extract_jsonld(rendered_html)
            schema_validations = validate_jsonld(jsonld)

            indexable_flag = is_indexable(status, meta_robots, x_robots)

            return {
                "url": norm,
                "status": status,
                "headers": dict(headers),
                "diff_score": diff_score,
                "jsonld": jsonld,
                "schema_validations": schema_validations,
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
                "render_ok": True,
                "render_attempts": render_attempts,
                "render_error": None,
            }

        except httpx.TimeoutException as e:
            return {
                "url": norm,
                "status": 0,
                "error_type": "http_timeout",
                "error_message": str(e),
            }
        except httpx.RequestError as e:
            return {
                "url": norm,
                "status": 0,
                "error_type": "http_error",
                "error_message": str(e),
            }
        except Exception as e:
            return {
                "url": norm,
                "status": 0,
                "error_type": "crawler_internal_error",
                "error_message": str(e),
            }


# -----------------------------------------
# Helper functions for diffs & structured data
# -----------------------------------------
def compute_diff_score(raw_html: str, rendered_html: str) -> float:
    raw_text = " ".join(BeautifulSoup(raw_html, "lxml").get_text().split())
    ren_text = " ".join(BeautifulSoup(rendered_html, "lxml").get_text().split())
    if not raw_text and not ren_text:
        return 0.0
    ratio = SequenceMatcher(None, raw_text, ren_text).ratio()
    return round((1.0 - ratio) * 100.0, 2)


def extract_jsonld(html: str):
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
            continue
    return blocks


def validate_jsonld(jsonld_blocks):
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
    if status >= 400:
        return False
    txt = " ".join(filter(None, [meta_robots or "", x_robots or ""])).lower()
    if "noindex" in txt:
        return False
    return True


async def dummy_cwv(url: str) -> Dict[str, Any]:
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
    if options is None:
        options = {}

    pages: List[Dict[str, Any]] = []
    structured_data: List[Dict[str, Any]] = []
    all_internal_links: Set[str] = set()
    all_external_links: Set[str] = set()

    for u in urls:
        single = await service._crawl_single(u, options)

        error_type = single.get("error_type")
        psi: Dict[str, Any] = {}

        if not error_type and single.get("url"):
            psi = await service.get_cwv(single["url"])

        pages.append(
            {
                "url": single.get("url", u),
                "status": single.get("status"),
                "title": single.get("title"),
                "meta_description": single.get("meta_description"),
                "canonical": single.get("canonical"),
                "robots": single.get("meta_robots") or single.get("x_robots"),
                "hreflang": ", ".join([h["lang"] for h in single.get("hreflangs", [])]) or None,
                "diff_score": single.get("diff_score"),
                "indexable": single.get("indexable"),
                "error_type": error_type,
                "error_message": single.get("error_message"),
                "render_ok": single.get("render_ok"),
                "render_attempts": single.get("render_attempts"),
                "render_error": single.get("render_error"),
                "lcp_ms": psi.get("lcp_ms"),
                "cls": psi.get("cls"),
                "inp_ms": psi.get("inp_ms"),
            }
        )

        for block, validation in zip(single.get("jsonld", []), single.get("schema_validations", [])):
            structured_data.append(
                {
                    "url": single.get("url", u),
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
        "links": {"internal": sorted(all_internal_links), "external": sorted(all_external_links)},
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

    MAX_PSI_PAGES = 5

    while not queue.empty() and len(visited) < max_pages:
        current_url, depth = await queue.get()
        if current_url in visited:
            continue
        visited.add(current_url)

        single = await service._crawl_single(current_url, options)

        error_type = single.get("error_type")
        psi: Dict[str, Any] = {}

        if not error_type and len(pages) < MAX_PSI_PAGES:
            psi = await service.get_cwv(single.get("url", current_url))

        pages.append(
            {
                "url": single.get("url", current_url),
                "status": single.get("status"),
                "title": single.get("title"),
                "meta_description": single.get("meta_description"),
                "canonical": single.get("canonical"),
                "robots": single.get("meta_robots") or single.get("x_robots"),
                "hreflang": ", ".join([h["lang"] for h in single.get("hreflangs", [])]) or None,
                "diff_score": single.get("diff_score"),
                "indexable": single.get("indexable"),
                "error_type": error_type,
                "error_message": single.get("error_message"),
                "render_ok": single.get("render_ok"),
                "render_attempts": single.get("render_attempts"),
                "render_error": single.get("render_error"),
                "lcp_ms": psi.get("lcp_ms"),
                "cls": psi.get("cls"),
                "inp_ms": psi.get("inp_ms"),
            }
        )

        for block, validation in zip(single.get("jsonld", []), single.get("schema_validations", [])):
            structured_data.append(
                {
                    "url": single.get("url", current_url),
                    "type": block.get("@type", "Unknown"),
                    "valid": validation.get("valid", True),
                    "missing_props": validation.get("missing_props", []),
                }
            )

        internal_links = single.get("internal_links", [])
        external_links = single.get("external_links", [])
        all_internal_links.update(internal_links)
        all_external_links.update(external_links)

        if depth + 1 <= max_depth:
            for link in internal_links:
                parsed_link = urlparse(link)
                if (parsed_link.hostname or "").lower() == base_host and link not in visited:
                    await queue.put((link, depth + 1))

    return {
        "pages": pages,
        "structured_data": structured_data,
        "links": {"internal": sorted(all_internal_links), "external": sorted(all_external_links)},
    }
