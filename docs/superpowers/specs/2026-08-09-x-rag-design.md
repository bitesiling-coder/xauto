# X 信息 RAG 资料库设计

## 目标

在本地搭建一套免费的 X 信息资料库。系统使用现有 `opencli` 连接搜索 X，将帖子保存为 Markdown，并用本地中文嵌入模型和 ChromaDB 建立语义索引。用户通过命令行收集、导入、查询和维护资料库。

系统仅返回相关 Markdown 原文、元数据和原帖链接，不运行生成式大模型。所有内容、向量和日志均保存在本机。

## 技术选型

- 运行环境：Ubuntu WSL
- 语言：Python 3
- X 数据源：已安装并连接浏览器扩展的 `opencli`
- 文档格式：UTF-8 Markdown
- 向量库：本地持久化 ChromaDB
- 嵌入：免费的本地中文/多语言嵌入模型
- 调度：Windows 任务计划程序调用 WSL
- 时区：Asia/Singapore

## 项目结构

```text
config/
└─ keywords.yaml             # 关键词、每次数量、采集间隔和每日时间
data/
├─ markdown/                 # 每条 X 帖子的 Markdown 源文档
├─ imports/                  # 待导入的 YAML、JSON 和 Markdown
└─ chroma/                   # ChromaDB 持久化数据
logs/                         # 采集、导入和索引日志
src/xrag/                     # Python 应用源码
tests/                        # 自动化测试
scripts/                      # 安装和定时任务脚本
```

## Markdown 文档规范

每条帖子保存为一个独立文件，文件名以帖子 ID 为稳定标识。文档使用 YAML front matter 保存结构化字段，正文保留原始内容。

元数据包括：

- `id`
- `author`
- `author_bio`
- `created_at`
- `collected_at`
- `updated_at`
- `url`
- `likes`
- `views`
- `media_urls`
- `source_keywords`
- `source_type`

同一帖子被多个关键词命中时，`source_keywords` 合并去重。再次采集时更新点赞数、浏览量和媒体链接，不创建重复文件。

## 命令行接口

```bash
xrag collect "AI 视频" --limit 50
xrag collect --all
xrag import data/imports
xrag search "最近大家在讨论哪些 AI 视频工具？" --top 10
xrag status
xrag rebuild
```

- `collect <keyword>`：立即搜索一个关键词，生成或更新 Markdown，然后更新向量索引。
- `collect --all`：按配置文件逐个收集所有关键词，关键词之间按配置暂停。
- `import <path>`：递归导入 YAML、JSON 和 Markdown，正规化后并入资料库。
- `search <query>`：返回排名最高的 Markdown 片段、作者、日期、分数和原帖链接。
- `status`：显示文档数、分块数、关键词数和最后一次采集结果。
- `rebuild`：删除可再生成的向量索引，然后从 Markdown 源文档重建。Markdown 不受影响。

## 数据流

```text
opencli twitter search
  -> 解析帖子
  -> 按帖子 ID 去重和更新
  -> 生成 UTF-8 Markdown
  -> 按正文语义边界分块
  -> 本地嵌入模型生成向量
  -> 写入 ChromaDB
  -> 查询返回可追溯的原文和链接
```

Markdown 是真实数据源，ChromaDB 是可再生成的检索索引。向量库损坏或被删除时，`rebuild` 能从 Markdown 恢复。

## 自动采集

默认配置：

```yaml
schedule:
  enabled: true
  time: "10:00"
  timezone: "Asia/Singapore"

collection:
  limit_per_keyword: 50
  delay_seconds: 10

keywords:
  - 人工智能
```

Windows 任务计划程序每天上午 10:00 调用 WSL 中的 `xrag collect --all`。定时任务名固定且可重复安装；重复执行安装脚本时更新现有任务，不创建多个副本。任务使用当前 Windows 用户权限，不要求管理员权限。

## 错误处理与安全性

- 网络、X 页面或浏览器扩展不可用时，任务记录失败并以非零状态结束，不修改已有资料。
- 单条帖子无法解析时，记录帖子识别信息后跳过，其他帖子继续处理。
- 导入文件格式错误时，报告文件路径和原因，不将部分解析数据写入库。
- 手动采集、导入和定时采集共享跨进程文件锁，同一时刻只允许一个写任务。
- 不在代码、Markdown 或日志中保存 X Cookie、auth token 或其他凭据。`opencli` 继续使用其现有的浏览器桥接认证。
- 正常的重建操作只替换 `data/chroma/` 中的可再生成数据，不删除 Markdown 原文档。

## 测试策略

实现遵循测试驱动开发，每项行为先写失败测试，再写最小实现。

自动化测试覆盖：

- `opencli` YAML 结果解析
- Markdown front matter、正文和文件名
- 按帖子 ID 去重
- 互动数据更新和关键词合并
- YAML、JSON 和 Markdown 导入
- 中文分块
- 向量入库、查询和从 Markdown 重建
- 损坏输入跳过与错误报告
- 文件锁和重复任务防护
- 配置解析和每日 10:00 定时命令生成

最终验收执行一次真实的“关键词搜索 -> Markdown 入库 -> 中文语义查询”端到端流程，并检查任务计划程序中的每日 10:00 任务。

## 完成标准

1. 所有自动化测试通过。
2. 真实 X 搜索至少产生一个合法 Markdown 文档并建立向量索引。
3. 中文查询返回带原帖链接的相关原文。
4. 现有的 YAML、JSON 或 Markdown 文件可被导入。
5. `rebuild` 能仅依赖 Markdown 重建可查询索引。
6. Windows 任务计划程序包含一个每天上午 10:00 运行的采集任务。
7. 中文使用说明包含安装、配置关键词、手动采集、导入、查询、重建和调整时间的步骤。
