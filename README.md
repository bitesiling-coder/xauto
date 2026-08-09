# X-RAG 本地运行指南

X-RAG 是一个仅检索的本地 RAG 工具：它通过 OpenCLI 收集 X 帖子，将 `data/markdown/` 中的 Markdown 作为唯一权威数据，并在 `data/chroma/` 生成可随时重建的 Chroma 检索索引。`search` 只返回相关原文片段和溯源信息，不生成答案，也不调用云端 LLM。

## 前置条件

- Windows 上的 WSL Ubuntu，以及 WSL 内的 Python 3.11 或更高版本。
- WSL 内已安装 OpenCLI，`opencli doctor` 显示浏览器桥接和 Twitter/X 扩展已连接。
- Ubuntu 如果提示无法创建 venv，先执行 `sudo apt update && sudo apt install -y python3-venv`。
- 安装依赖会下载较大的 PyTorch/模型运行栈，首次功能性 `xrag` 命令还需下载 embedding 模型；请预留时间、网络和足够磁盘空间。模型缓存完成后可本地检索。

## 快速开始

在 WSL Ubuntu 中执行：

```bash
cd "/mnt/c/Users/1/Documents/X工作流"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
xrag --help
```

`.[dev]` 包含测试依赖；仅运行时可用 `python -m pip install -e .`。上述是安装命令，请以 `xrag --help` 的实际结果确认安装。隔离 worktree 只用于开发；用户最终应在上面的普通项目路径中运行。

编辑 `config/keywords.yaml`：

```yaml
schedule:
  enabled: true
  time: "10:00"
  timezone: Asia/Singapore
collection:
  limit_per_keyword: 50
  delay_seconds: 10
keywords:
  - DDR5
  - 人工智能
embedding:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

`limit_per_keyword` 是每个关键词的默认数量，`delay_seconds` 是 `collect --all` 在两个关键词之间的等待时间。`embedding.model` 决定向量模型。配置中的时间为每日 10:00，时区为 `Asia/Singapore`；但 Windows 计划任务使用 Windows 本地时间，脚本不做时区换算。请确保 Windows 时区正确，并在安装计划任务时传入同样的时间。计划任务以当前交互式登录用户运行。

然后执行一次收集、检查状态和检索：

```bash
xrag --root . collect "DDR5" --limit 20
xrag --root . status
xrag --root . search "DDR5 内存价格" --top 5
```

## 常用命令与输出

```bash
# 收集单个关键词；省略 --limit 时使用配置值
xrag --root . collect "DDR5" --limit 20

# 按配置顺序收集全部关键词
xrag --root . collect --all

# 导入单个受支持的文件，或递归扫描目录
xrag --root . import data/imports

# 语义检索、运行状态和从 Markdown 重建索引
xrag --root . search "内存价格" --top 10
xrag --root . status
xrag --root . rebuild
```

- `collect` 输出 `found/stored/chunks/errors`；`collect --all` 每个关键词一行。
- `import`、`status` 和 `rebuild` 输出 JSON。`search` 输出排名、分数、作者、时间、文本、URL 和 Markdown 路径；无命中时输出 `No results found.`。
- `data/markdown/` 是可读、可备份的权威帖子库；`data/imports/` 是建议的待导入目录；`data/chroma/` 是可丢弃索引。
- `logs/last-run.json` 保存最近一次 collect/import/rebuild 摘要，`logs/errors.jsonl` 保存单项错误，`logs/scheduler.log` 接收计划任务的标准输出和错误。

## 导入格式

支持 `.yaml`/`.yml`、`.json` 和 `.md`。YAML/JSON 顶层可以是单个对象或对象列表；每条至少需要非空 `id` 和 `text`。

```yaml
- id: ddr5-001
  author: example
  text: DDR5 价格观察
  created_at: "2026-08-08T10:30:00Z"
  url: https://x.com/example/status/123
  likes: 5
  views: 1739
  media_urls:
    - https://example.com/image.jpg
  source_keywords: [DDR5]
```

```json
{"id":"ddr5-002","author":"example","text":"DDR5 市场更新","source_keywords":["DDR5"]}
```

Markdown 使用 YAML front matter 和正文：

```markdown
---
id: ddr5-003
author: example
created_at: "2026-08-08T10:30:00Z"
url: https://x.com/example/status/456
source_keywords: [DDR5]
---
DDR5 现货市场观察。
```

ID 只能匹配 `[A-Za-z0-9][A-Za-z0-9_-]*`，其中所有字符都必须是 ASCII：首字符必须是 ASCII 字母或数字，其余只允许 ASCII 字母、数字、下划线和连字号。同一批数据中不允许忽略大小写后重复的 ID，权威目录也会拒绝大小写冲突。Markdown 可在缺少 `id` 时使用符合该规则的文件名作为 ID。

## Windows 每日计划任务

在 Windows PowerShell 中进入项目根目录。先预览不写入系统的命令：

```powershell
Set-Location 'C:\Users\1\Documents\X工作流'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-schedule.ps1 -Distribution Ubuntu -ScheduleTime "10:00" -DryRun
```

安装或更新名为 `X-RAG Daily Collection` 的任务（脚本使用 `-Force`）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-schedule.ps1 -Distribution Ubuntu -ScheduleTime "10:00"
```

检查任务定义和最近运行信息：

```powershell
Get-ScheduledTask -TaskName 'X-RAG Daily Collection' | Format-List TaskName,State,Actions,Triggers,Principal
Get-ScheduledTaskInfo -TaskName 'X-RAG Daily Collection'
```

安装器将登录类型设为 `Interactive`，因此只有当该 Windows 用户已登录，且 OpenCLI 浏览器桥接可用时，收集任务才能正常运行。触发时间始终是 Windows 本地时间，不会根据 `schedule.timezone` 换算。

任务会调用 WSL 内的 `scripts/run-daily.sh`，该脚本运行项目自带的 `.venv/bin/xrag --root <项目根> collect --all`。`config/keywords.yaml` 中的 `schedule.enabled: false` **不会**停止 `run-daily.sh`，也不会禁用或删除已注册的 Windows 任务。需要暂停或删除时显式执行：

```powershell
Disable-ScheduledTask -TaskName 'X-RAG Daily Collection'
Unregister-ScheduledTask -TaskName 'X-RAG Daily Collection' -Confirm:$false
```

如需重新启用已禁用的任务，执行 `Enable-ScheduledTask -TaskName 'X-RAG Daily Collection'`。如需更新 WSL 发行版或触发时间，使用新参数重新运行上述安装命令；脚本会使用 `-Force` 更新同名任务。

## 备份与恢复

为了获得一致快照，备份前先禁用或暂停 Windows 计划任务，并确保没有手动的 `collect`、`import` 或 `rebuild` 写入操作正在运行。必备内容是权威数据 `data/markdown/` 和配置 `config/`。如果把原始导入文件保留在 `data/imports/`，也应一并备份；如需审计历史，可选备份 `logs/`。`data/chroma/` 是可重建索引，可不备份。

恢复必备目录（以及需要保留的 `data/imports/`/`logs/`）后，在 WSL 项目根目录重建向量索引：

```bash
xrag --root . rebuild
xrag --root . status
```

## 故障排查

- **OpenCLI 无法收集：**先运行 `opencli doctor`，确认浏览器桥接与 Twitter/X 扩展已连接，再直接测试 `opencli twitter search "DDR5" --limit 1 -f yaml`。
- **WSL 代理警告或无法下载模型：** Windows 上的 localhost 代理未必能直接从 WSL 访问。先修正 WSL 网络/代理配置，确认 WSL 可访问所需下载地址，再重试首次模型加载。
- **更换了 `embedding.model` 或 Chroma 损坏：**运行 `xrag --root . rebuild`。重建会在独立目录生成完整新索引，成功后才替换旧索引；模型加载或索引失败时保留旧索引。
- **Markdown 损坏：** `xrag --root . status` 的 `document_errors` 会报告无法解析的文档数。检查 `logs/errors.jsonl`，修复对应 Markdown front matter 后再 `rebuild`；只要有一篇文档失败，重建就不会替换旧索引。
- **计划任务失败：**查看 `logs/scheduler.log`、`logs/errors.jsonl` 和 `Get-ScheduledTaskInfo`，确认 WSL 发行版名、`.venv/bin/xrag` 与项目路径存在。
- **中文乱码：**导入文件必须是 UTF-8；在 WSL 中可设置 `export PYTHONUTF8=1` 后重试。请优先在 WSL 终端运行 CLI。

## 安全

不要在命令、配置、导入文件或问题报告中粘贴/存储 token、`auth_token`、`ct0` 或其他凭据。X 的认证由 OpenCLI 浏览器桥接管理，X-RAG 不需要接管这些凭据。本地错误日志会尽力遮罩常见密钥形式，但源 YAML、JSON、Markdown 和其备份由用户自行保护；日志遮罩不能代替源文件清理。

## 目录结构

```text
X工作流/
├── config/keywords.yaml       # 关键词、收集、调度和模型配置
├── data/
│   ├── imports/                  # 建议的待导入文件目录
│   ├── markdown/                 # 权威 Markdown 帖子库
│   └── chroma/                   # 可重建的本地向量索引
├── logs/                        # 最近运行、错误和调度日志
├── scripts/
│   ├── install-schedule.ps1      # Windows 计划任务安装/更新
│   └── run-daily.sh              # WSL 每日收集入口
├── src/xrag/                     # Python 包与 CLI
└── tests/                       # 测试与离线 fixture
```
