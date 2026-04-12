"""
Web Tools — SearXNG search + Playwright fallback scraper.
Results are injected into the Memory Palace web_research field.
"""

import logging
import re
import html
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

import requests

logger = logging.getLogger("council.tools")


class WebSearchProvider:
    def search(
        self,
        query: str,
        num_results: int = 6,
        engines: list[str] | None = None,
        language: str = "en",
        safe_search: int = 1,
    ) -> list[dict]:
        raise NotImplementedError


# ─── SearXNG ──────────────────────────────────────────────────────────────────

class SearXNG(WebSearchProvider):
    """
    Queries a self-hosted SearXNG instance and returns clean result dicts.
    Default: http://localhost:8080
    """

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base = base_url.rstrip("/")

    def _fallback_base_urls(self) -> list[str]:
        """Common local fallback mappings for Dockerized SearXNG setups."""
        candidates = []
        if "localhost:8080" in self.base or "127.0.0.1:8080" in self.base:
            candidates.append(self.base.replace(":8080", ":8001"))
        return candidates

    def search(
        self,
        query: str,
        num_results: int = 6,
        engines: list[str] | None = None,
        language: str = "en",
        safe_search: int = 1,
    ) -> list[dict]:
        params = {
            "q":           query,
            "format":      "json",
            "language":    language,
            "safesearch":  safe_search,
            "pageno":      1,
        }
        if engines:
            params["engines"] = ",".join(engines)

        bases = [self.base, *self._fallback_base_urls()]
        data = None
        last_error = None

        for idx, base in enumerate(bases):
            try:
                resp = requests.get(
                    f"{base}/search",
                    params=params,
                    timeout=20,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                if idx > 0:
                    logger.warning(f"SearXNG fallback active: using {base} instead of {self.base}")
                    self.base = base
                break
            except requests.exceptions.ConnectionError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                break

        if data is None:
            if isinstance(last_error, requests.exceptions.ConnectionError):
                logger.warning("SearXNG unreachable — skipping web search")
            else:
                logger.warning(f"SearXNG error: {last_error}")
            return []

        results = []
        for r in data.get("results", [])[:num_results]:
            results.append({
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "snippet": r.get("content", ""),
                "source":  r.get("engine", "searxng"),
                "score":   r.get("score", 0),
            })
        logger.info(f"SearXNG: {len(results)} results for '{query}'")
        return results


class BraveSearch(WebSearchProvider):
    """Brave Web Search API provider."""

    def __init__(self, api_key: str, base_url: str = "https://api.search.brave.com/res/v1/web/search"):
        self.api_key = (api_key or "").strip()
        self.base_url = base_url

    def search(
        self,
        query: str,
        num_results: int = 6,
        engines: list[str] | None = None,
        language: str = "en",
        safe_search: int = 1,
    ) -> list[dict]:
        if not self.api_key:
            logger.warning("Brave API key is missing — skipping Brave search")
            return []

        params = {
            "q": query,
            "count": max(1, min(20, int(num_results))),
            "search_lang": language,
            "safesearch": "strict" if safe_search else "off",
        }
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        try:
            resp = requests.get(self.base_url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning(f"Brave search error: {e}")
            return []

        out = []
        for r in payload.get("web", {}).get("results", [])[:num_results]:
            out.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("description", ""),
                    "source": "brave-api",
                    "score": 0,
                }
            )
        logger.info(f"Brave API: {len(out)} results for '{query}'")
        return out


class DuckDuckGoSearch(WebSearchProvider):
    """DuckDuckGo HTML results scraping provider (no API key required)."""

    def __init__(self, base_url: str = "https://duckduckgo.com/html/"):
        self.base_url = base_url

    def _clean_text(self, s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        return html.unescape(s).strip()

    def _extract_uddg_url(self, url: str) -> str:
        if "duckduckgo.com/l/?" not in url:
            return url
        try:
            parsed = urlparse(url)
            q = parse_qs(parsed.query)
            uddg = q.get("uddg", [""])[0]
            return unquote(uddg) or url
        except Exception:
            return url

    def search(
        self,
        query: str,
        num_results: int = 6,
        engines: list[str] | None = None,
        language: str = "en",
        safe_search: int = 1,
    ) -> list[dict]:
        params = {
            "q": query,
            "kl": "us-en" if language.lower().startswith("en") else "wt-wt",
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
        }
        try:
            resp = requests.get(self.base_url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            body = resp.text
        except Exception as e:
            logger.warning(f"DuckDuckGo search error: {e}")
            return []

        links = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, flags=re.IGNORECASE | re.DOTALL)
        snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>|<div[^>]*class="result__snippet"[^>]*>(.*?)</div>', body, flags=re.IGNORECASE | re.DOTALL)

        out = []
        for idx, (href, title_raw) in enumerate(links[:num_results]):
            snip_pair = snippets[idx] if idx < len(snippets) else ("", "")
            snippet_raw = snip_pair[0] or snip_pair[1] or ""
            out.append(
                {
                    "title": self._clean_text(title_raw),
                    "url": self._extract_uddg_url(href),
                    "snippet": self._clean_text(snippet_raw),
                    "source": "duckduckgo",
                    "score": 0,
                }
            )
        logger.info(f"DuckDuckGo: {len(out)} results for '{query}'")
        return out


# ─── Playwright Scraper ───────────────────────────────────────────────────────

class PlaywrightScraper:
    """
    Fallback for JS-heavy pages. Uses playwright (sync API).
    Install: pip install playwright && playwright install chromium
    """

    def scrape(self, url: str, timeout: int = 30_000) -> dict:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not installed. pip install playwright")
            return {}

        logger.info(f"Playwright scraping: {url}")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, timeout=timeout, wait_until="domcontentloaded")

                # Extract visible text from meaningful tags
                text_parts = []
                for selector in ["h1", "h2", "h3", "p", "li", "blockquote"]:
                    elements = page.query_selector_all(selector)
                    for el in elements[:40]:
                        t = el.text_content()
                        if t and len(t.strip()) > 30:
                            text_parts.append(t.strip())

                title = page.title()
                browser.close()

                full_text = "\n".join(text_parts)
                # Trim aggressively
                snippet = full_text[:2000]
                return {"title": title, "url": url, "snippet": snippet, "source": "playwright"}

        except Exception as e:
            logger.warning(f"Playwright failed for {url}: {e}")
            return {}


# ─── Research Agent ───────────────────────────────────────────────────────────

class ResearchAgent:
    """
    Orchestrates search + optional scraping.
    Returns a list of result dicts ready for the Memory Palace.
    """

    def __init__(
        self,
        searxng: SearXNG | None = None,
        search_provider: WebSearchProvider | None = None,
        playwright: PlaywrightScraper | None = None,
        use_playwright_fallback: bool = False,
    ):
        self.searxng  = searxng  or SearXNG()
        self.search_provider = search_provider or self.searxng
        self.playwright = playwright or PlaywrightScraper()
        self.use_playwright_fallback = use_playwright_fallback
        self._blocked_domains = {
            "tiktok.com",
            "xvideos.com",
            "pornhub.com",
            "xnxx.com",
            "redtube.com",
            "youjizz.com",
        }
        self._preferred_domain_tokens = {
            ".edu",
            ".gov",
            ".dk",
            "studienet.dk",
            "uvm.dk",
            "astra.dk",
            "study.com",
            "wikipedia.org",
            "science.org",
            "nature.com",
            "arxiv.org",
        }

    def _query_terms(self, query: str) -> set[str]:
        terms = set()
        for token in re.findall(r"[a-zA-Z]{4,}", (query or "").lower()):
            if token in {"with", "that", "this", "from", "into", "about", "what", "when", "where", "which"}:
                continue
            terms.add(token)
        return terms

    def _is_quality_result(self, result: dict, query_terms: set[str]) -> bool:
        title = (result.get("title") or "").strip().lower()
        snippet = (result.get("snippet") or "").strip().lower()
        url = (result.get("url") or "").strip().lower()

        if not url or len(snippet) < 40:
            return False

        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""
        if host.startswith("www."):
            host = host[4:]

        if any(blocked in host for blocked in self._blocked_domains):
            return False

        # Reject obvious junk/unsafe snippets.
        junk_terms = {"penis", "sex", "casino", "betting", "adult content", "clickbait"}
        if any(term in snippet for term in junk_terms):
            return False

        # Mildly prefer educational/government signals.
        is_preferred = any(token in host for token in self._preferred_domain_tokens)
        if is_preferred:
            return True

        # For non-preferred hosts, require relevance to query terms.
        haystack = f"{title} {snippet}"
        overlap = sum(1 for term in query_terms if term in haystack)
        if overlap >= 1:
            return True

        # Last-resort pass for clearly descriptive science/education snippets.
        generic_terms = {"feasibility", "engineering", "analysis", "research", "study", "curriculum", "exam"}
        return any(term in haystack for term in generic_terms)

    def research(
        self,
        queries: list[str],
        results_per_query: int = 4,
        scrape_top_n: int = 2,
    ) -> list[dict]:
        """
        Run one or more search queries, optionally scraping the top N results
        with Playwright for richer content.
        """
        all_results = []
        seen_urls: set[str] = set()

        for query in queries:
            q_terms = self._query_terms(query)
            results = self.search_provider.search(query, num_results=results_per_query)
            for r in results:
                url = r.get("url", "")
                if url in seen_urls:
                    continue
                if not self._is_quality_result(r, q_terms):
                    continue
                seen_urls.add(url)
                all_results.append(r)

        # Playwright enrichment pass
        if self.use_playwright_fallback and all_results:
            for r in all_results[:scrape_top_n]:
                url = r.get("url", "")
                if not url:
                    continue
                scraped = self.playwright.scrape(url)
                if scraped.get("snippet"):
                    # Replace snippet with richer scraped content
                    r["snippet"] = scraped["snippet"]
                    r["source"]  = "playwright"

        logger.info(f"Research complete: {len(all_results)} unique results")
        return all_results

    def extract_queries_from_prompt(self, prompt: str) -> list[str]:
        """
        Simple heuristic to generate 2-3 search queries from the user's prompt.
        Will be replaced by a model call in the full pipeline.
        """
        # Trim to core — just use the first 120 chars as a seed query
        base = prompt[:120].strip().rstrip("?.,!")
        queries = [base]
        # Add a "latest" variant
        if len(prompt.split()) > 4:
            queries.append(f"{base} recent developments")
        return queries[:3]
