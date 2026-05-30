from __future__ import annotations

import asyncio
from typing import Iterable

import pandas as pd
import streamlit as st

from core.service import run_nodespider
from utils.exporter import export_clash_yaml, export_raw_links, export_v2ray_json


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def nodes_to_frame(nodes) -> pd.DataFrame:
    rows = []
    for node in nodes:
        rows.append(
            {
                "name": node.name,
                "protocol": node.protocol,
                "address": node.address,
                "port": node.port,
                "alive": node.status.is_alive,
                "latency_ms": node.status.latency_ms,
                "country": node.ip_info.country,
                "isp": node.ip_info.isp,
                "residential": node.ip_info.is_residential,
                "proxy": node.ip_info.proxy,
                "hosting": node.ip_info.hosting,
                "risk_score": node.ip_info.risk_score,
                "error": node.status.error or node.ip_info.error,
                "raw_link": node.raw_link,
            }
        )
    return pd.DataFrame(rows)


def filter_alive_nodes(nodes: Iterable) -> list:
    return [node for node in nodes if node.status.is_alive]


st.set_page_config(page_title="NodeSpider", layout="wide")
st.title("NodeSpider")
st.caption("抓取公开节点链接，做基础连通性与 IP 信息检测，并导出结果。")

with st.sidebar:
    st.header("配置")
    source_input = st.text_area(
        "目标源地址",
        value="https://example.com/subscription-page",
        height=180,
        help="每行一个 URL。支持网页正文中直接节点链接，或包含 Base64 订阅文本的页面。",
    )
    timeout_seconds = st.slider("超时时间（秒）", min_value=2, max_value=15, value=5)
    concurrency = st.slider("最大并发数", min_value=1, max_value=50, value=10)
    start = st.button("开始执行", type="primary", use_container_width=True)

if "result_nodes" not in st.session_state:
    st.session_state.result_nodes = []

if "source_rows" not in st.session_state:
    st.session_state.source_rows = []

if "summary" not in st.session_state:
    st.session_state.summary = None

if start:
    urls = [line.strip() for line in source_input.splitlines() if line.strip()]
    if not urls:
        st.error("请至少输入一个目标 URL。")
    else:
        with st.status("执行中", expanded=True) as status:
            st.write("执行抓取、解析与检测流程")
            result = _run_async(run_nodespider(urls, timeout_seconds=float(timeout_seconds), concurrency=int(concurrency)))
            st.session_state.result_nodes = result.nodes
            st.session_state.source_rows = [
                {"url": url, "links_found": len(links)} for url, links in result.source_links.items()
            ]
            st.session_state.summary = result.summary
            status.update(label="执行完成", state="complete", expanded=False)

nodes = st.session_state.result_nodes
summary = st.session_state.summary

if st.session_state.source_rows:
    st.subheader("源地址抓取结果")
    st.dataframe(pd.DataFrame(st.session_state.source_rows), use_container_width=True)

if nodes:
    df = nodes_to_frame(nodes)
    alive_only = st.toggle("仅显示可用节点", value=False)
    export_nodes = filter_alive_nodes(nodes) if alive_only else nodes
    display_df = df[df["alive"] == True] if alive_only else df

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总节点数", len(nodes))
    col2.metric("可用节点", int(df["alive"].fillna(False).sum()))
    col3.metric("平均延迟", round(float(df["latency_ms"].dropna().mean()), 2) if not df["latency_ms"].dropna().empty else "-")
    col4.metric("住宅倾向", int(df["residential"].fillna(False).sum()))

    if summary is not None:
        st.caption(
            f"已抓取 {summary.urls_total} 个源，其中 {summary.urls_with_links} 个提取到链接；"
            f"共发现 {summary.raw_links_total} 条原始链接，成功解析 {summary.parsed_nodes_total} 个节点。"
        )

    st.subheader("检测结果")
    st.dataframe(display_df, use_container_width=True)

    st.subheader("导出")
    st.download_button("导出原始订阅链接", data=export_raw_links(export_nodes), file_name="nodes.txt")
    st.download_button("导出 Clash YAML", data=export_clash_yaml(export_nodes), file_name="nodes.yaml")
    st.download_button("导出 V2Ray JSON", data=export_v2ray_json(export_nodes), file_name="nodes.json")
else:
    st.info("在左侧输入目标 URL 后，点击“开始执行”。")
