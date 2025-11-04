# api/crawl.py
import asyncio, json, base64, hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import List, Dict, Any, Optional

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
    host = p.hostname.lower()
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
# Crawler service (browser reused across requests)
# -----------------------------------------
class CrawlerService:
    def __init__(self, concurrency: int = 3):
        self.concurrency = concurrency
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._browser = None
        self._worker_tasks: List[asyncio.Task] = []
        self._client = httpx.AsyncClient(timeout=15)

    async def start(self):
        pw = await async_playwright().start()
        # Headless chromium
        self._browser = await pw.chromium.launch(headless=True)
        # Workers only needed if you use the queue-based API
        for _ in range(self.concurrency):
            t = asyncio.create_task(self._worker())
            self._worker_tasks.append(t)

    async def stop(self):
        for t in self._worker_tasks:
            t.cancel()
        if self._browser:
            await self._browser.close()
        await self._client.aclose()

    async def enqueue(self, url: str, options: Dict[str, Any]):
        """Queue-based API (used only if you stick with job-id model)."""
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
        """Optional helper if you keep job-based endpoints."""
        return self._tasks.get(job_id, {"status": "unknown"})

    async def get_results(self, job_id: str) -> Dict[str, Any]:
        """Optional helper if you keep job-based endpoints."""
        task = self._tasks.get(job_id)
        if not task:
            return {"status": "unknown"}
        return task

    async def get_cwv(self, url: str, strategy: str = "mobile") -> Dict[str, Any]:
        """CWV hook called from _crawl_single or /cwv; stub for now."""
        return await dummy_cwv(url)


    # -----------------------------------------
    # HTTP + rendering
    # -----------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    async def _fetch_raw(self, url: str):
        r = await self._client.get(url)
        return r.status_code, r.text, r.headers

    async def _render(self, url: str, wait_until: str = "networkidle", timeout: int = 20000):
        ctx = await self._browser.new_context(viewport={"width": 1366, "height": 768})
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
    async def _crawl_single(self, url: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        norm = normalize_url(url)
        # robots check — naive: fetch robots.txt (not implemented yet)
        status, raw_html, headers = await self._fetch_raw(norm)
        rendered_html, screenshot = await self._render(norm)

        diff_score = compute_diff_score(raw_html, rendered_html)
        jsonld = extract_jsonld(rendered_html)
        schema_validations = validate_jsonld(jsonld)
        psi = await self.get_cwv(norm)   # stubbed for now

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
        }


# -----------------------------------------
# Helper functions for diffs & structured data
# -----------------------------------------
def compute_diff_score(raw_html: str, rendered_html: str) -> float:
    """
    Compute a simple text-based diff score between raw and rendered HTML.
    Returns a value between 0 and 1 (fraction of difference).
    """
    raw_text = " ".join(BeautifulSoup(raw_html, "lxml").get_text().split())
    ren_text = " ".join(BeautifulSoup(rendered_html, "lxml").get_text().split())
    if not raw_text and not ren_text:
        return 0.0
    ratio = SequenceMatcher(None, raw_text, ren_text).ratio()
    return 1.0 - ratio


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
    Minimal validation stub: mark each JSON-LD object as valid,
    but structure can be extended later with jsonschema-based checks.
    """
    results = []
    for obj in jsonld_blocks:
        t = obj.get("@type", "Unknown")
        results.append(
            {
                "type": t,
                "valid": True,
                "missing_props": [],  # fill with required properties later
            }
        )
    return results


async def dummy_cwv(url: str) -> Dict[str, Any]:
    """
    Temporary CWV stub.
    Replace with a real PSI / CrUX integration later.
    """
    return {
        "url": url,
        "lcp_ms": 2500,
        "cls": 0.1,
        "inp_ms": 150,
    }


# -----------------------------------------
# Immediate crawl helper (Option A)
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

    pages = []
    structured_data = []
    all_internal_links = set()
    all_external_links = set()

    # For simplicity, crawl sequentially; you can parallelize later
    for u in urls:
        single = await service._crawl_single(u, options)

        # Page summary row for the Pages / SEO tabs
        pages.append(
            {
                "url": single["url"],
                "status": single["status"],
                "title": None,              # extend by parsing <title> if desired
                "meta_description": None,   # extend by parsing <meta name='description'>
                "canonical": single["url"], # extend with real canonical extraction
                "robots": "index,follow",   # extend with real robots logic
                "hreflang": None,
                "diff_score": single["diff_score"],
            }
        )

        # Structured data summary
        for block, validation in zip(single["jsonld"], single["schema_validations"]):
            structured_data.append(
                {
                    "url": single["url"],
                    "type": block.get("@type", "Unknown"),
                    "valid": validation.get("valid", True),
                    "missing_props": validation.get("missing_props", []),
                }
            )

        # TODO: real internal/external link extraction can go here.
        # For now we keep these sets empty or fill with stubs if you like.

    return {
        "pages": pages,
        "structured_data": structured_data,
        "links": {
            "internal": sorted(all_internal_links),
            "external": sorted(all_external_links),
        },
    }