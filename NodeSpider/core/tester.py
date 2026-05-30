from __future__ import annotations

import asyncio
import socket
import time
from typing import Iterable

import httpx

from .models import NodeIPInfo, NodeItem, NodeStatus


async def tcp_ping(address: str, port: int, timeout_seconds: float) -> NodeStatus:
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(address, port), timeout=timeout_seconds)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        writer.close()
        await writer.wait_closed()
        return NodeStatus(is_alive=True, latency_ms=latency_ms)
    except Exception as exc:
        return NodeStatus(is_alive=False, error=str(exc))


async def resolve_target(value: str) -> str:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(value, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        if infos:
            return infos[0][4][0]
    except Exception:
        return value
    return value


async def query_ip_api(client: httpx.AsyncClient, query: str) -> NodeIPInfo:
    url = f"http://ip-api.com/json/{query}"
    params = {"fields": "status,message,country,isp,proxy,hosting,query"}
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            return NodeIPInfo(error=str(data.get("message") or "ip-api failed"))
        hosting = data.get("hosting")
        proxy = data.get("proxy")
        risk_score = 20 if hosting else 5
        if proxy:
            risk_score += 25
        is_residential = None if hosting is None else not bool(hosting)
        return NodeIPInfo(
            country=data.get("country"),
            isp=data.get("isp"),
            is_residential=is_residential,
            risk_score=risk_score,
            proxy=data.get("proxy"),
            hosting=data.get("hosting"),
            query=data.get("query"),
        )
    except Exception as exc:
        return NodeIPInfo(error=str(exc))


async def evaluate_node(
    node: NodeItem,
    semaphore: asyncio.Semaphore,
    timeout_seconds: float,
    ip_client: httpx.AsyncClient,
) -> NodeItem:
    async with semaphore:
        node.status = await tcp_ping(node.address, node.port, timeout_seconds)
        query = await resolve_target(node.address)
        node.ip_info = await query_ip_api(ip_client, query)
        return node


async def evaluate_nodes(
    nodes: Iterable[NodeItem],
    concurrency: int = 20,
    timeout_seconds: float = 5.0,
) -> list[NodeItem]:
    node_list = list(nodes)
    effective_concurrency = min(max(1, concurrency), 20)
    semaphore = asyncio.Semaphore(effective_concurrency)
    ip_timeout = httpx.Timeout(min(max(timeout_seconds, 3.0), 15.0))
    async with httpx.AsyncClient(timeout=ip_timeout, headers={"User-Agent": "NodeSpider/1.0"}) as ip_client:
        tasks = [evaluate_node(node, semaphore, timeout_seconds, ip_client) for node in node_list]
        return await asyncio.gather(*tasks)
