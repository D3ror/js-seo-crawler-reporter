# api/main.py
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from api.crawl import (
    CrawlerService,
    crawl_urls_immediate,
    crawl_domain_immediate,
)

app = FastAPI(title="JS SEO Crawler API")
crawler: Optional[CrawlerService] = None

# Demo caps (server-side enforcement)
MAX_URLS_LIST_MODE = 10
MAX_PAGES_DOMAIN_MODE = 10
MAX_DEPTH_DOMAIN_MODE = 3


class CrawlRequest(BaseModel):
    """
    Request body for /crawl, matching what the Streamlit UI sends.

    mode:
      - "list"   → treat `urls` as an explicit list to crawl
      - "domain" → treat `urls[0]` as seed URL and crawl internal links
    """
    urls: List[str]
    mode: str = "list"          # "list" or "domain"
    max_pages: int = 50         # used in domain mode
    max_depth: int = 2          # used in domain mode

    depth: int = 1              # reserved for future use
    concurrency: int = 3        # reserved for future use
    delay: float = 0.5          # reserved for future use
    headless: bool = True       # reserved for future use

    emulate_googlebot: bool = False  # if True, use Googlebot UA


@app.on_event("startup")
async def startup():
    global crawler
    crawler = CrawlerService(concurrency=3)   # reuse browser
    await crawler.start()


@app.on_event("shutdown")
async def shutdown():
    if crawler:
        await crawler.stop()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/crawl")
async def crawl_url(request: CrawlRequest):
    """
    Immediate crawl endpoint (no job queue):

    - In `mode="list"`:
        Accepts a list of URLs and returns pages + structured_data + links.
    - In `mode="domain"`:
        Uses the first URL as seed and crawls internal links up to
        `max_pages` and `max_depth`.
    """
    if not crawler:
        raise HTTPException(status_code=500, detail="Crawler not initialized")

    if not request.urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    # Hard safety caps for demo stability
    mode = (request.mode or "list").lower().strip()
    urls = request.urls

    if mode == "domain":
        # Only the first URL is used as seed; cap pages/depth
        urls = [urls[0]]
        max_pages = max(1, min(request.max_pages, MAX_PAGES_DOMAIN_MODE))
        max_depth = max(1, min(request.max_depth, MAX_DEPTH_DOMAIN_MODE))
    else:
        # List mode: cap to 10 URLs
        mode = "list"
        urls = urls[:MAX_URLS_LIST_MODE]
        max_pages = None
        max_depth = None

    options = {
        "emulate_googlebot": request.emulate_googlebot,
        "mode": mode,
    }

    try:
        if mode == "domain":
            result = await crawl_domain_immediate(
                crawler,
                start_url=urls[0],
                max_pages=max_pages,
                max_depth=max_depth,
                options=options,
            )
        else:
            result = await crawl_urls_immediate(
                crawler,
                urls,
                options=options,
            )

        return result

    except Exception as e:
        print("Error in /crawl:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cwv")
async def get_cwv(url: str, strategy: str = "mobile"):
    if not crawler:
        raise HTTPException(status_code=500, detail="Crawler not initialized")
    try:
        return await crawler.get_cwv(url, strategy=strategy)
    except Exception as e:
        print("Error in /cwv:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if not crawler:
        raise HTTPException(status_code=500, detail="Crawler not initialized")
    return await crawler.get_status(job_id)


@app.get("/results/{job_id}")
async def get_results(job_id: str):
    if not crawler:
        raise HTTPException(status_code=500, detail="Crawler not initialized")
    return await crawler.get_results(job_id)
