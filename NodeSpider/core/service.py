from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import NodeItem
from .parser import parse_node_links
from .scraper import scrape_urls
from .tester import evaluate_nodes


@dataclass
class CrawlSummary:
    urls_total: int
    urls_with_links: int
    raw_links_total: int
    parsed_nodes_total: int
    alive_nodes_total: int


@dataclass
class CrawlResult:
    source_links: dict[str, list[str]]
    nodes: list[NodeItem]
    summary: CrawlSummary


async def run_nodespider(
    urls: Iterable[str],
    timeout_seconds: float = 5.0,
    concurrency: int = 10,
) -> CrawlResult:
    source_links = await scrape_urls(urls, timeout_seconds=timeout_seconds)
    all_links: list[str] = []
    for links in source_links.values():
        all_links.extend(links)

    parsed_nodes = parse_node_links(all_links)
    evaluated_nodes = await evaluate_nodes(parsed_nodes, concurrency=concurrency, timeout_seconds=timeout_seconds)
    alive_count = sum(1 for node in evaluated_nodes if node.status.is_alive)

    summary = CrawlSummary(
        urls_total=len(source_links),
        urls_with_links=sum(1 for links in source_links.values() if links),
        raw_links_total=len(all_links),
        parsed_nodes_total=len(parsed_nodes),
        alive_nodes_total=alive_count,
    )
    return CrawlResult(source_links=source_links, nodes=evaluated_nodes, summary=summary)
