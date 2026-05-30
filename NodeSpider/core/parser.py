from __future__ import annotations

import base64
import binascii
import json
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse

from .models import NodeItem


SUPPORTED_SCHEMES = ("vmess://", "ss://", "trojan://")


def _pad_base64(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def decode_base64_text(value: str) -> str:
    raw = base64.b64decode(_pad_base64(value.strip()), validate=False)
    return raw.decode("utf-8", errors="ignore")


def looks_like_subscription_blob(blob: str) -> bool:
    text = blob.strip()
    if len(text) < 32 or any(ch.isspace() for ch in text):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(ch in allowed for ch in text)


def extract_subscription_links(decoded_text: str) -> list[str]:
    links: list[str] = []
    for line in decoded_text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if line.startswith(SUPPORTED_SCHEMES):
            links.append(line)
    return links


def parse_vmess_link(link: str) -> NodeItem:
    payload = link[len("vmess://") :].strip()
    decoded = decode_base64_text(payload)
    data = json.loads(decoded)
    address = str(data.get("add", "")).strip()
    port = int(str(data.get("port", "0") or "0"))
    name = str(data.get("ps") or address or "vmess-node")
    extras = {
        "uuid": data.get("id"),
        "alter_id": data.get("aid"),
        "cipher": data.get("scy") or data.get("cipher") or "auto",
        "network": data.get("net"),
        "type": data.get("type"),
        "host": data.get("host"),
        "path": data.get("path"),
        "tls": data.get("tls"),
        "sni": data.get("sni"),
    }
    return NodeItem(protocol="vmess", name=name, address=address, port=port, raw_link=link, extras=extras)


def _parse_ss_userinfo(encoded: str) -> tuple[str, str]:
    decoded = decode_base64_text(encoded)
    method, password = decoded.split(":", 1)
    return method, password


def parse_ss_link(link: str) -> NodeItem:
    body = link[len("ss://") :]
    if "#" in body:
        main, fragment = body.split("#", 1)
        name = unquote(fragment) or "ss-node"
    else:
        main = body
        name = "ss-node"

    if "?" in main:
        main, query = main.split("?", 1)
        plugin = parse_qs(query).get("plugin", [None])[0]
    else:
        plugin = None

    if "@" in main:
        userinfo, server = main.split("@", 1)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
        else:
            method, password = _parse_ss_userinfo(userinfo)
    else:
        decoded = decode_base64_text(main)
        userinfo, server = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)

    address, port_text = server.rsplit(":", 1)
    extras = {"method": method, "password": password, "plugin": plugin}
    return NodeItem(protocol="ss", name=name, address=address, port=int(port_text), raw_link=link, extras=extras)


def parse_trojan_link(link: str) -> NodeItem:
    parsed = urlparse(link)
    address = parsed.hostname or ""
    port = parsed.port or 443
    name = unquote(parsed.fragment) or address or "trojan-node"
    query = parse_qs(parsed.query)
    extras = {
        "password": unquote(parsed.username or ""),
        "sni": query.get("sni", [None])[0],
        "security": query.get("security", [None])[0],
        "type": query.get("type", [None])[0],
        "host": query.get("host", [None])[0],
        "path": query.get("path", [None])[0],
    }
    return NodeItem(protocol="trojan", name=name, address=address, port=port, raw_link=link, extras=extras)


def parse_node_link(link: str) -> NodeItem | None:
    try:
        if link.startswith("vmess://"):
            return parse_vmess_link(link)
        if link.startswith("ss://"):
            return parse_ss_link(link)
        if link.startswith("trojan://"):
            return parse_trojan_link(link)
    except (ValueError, KeyError, json.JSONDecodeError, binascii.Error):
        return None
    return None


def parse_node_links(links: Iterable[str]) -> list[NodeItem]:
    items: list[NodeItem] = []
    seen: set[str] = set()
    for link in links:
        clean = link.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        parsed = parse_node_link(clean)
        if parsed is not None:
            items.append(parsed)
    return items
