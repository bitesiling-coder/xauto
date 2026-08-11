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
  limit_per_keyword: 10
  delay_seconds: 10
keywords:
  - '"Autonomous AI Agents" OR 自主智能体 OR "Rogue AI Agents" OR "Agent Security" OR "AI Safety Evaluation" OR "AI Cybersecurity"'
  - '"World Models" OR 世界模型 OR "Open-weight Models" OR AGI OR "Intelligence Explosion" OR "Embodied AI" OR 具身智能 OR "Humanoid Robots"'
  - 'RWA OR 现实资产代币化 OR "Tokenized Stocks" OR "Stablecoin Payments" OR "Solana RWA"'
  - '"Prediction Markets" OR "AI Agents Crypto" OR x402 OR "On-chain Perps" OR "Crypto ETF" OR MiCA OR "CLARITY Act" OR 加密监管'
embedding:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

当前配置包含两组 AI 和两组 Web3 查询，每组每天采集 10 条，最多约 40 条结果。`limit_per_keyword` 是每组的默认数量，`delay_seconds` 是 `collect --all` 在两组之间的等待时间。`embedding.model` 决定向量模型。配置中的时间为每日 10:00，时区为 `Asia/Singapore`；但 Windows 计划任务使用 Windows 本地时间，脚本不做时区换算。请确保 Windows 时区正确，并在安装计划任务时传入同样的时间。计划任务以当前交互式登录用户运行。

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
- `logs/last-run.json` 保存最近一次 collect/import/rebuild 摘要；OpenCLI 搜索失败时会记录该关键词、零写入计数和 `outcome: failed`。OpenCLI 返回的畸形行计入 `found/errors` 并以脱敏诊断写入 `logs/errors.jsonl`，不会生成 Markdown 或进入向量索引。`logs/scheduler.log` 接收计划任务的标准输出和错误。

## 本地媒体与 Markdown

每条推文仍以 `data/markdown/<推文ID>.md` 作为权威文档；允许下载的 X 图片保存在 `data/media/<推文ID>/`。Markdown 会显示完整正文、本地图片、引用推文和原始 X 链接，因此可以直接预览，也可以继续供 RAG 重建和检索。

- 视频只下载封面，不下载完整视频；原始视频 URL 继续保留。
- 正文只有短链接的推文也会保留，不会因信息较少而删除。
- 图片下载失败、超时、类型不支持或超过大小上限时，正文仍会入库，失败记录写入 `logs/errors.jsonl`。
- 自动下载只接受 HTTPS 的 X 图片域名，不会抓取导入文件指向的任意第三方网站。
- `xrag rebuild` 只读取本地 Markdown，不会重新下载媒体。

查看已经落盘的图片和视频封面：

```bash
find data/media -mindepth 2 -maxdepth 2 -type f -print
```

## 公开热点看板

看板从现有的权威 Markdown 和本地媒体中选出当天（不足时回溯最近 48 小时）的 AI/Web3 热点，并生成白色底、淡色卡片的静态网页。`dashboard build` 和 `dashboard publish` 对权威 archive 只读；前者只写入 Git 忽略的 `data/dashboard-site/`，后者还会更新专用的 `gh-pages` 发布 worktree 和远程分支。`dashboard update` 会先执行采集，因此可能在项目的 `data/` 与 `logs/` 下新增或更新 Markdown、媒体、Chroma 索引、`last-run.json` 和日志，但不会删除来源数据或电脑中的其他文件。

在 WSL 项目根目录中运行：

```bash
# 只读取现有 Markdown 并生成本地静态站点
xrag --root . dashboard build

# 本地预览；浏览器打开 http://localhost:8000
python -m http.server 8000 --directory data/dashboard-site

# 读取现有 Markdown、构建并发布到 gh-pages
xrag --root . dashboard publish

# 立即采集四组关键词、构建并发布
xrag --root . dashboard update
```

启用 GitHub Pages 后，本项目的公开地址是 <https://bitesiling-coder.github.io/xauto/>。页面上的“立即刷新”只会绕过浏览器缓存、重新读取已经发布的 `data/latest.json`，并提示是否发现了更新快照；它不会远程控制这台电脑，也不会触发 X 采集。需要现在采集并发布时，请在本机运行 `dashboard update`。

`dashboard build` 和 `dashboard publish` 都会先从现有 Markdown 构建内容；前者只生成本地站点，后者再发布。`dashboard update` 则按“采集全部四组关键词 → 构建 → 校验 → 发布”的顺序执行。采集没有写入任何帖子、输出校验失败或 Git 发布失败时，流程以非零状态停止，不会用空白或不安全的内容替换当前线上快照。

发布前请确认以下条件：

- `opencli doctor` 显示浏览器桥接及 Twitter/X 扩展已经连接；`dashboard update` 和每日任务依赖它们，单纯 `build`/`publish` 不重新采集。
- Git 已配置提交身份（`git config user.name` 和 `git config user.email`），且当前认证可以执行 `git push origin gh-pages`。发布器不会代为读取、打印或保存 Git/X 凭据。
- Git 远程 `origin` 指向预期仓库；网络或认证失败后，修复问题并重新运行原命令即可，发布器不会执行强制推送或破坏性 Git 恢复。

本地生成目录是 `data/dashboard-site/`，已被 Git 忽略。发布器使用项目内专用的 `.worktrees/x-rag-pages/` 链接 worktree，把经过白名单校验的公开文件复制到 `gh-pages`；它不使用带删除功能的目录同步，也不会清理来源数据。带日期的 JSON 快照和按内容哈希命名的媒体会持续累积，因此需要定期监控本地磁盘与 `gh-pages` 仓库大小；如需制定历史保留策略，请先单独审核，不要直接对数据目录做递归删除。

公开输出采用字段白名单，并在提交前扫描凭据和本地绝对路径。如果检测到 `auth_token`、`ct0`、其他凭据形式或 Windows/WSL 本地路径，发布会中止，已有线上 `latest.json` 保持不变。安全错误会统一脱敏，通常不会指出具体源字段；请只在本机用编辑器搜索最近新增或变更的 Markdown 中是否出现 `auth_token`、`ct0`、`authorization`、`api_key`、`password`、`secret` 等键名或本地绝对路径，修正后先重新运行 `dashboard build`。不要把凭据值粘贴到搜索命令、配置、Markdown、日志、聊天或问题报告中，也不要复制可能包含凭据的匹配行。

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

注册任务之前，非 `-DryRun` 安装器会先检查本项目的 linked-worktree Git 指针，并把项目根目录的 `.git` marker，以及已经存在的专用 `.worktrees/x-rag-pages/.git` marker 和各自的 Git admin backpointer，规范成使用 `/` 的相对路径，确保 Windows Git 与 WSL Git 都能解析。它只接受同一 common `.git/worktrees/*` 下、反向指针完全匹配且不含符号链接、junction 或其他 reparse point 的元数据；任何不确定状态都会在注册任务前停止。普通仓库的 `.git` 目录会原样跳过，尚未创建 pages worktree 也不会被安装器创建。`-DryRun` 只报告预计转换数量，不修改 Git 元数据或注册任务。这个准备步骤只处理上述本项目的精确 marker/backpointer，不会处理其他仓库或文件，也不会扫描、清理、重置或删除数据。

检查任务定义和最近运行信息：

```powershell
Get-ScheduledTask -TaskName 'X-RAG Daily Collection' | Format-List TaskName,State,Actions,Triggers,Principal
Get-ScheduledTaskInfo -TaskName 'X-RAG Daily Collection'
```

安装器将登录类型设为 `Interactive`，因此只有当该 Windows 用户已登录，且 OpenCLI 浏览器桥接可用时，收集任务才能正常运行。触发时间始终是 Windows 本地时间，不会根据 `schedule.timezone` 换算。

任务会调用 WSL 内的 `scripts/run-daily.sh`。每天 10:00，它先在 WSL 运行项目自带的 `.venv/bin/xrag --root <项目根> dashboard update --no-publish`，完成四组关键词采集、静态站点构建和安全校验；成功后再把 `scripts/publish-dashboard.py` 转换为 Windows 路径，以 `python.exe -I -S <Windows 脚本路径>` 启动隔离且不加载 `site` 的发布进程，通过 Windows Git/GCM 发布 `gh-pages`。发布脚本不接受项目根目录或其他命令行参数，只从自身位置推导仓库；它固定一个已验证的 HEAD commit，核对精确的路径、文件模式和索引标志，再通过 Git 捕获 `HEAD tree blobs`，仅编译内存中已捕获的 `xrag` 发布模块字节。因此，捕获后的工作树模块替换不会改变本次执行的代码。Manual `dashboard update` still publishes by default；`--no-publish` 仅供混合计划任务把发布阶段交给 Windows 使用。

The minimal trust boundary is the tracked scheduled launcher `scripts/run-daily.sh` and the tracked wrapper `scripts/publish-dashboard.py` selected by the Windows task. Wrapper self-checks can fail closed on accidental local changes, but cannot protect against a launcher or wrapper that was already malicious before Python started; the HEAD-blob isolation applies to the publisher modules loaded after the wrapper starts.

Hybrid scheduler requirements: WSL performs collection and build, while Windows Python, Windows Git, and Git Credential Manager perform the authenticated publication. Windows 发布端要求 Python 3.11 或更高版本，但不要求在 Windows Python 中安装 PyYAML 或项目的其他第三方依赖。当前 Windows 用户必须已登录，OpenCLI 浏览器桥接和 Twitter/X 扩展必须已连接，WSL 中必须能找到 `opencli`、`python.exe` 和 `wslpath`，Windows Git 必须配置提交身份，且 Git Credential Manager 必须允许推送 `origin/gh-pages`。任一步失败都会停止后续步骤并保留上一次成功发布的线上快照，详细输出写入 `logs/scheduler.log`。计划任务不会回退到 WSL Git 发布。

`config/keywords.yaml` 中的 `schedule.enabled: false` **不会**停止 `run-daily.sh`，也不会禁用或删除已注册的 Windows 任务。需要暂停或删除时显式执行：

```powershell
Disable-ScheduledTask -TaskName 'X-RAG Daily Collection'
Unregister-ScheduledTask -TaskName 'X-RAG Daily Collection' -Confirm:$false
```

如需重新启用已禁用的任务，执行 `Enable-ScheduledTask -TaskName 'X-RAG Daily Collection'`。如需更新 WSL 发行版或触发时间，使用新参数重新运行上述安装命令；脚本会使用 `-Force` 更新同名任务。

## 备份与恢复

为了获得一致快照，备份前先禁用或暂停 Windows 计划任务，并确保没有手动的 `collect`、`import` 或 `rebuild` 写入操作正在运行。必备内容是权威数据 `data/markdown/`、本地媒体 `data/media/` 和配置 `config/`。如果把原始导入文件保留在 `data/imports/`，也应一并备份；如需审计历史，可选备份 `logs/`。`data/chroma/` 是可重建索引，可不备份。

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
- **计划任务失败：**查看 `logs/scheduler.log`、`logs/errors.jsonl` 和 `Get-ScheduledTaskInfo`，确认 WSL 发行版名、`.venv/bin/xrag`、`python.exe`、`wslpath` 与项目路径存在；再运行 `opencli doctor` 检查浏览器桥接/扩展，并在 Windows PowerShell 中确认 Windows Git 身份和 Git Credential Manager 认证可用。WSL 只负责 `dashboard update --no-publish`；发布由 Windows `python.exe -I -S scripts/publish-dashboard.py` 完成，不接受 `--root` 或其他参数，也不依赖 WSL Git 网络。修复后可手动运行不带 `--no-publish` 的 `xrag --root . dashboard update` 验证默认的采集、构建和发布流程。
- **看板构建或发布被阻止：**空采集、候选内容不足、公开输出包含凭据/本地路径、Git worktree 不安全、远程冲突或网络认证失败都会使命令以非零状态退出。不要删除 `data/markdown/` 或重建 `gh-pages` worktree 来规避校验；先根据脱敏错误检查输入与 Git/OpenCLI 状态，然后重试。失败不会替换当前线上快照。
- **图片下载失败：**查看 `logs/errors.jsonl` 中的 `media` 记录；正文和远程链接仍会保留。确认 `pbs.twimg.com` 可访问后，下次采集同一推文会再次尝试。
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
│   ├── media/                    # 按推文 ID 保存的图片与视频封面
│   ├── chroma/                   # 可重建的本地向量索引
│   └── dashboard-site/           # 生成的本地静态站点（Git 忽略）
├── dashboard/                    # 看板 HTML、CSS、JavaScript 源文件
├── .worktrees/
│   └── x-rag-pages/              # 专用 gh-pages 发布 worktree
├── logs/                        # 最近运行、错误和调度日志
├── scripts/
│   ├── install-schedule.ps1      # Windows 计划任务安装/更新
│   ├── run-daily.sh              # WSL 采集/构建与 Windows 发布编排入口
│   └── publish-dashboard.py      # Windows Python/Git 发布入口
├── src/xrag/                     # Python 包与 CLI
└── tests/                       # 测试与离线 fixture
```
