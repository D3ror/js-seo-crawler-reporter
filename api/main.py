# api/main.py
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from api.crawl import CrawlerService, crawl_urls_immediate

app = FastAPI(title="JS SEO Crawler API")
crawler: Optional[CrawlerService] = None


class CrawlRequest(BaseModel):
    """Request body for /crawl, matching what the Streamlit UI sends."""
    urls: List[str]
    depth: int = 1
    concurrency: int = 3
    delay: float = 0.5
    headless: bool = True


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
    - Accepts a list of URLs
    - Returns pages + structured_data + links in one response
    """
    if not crawler:
        raise HTTPException(status_code=500, detail="Crawler not initialized")

    if not request.urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    try:
        result = await crawl_urls_immediate(crawler, request.urls)
        return result
    except Exception as e:
        # Log to stdout so you can see it in `fly logs`
        print("Error in /crawl:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cwv")
async def get_cwv(url: str, strategy: str = "mobile"):
    """
    CWV endpoint used by the Streamlit UI.
    Currently calls the stubbed CrawlerService.get_cwv.
    """
    if not crawler:
        raise HTTPException(status_code=500, detail="Crawler not initialized")
    try:
        return await crawler.get_cwv(url, strategy=strategy)
    except Exception as e:
        print("Error in /cwv:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


# The status/results endpoints are not used by the Streamlit app in Option A,
# but you can keep or remove them depending on whether you want a job-based API later.

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
