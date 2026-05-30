from __future__ import annotations

import json
from typing import Iterable

from core.models import NodeItem


def export_raw_links(nodes: Iterable[NodeItem]) -> str:
    return "\n".join(node.raw_link for node in nodes)


def export_v2ray_json(nodes: Iterable[NodeItem]) -> str:
    outbounds = []
    for node in nodes:
        if node.protocol == "vmess":
            outbounds.append(
                {
                    "protocol": "vmess",
                    "tag": node.name,
                    "settings": {
                        "vnext": [
                            {
                                "address": node.address,
                                "port": node.port,
                                "users": [
                                    {
                                        "id": node.extras.get("uuid"),
                                        "alterId": int(str(node.extras.get("alter_id") or 0)),
                                        "security": node.extras.get("cipher") or "auto",
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        elif node.protocol == "ss":
            outbounds.append(
                {
                    "protocol": "shadowsocks",
                    "tag": node.name,
                    "settings": {
                        "servers": [
                            {
                                "address": node.address,
                                "port": node.port,
                                "method": node.extras.get("method"),
                                "password": node.extras.get("password"),
                            }
                        ]
                    },
                }
            )
        elif node.protocol == "trojan":
            outbounds.append(
                {
                    "protocol": "trojan",
                    "tag": node.name,
                    "settings": {
                        "servers": [
                            {
                                "address": node.address,
                                "port": node.port,
                                "password": node.extras.get("password"),
                            }
                        ]
                    },
                }
            )
    payload = {"outbounds": outbounds}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _yaml_quote(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def export_clash_yaml(nodes: Iterable[NodeItem]) -> str:
    lines = ["proxies:"]
    for node in nodes:
        lines.append(f"  - name: {_yaml_quote(node.name)}")
        lines.append(f"    type: {node.protocol}")
        lines.append(f"    server: {_yaml_quote(node.address)}")
        lines.append(f"    port: {node.port}")
        if node.protocol == "vmess":
            lines.append(f"    uuid: {_yaml_quote(str(node.extras.get('uuid') or ''))}")
            lines.append(f"    alterId: {int(str(node.extras.get('alter_id') or 0))}")
            lines.append(f"    cipher: {_yaml_quote(str(node.extras.get('cipher') or 'auto'))}")
            if node.extras.get("tls"):
                lines.append("    tls: true")
            if node.extras.get("network"):
                lines.append(f"    network: {_yaml_quote(str(node.extras.get('network')))}")
            if node.extras.get("host"):
                lines.append(f"    servername: {_yaml_quote(str(node.extras.get('host')))}")
            if node.extras.get("path"):
                lines.append(f"    ws-opts:")
                lines.append(f"      path: {_yaml_quote(str(node.extras.get('path')))}")
        elif node.protocol == "ss":
            lines.append(f"    cipher: {_yaml_quote(str(node.extras.get('method') or ''))}")
            lines.append(f"    password: {_yaml_quote(str(node.extras.get('password') or ''))}")
        elif node.protocol == "trojan":
            lines.append(f"    password: {_yaml_quote(str(node.extras.get('password') or ''))}")
            if node.extras.get("sni"):
                lines.append(f"    sni: {_yaml_quote(str(node.extras.get('sni')))}")
    return "\n".join(lines) + "\n"
