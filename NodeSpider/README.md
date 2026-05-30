# NodeSpider

NodeSpider 是一个独立于当前钉钉项目的子项目，用于抓取公开页面中的代理节点链接，解析主流协议，做基础可用性与 IP 信息检测，并通过 Streamlit 展示与导出结果。

## MVP 范围

- 从一个或多个 URL 抓取文本/HTML 内容
- 提取 `vmess://`、`ss://`、`trojan://` 链接
- 尝试识别并解码包含订阅链接的 Base64 文本块
- 解析节点为结构化数据
- 异步做 TCP 连通性/延迟检测
- 调用 `ip-api.com` 查询 IP/域名归属地与 `hosting/proxy` 信息
- 在 Streamlit 页面中查看、筛选、导出结果

## 已知取舍

- 当前 MVP 使用 TCP 建连作为基础存活检测，不直接拉起完整代理协议栈做真实代理转发测速。
- `ip-api.com` 免费接口仅支持 HTTP，且限流为 45 次/分钟，需要控制并发。
- Clash 导出为基础兼容格式，复杂传输参数按可解析字段尽力生成。

## 目录结构

```text
NodeSpider/
├── main.py
├── requirements.txt
├── core/
│   ├── __init__.py
│   ├── models.py
│   ├── parser.py
│   ├── scraper.py
│   └── tester.py
└── utils/
    ├── __init__.py
    ├── exporter.py
    └── logger.py
```

## 运行

```bash
pip install -r NodeSpider/requirements.txt
streamlit run NodeSpider/main.py
```

默认地址通常是：`http://localhost:8501`

## 使用说明

1. 在左侧输入一个或多个公开页面 URL，每行一个。
2. 点击“开始执行”。
3. 系统会依次执行：抓取页面、提取节点、解析协议、做 TCP 存活检测、查询 IP 信息。
4. 页面下方可查看结果表，并导出：
   - 原始节点链接 `nodes.txt`
   - 基础 Clash 配置 `nodes.yaml`
   - 基础 V2Ray 配置 `nodes.json`

## 注意事项

- 免费 `ip-api.com` 接口不支持 HTTPS，部分网络环境可能会直接拦截 HTTP 请求。
- 当前连通性检测是“目标地址端口可连接”级别，不代表完整代理链路一定可用于科学上网。
- 如果目标页面是动态渲染且正文里没有直接暴露节点文本，当前 MVP 可能抓不到内容。
