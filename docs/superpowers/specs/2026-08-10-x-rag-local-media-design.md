# X-RAG 本地媒体与可读 Markdown 设计

日期：2026-08-10

## 背景

当前 X-RAG 会把 OpenCLI 返回的推文正文写入 Markdown 正文，并把 `media_urls` 保存在 YAML front matter 中。实际检查发现：

- 部分搜索结果本身只有 `t.co` 短链接，OpenCLI 的详情接口也没有返回更多正文或媒体；
- OpenCLI 对其他推文能够返回完整长正文、图片地址、视频地址、视频封面及引用推文；
- 当前解析器忽略 `media_posters` 和引用推文；
- 当前 Markdown 不下载媒体，也不在正文区域渲染图片，因此用户无法离线查看图片，文档的可读性也较差。

本设计在保留 Markdown 作为权威资料库的前提下，增加安全的本地图片与视频封面存储，并把正文和媒体组织成可直接阅读的 Markdown。

## 目标

1. 完整保存 OpenCLI 返回的推文正文，不摘要、不截断。
2. 下载 X 官方媒体域名返回的图片到本地。
3. 对视频只下载封面，并保留原视频链接；不下载完整视频。
4. 保存引用推文的作者、正文、原文链接、图片和视频封面。
5. 在 Markdown 正文中使用相对路径显示本地图片和视频封面。
6. 保留原始媒体 URL 与本地文件路径，供程序调用和问题排查。
7. 媒体下载失败时仍保存推文正文和远程链接，不让单个媒体失败中断整批采集。
8. 兼容现有 Markdown，并继续支持离线重建与检索。
9. 把每日关键词缩减为两个 AI 主题组和两个 Web3 主题组，每组采集 10 条。

## 非目标

- 不下载完整视频。
- 不抓取或解析 `t.co` 指向的任意第三方网页。
- 不使用大模型生成摘要或改写正文。
- 不删除正文只有短链接的低信息推文。
- 不让 `rebuild` 命令联网下载媒体。
- 不自动下载导入文件中指向任意非 X 域名的资源。

## 关键词配置

`config/keywords.yaml` 保存以下四个查询，每个查询的 `limit_per_keyword` 为 10：

### AI：AI Agents 与安全

```text
"Autonomous AI Agents" OR 自主智能体 OR "Rogue AI Agents" OR "Agent Security" OR "AI Safety Evaluation" OR "AI Cybersecurity"
```

### AI：前沿模型与机器人

```text
"World Models" OR 世界模型 OR "Open-weight Models" OR AGI OR "Intelligence Explosion" OR "Embodied AI" OR 具身智能 OR "Humanoid Robots"
```

### Web3：资产代币化与支付

```text
RWA OR 现实资产代币化 OR "Tokenized Stocks" OR "Stablecoin Payments" OR "Solana RWA"
```

### Web3：链上市场与监管

```text
"Prediction Markets" OR "AI Agents Crypto" OR x402 OR "On-chain Perps" OR "Crypto ETF" OR MiCA OR "CLARITY Act" OR 加密监管
```

每天最多请求约 40 条结果；现有按推文 ID 去重的行为保持不变。

## 架构

采集数据流：

```text
OpenCLI YAML
  -> OpenCLIClient（解析正文、顶层媒体、视频封面、引用推文）
  -> MediaStore（下载并校验图片/封面）
  -> MarkdownStore（写入可读 Markdown 与媒体元数据）
  -> VectorStore（仅索引文本）
```

新增 `MediaStore`，使网络下载与 Markdown 序列化保持独立。`Service` 负责协调三个存储组件，并把媒体下载错误写入现有错误日志。

### 数据模型

`Post` 保留现有字段，并增加：

- `media_posters`：视频封面远程 URL；
- `quoted_post`：可选的引用推文结构；
- `local_media`：已经安全保存的本地媒体描述列表。

本地媒体描述至少包含：

- `kind`：`image` 或 `video_poster`；
- `source_url`：OpenCLI 返回的原始 URL；
- `relative_path`：相对 Markdown 文件的本地路径；
- `content_type`：校验后的 MIME 类型。

引用推文使用有界的独立模型，不允许无限递归嵌套；只保存 OpenCLI 返回的第一层引用内容。

## 本地目录

```text
data/
├── markdown/
│   └── <推文ID>.md
└── media/
    └── <推文ID>/
        ├── image-01.jpg
        ├── image-02.png
        └── video-poster-01.jpg
```

文件名由媒体类型和稳定序号生成，不直接采用远程 URL 中的文件名。再次采集同一推文时，相同源 URL 复用已有文件。

## MediaStore 行为

1. 只接受 HTTPS URL。
2. 自动下载仅允许 X 官方媒体域名，包括 `pbs.twimg.com`；视频封面也必须来自允许域名。
3. 不跟随重定向到非允许域名。
4. 使用固定超时和单文件大小上限；默认上限为 25 MiB。
5. 只接受受支持的图片 MIME 类型：JPEG、PNG、WebP 和 GIF。
6. 文件扩展名由校验后的 MIME 类型决定，不信任 URL 后缀。
7. 在目标目录内写临时文件，完成校验和同步后原子替换。
8. 超时、HTTP 错误、类型错误、超限或磁盘写入错误均返回结构化失败，不留下半文件。
9. 下载失败不删除已有有效文件。
10. 不下载 `video.twimg.com` 视频正文；视频项只使用 `media_posters` 中的图片封面。

## Markdown 格式

YAML front matter 保留现有字段，并增加：

- `media_posters`
- `local_media`
- `quoted_tweet`（如果存在）

正文格式：

```markdown
# @作者的推文

## 正文

OpenCLI 返回的完整正文。

## 媒体

![图片 1](../media/<推文ID>/image-01.jpg)

[查看原始图片](https://pbs.twimg.com/...)

![视频封面 1](../media/<推文ID>/video-poster-01.jpg)

[打开原视频](https://video.twimg.com/...)

## 引用推文

> @引用作者：引用推文的完整正文

![引用图片 1](../media/<推文ID>/quoted-image-01.jpg)

[查看引用推文](https://x.com/...)

[查看 X 原文](https://x.com/...)
```

如果推文只有短链接且没有媒体，仍写入正文区，不删除、不隐藏。没有媒体或引用推文时省略对应章节。

## 兼容与迁移

- 读取旧 Markdown 时，缺失的新字段按空值处理。
- `rebuild` 继续只读取 Markdown 并重建 Chroma，不发起网络请求。
- 新采集或再次采集同一推文时，将其重写为新格式，并下载当前 OpenCLI 返回的媒体。
- 现有五条短链接记录保留。它们的详情接口没有返回媒体时不会制造虚假内容。
- 功能上线后实际运行四组关键词采集，以增加包含完整正文和媒体的新资料。

## 错误处理与日志

媒体失败记录使用现有 `logs/errors.jsonl`，包含操作类型、推文 ID、经过脱敏的来源 URL、错误类型与简短原因。日志不得包含 Cookie、Authorization、`auth_token` 或 `ct0`。

一次推文包含多个媒体时，各媒体独立处理：成功项保留，失败项记录远程链接。整批 `collect` 的 `errors` 计数包含媒体失败数，但推文正文仍计入 `stored`。

## 检索行为

向量索引包含：

- 主推文正文；
- 第一层引用推文正文（如果存在）。

本地文件路径、远程媒体 URL 和 Markdown 装饰文字不进入向量文本。搜索结果继续返回原始推文 URL 和 Markdown 路径，不生成回答或摘要。

## 计划任务

现有 Windows 计划任务继续每天上午 10:00 运行 `scripts/run-daily.sh`。脚本、离线模型设置、OpenCLI 路径和浏览器桥接检查保持不变。四个关键词组按配置顺序执行，组间继续使用已有延迟配置，降低触发 X 限流的风险。

## 测试与验收

自动测试覆盖：

1. OpenCLI 顶层 `media_urls`、`media_posters` 和第一层 `quoted_tweet` 解析。
2. MediaStore 成功下载、去重、MIME 校验、域名限制、重定向限制、超时、大小上限、原子写入与残留清理。
3. Markdown 新格式、相对路径、正文完整性、引用推文和旧格式兼容。
4. 单个媒体失败后正文仍入库，并正确增加错误计数和日志。
5. `rebuild` 不联网，旧文档仍可重建和检索。
6. 四组关键词与每组 10 条的配置测试。
7. 完整现有测试套件无回归。

实际验收：

1. 运行 OpenCLI doctor，确认浏览器桥接连接。
2. 运行四组关键词采集。
3. 至少确认一条包含长正文的 Markdown。
4. 至少确认一张图片或视频封面实际存在于 `data/media/<推文ID>/`。
5. 确认 Markdown 使用相对路径并能在预览器显示本地媒体。
6. 运行离线 `xrag search`，确认命中正文并返回原文 URL 与 Markdown 路径。
7. 运行完整测试、依赖检查和计划任务状态检查。

## Git 集成

该功能在 `codex/x-rag-media` 分支开发。设计、实现和测试完成后推送新分支并创建独立 PR；不修改或删除已合并的 PR #1，也不删除当前本地采集数据。
