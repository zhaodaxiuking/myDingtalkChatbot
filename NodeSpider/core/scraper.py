from __future__ import annotations

import re
from typing import Iterable

import httpx

from .parser import decode_base64_text, extract_subscription_links, looks_like_subscription_blob


DIRECT_LINK_RE = re.compile(r"(?:vmess|ss|trojan)://[^\s'\"<>]+")
BASE64_BLOB_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{48,}(?![A-Za-z0-9+/=])")


async def fetch_url_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return response.text


def extract_links_from_text(text: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    for match in DIRECT_LINK_RE.findall(text):
        if match not in seen:
            seen.add(match)
            links.append(match)

    for blob in BASE64_BLOB_RE.findall(text):
        if not looks_like_subscription_blob(blob):
            continue
        try:
            decoded_links = extract_subscription_links(decode_base64_text(blob))
        except Exception:
            continue
        for link in decoded_links:
            if link not in seen:
                seen.add(link)
                links.append(link)

    return links


async def scrape_urls(urls: Iterable[str], timeout_seconds: float = 10.0) -> dict[str, list[str]]:
    clean_urls = [url.strip() for url in urls if url.strip()]
    results: dict[str, list[str]] = {}
    timeout = httpx.Timeout(timeout_seconds)
    headers = {"User-Agent": "NodeSpider/1.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for url in clean_urls:
            try:
                text = await fetch_url_text(client, url)
            except Exception:
                results[url] = []
                continue
            results[url] = extract_links_from_text(text)
    return results
