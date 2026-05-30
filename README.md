# AliDocs → 图床 → 钉钉 任务中心

这是一个轻量脚本 + WebUI 项目，支持：

- 模式1：直接根据区域截图
- 模式2：根据筛选条件截图
- 支持多个子任务合并为 1 条消息发送（逐个截图 → 逐个上传图床 → 合并 markdown）
- 定时任务新增 / 删除 / 启用 / 停用
- 当前任务状态查看
- 手动测试按钮
- 图床上传到 `imgbed-e0n.pages.dev`
- 发送到钉钉机器人
- 支持每日 / 每周

---

## WebUI

启动：

```bash
python app\webui_server.py
```

打开：

```text
http://127.0.0.1:8787
```

### WebUI 支持的设置项

- 是否启用合并发送
  - 否：按普通单任务发送
  - 是：配置多个子任务 ID，最终只发送 1 条汇总消息
- 模式
  - 1. 直接根据区域截图
  - 2. 根据筛选条件截图
- a. sheet（表名）
- b. 是否筛选
- c. 筛选（列名和值）
- b. 截图区域
  - 模式1：如 `A1:F7`
  - 模式2：如 `M:AE`
- 合并消息标题 / 正文
- 子任务 ID 列表（每行一个，或逗号分隔）
- c. 周期
  - 每日 / 每周
- d. 时间
  - 如 `09:00`

### 任务操作

- 测试并发送
- 测试截图+上传
- 启用 / 停用
- 删除
- 查看下一次执行时间

---

## 命令行

### 列出任务

```bash
python scripts\run_task.py --list
```

### 运行单个任务

```bash
python scripts\run_task.py --task hubei-no-demo
```

### 仅截图+上传

```bash
python scripts\run_task.py --task hubei-no-demo --upload-only
```

### 启动调度器

```bash
python scripts\scheduler.py
```

---

## 配置文件

正式配置：

```text
config/config.json
```

示例配置：

```text
config/config.example.json
```

### 合并发送任务示例

```json
{
  "id": "daily-summary-demo",
  "enabled": true,
  "name": "多任务合并发送示例",
  "mode": "merge_send",
  "schedule": {
    "type": "daily",
    "time": "09:30"
  },
  "merge": {
    "enabled": true,
    "task_ids": [
      "hubei-no-demo",
      "range-demo"
    ],
    "include_subtitles": true
  },
  "message": {
    "mode": "markdown",
    "title": "每日截图汇总",
    "text": "以下为今日多个任务截图汇总"
  }
}
```

执行流程：

1. 按 `merge.task_ids` 顺序逐个执行子任务截图
2. 每张截图单独上传图床
3. 按顺序拼成一条 markdown
4. 最后只发送 1 条钉钉消息

---

## 说明

当前截图方案使用的是：

- 从当前已打开的阿里文档页面读取真实数据
- 按配置筛选或按区域截取
- 重建 HTML 后截图

这比直接硬点网页 UI 更稳定，适合定时任务。
