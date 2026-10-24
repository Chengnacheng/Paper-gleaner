# Paper Gleaner

一个 fork 即用、不需要服务器的论文追踪 Agent。

- 写一段研究方向描述即可，Zotero 可选；
- 嵌入粗筛 + LLM 精排，每篇推荐都说明「为什么和你相关」；
- 每日推送发成 GitHub Issue，评论 `/deep`、`/compare` 让 Agent 深读、对比；
- 根据 👍/👎 自动更新兴趣画像，记忆以文件形式存在仓库中；
- 支持邮件、飞书、企业微信推送，以及 Claude Code Skill/MCP；
- 适配 DeepSeek、Qwen 等低价模型。

> 项目处于早期开发阶段：每日日报（Phase 1）和 Issue 指令交互（Phase 2）已可用，反馈学习、多渠道推送等功能还在开发中。总体架构与实施路线见 [docs/architecture.md](docs/architecture.md)。

## 在 GitHub Actions 上使用

1. 在仓库 Settings → Secrets and variables → Actions 中添加 Secret `LLM_API_KEY`；不用 DeepSeek 时再添加 `LLM_BASE_URL`（如 `https://dashscope.aliyuncs.com/compatible-mode/v1`），并在 `config.yaml` 或 Actions variables（`LLM_FAST_MODEL` / `LLM_DEEP_MODEL`）中改模型名；
2. 把 `profile/interests.example.md` 复制为 `profile/interests.md`，用自然语言写下你的研究方向；
3. 把 `config.example.yaml` 复制为 `config.yaml`，至少改一下要关注的 arXiv 分类；
4. 在 Actions 页面打开 **Daily digest**，点 Run workflow 手动运行一次。可以先勾选 dry_run 试跑：只跑流程、不发 Issue；周末和节假日 arXiv 没有新公告时，试运行会改用最近提交的 20 篇论文，照样能检查大模型和嵌入模型是否配置正确。

之后每个工作日北京时间 09:30 自动运行，日报发成带 `daily` 标签的 Issue。运行产生的数据（去重记录、兴趣拆解缓存、每日结果和统计、深读报告）保存在 `gleaner-data` 分支，不会污染 `main`。

## 在 Issue 中交互

在日报 Issue 下评论指令，Interact workflow 会运行 Agent 并回复（要等 Actions 启动和模型生成，一般需要一到几分钟）。收到指令时评论会被加上 👀，完成后加 🚀，出错时加 😕 并回复原因。

| 指令 | 示例 | 说明 |
| --- | --- | --- |
| `/deep <ref>` | `/deep 3`、`/deep 2410.12345` | 读全文（arXiv HTML，没有时读 PDF），生成深读报告：一句话总结、问题与动机、方法要点、实验、代码与资源、与你研究的关系、局限与可追问点。报告缓存在 `gleaner-data` 的 `reports/`，再次 `/deep` 直接返回缓存，`/deep 3 --force` 重新生成 |
| `/compare <ref> <ref> ...` | `/compare 3 5` | 多篇论文（最多 5 篇）横向对比问题、方法、实验设置、结果和代码 |
| `/compare <ref>` | `/compare 3` | 单篇论文与相关工作（Semantic Scholar 的参考文献和推荐论文）对比 |
| `/more [n]` | `/more`、`/more 10` | 展开这份日报中打过分但没有推送的候选，默认 5 篇，不调用 LLM |
| `/help` | | 列出可用指令 |

- `<ref>` 可以是本 Issue 中的序号、arXiv ID（可带版本号，如 `2410.12345v2`）或 arXiv 链接。序号按 Issue 对应的那一天解析，在旧日报下评论同样有效；`/more` 列出的候选接着日报往下编号（日报有 6 篇时，候选从 7 开始）。
- 只响应仓库所有者和协作者的评论，Issue 必须带 `gleaner` 标签（日报自动带上）。
- `/deep`、`/compare` 使用 deep 档模型，单条指令的输入不超过 `deep_read.max_input_tokens`（默认 30000）。可选添加 Secret `S2_API_KEY` 提高 Semantic Scholar 的请求限额；没有时偶尔会被限流，回复中会注明缺少相关工作部分。
- 外部服务失败时指令不会失败：拿不到全文就基于摘要分析，LLM 调用失败就先回复摘要和代码链接。

## 本地开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                                   # 安装依赖
cp config.example.yaml config.yaml        # 按需修改
cp profile/interests.example.md profile/interests.md

export LLM_API_KEY=sk-...                 # 可选：LLM_BASE_URL 覆盖配置中的 base_url
uv run gleaner check --ping-llm           # 检查配置，并验证 API Key 与模型名
uv run gleaner daily --dry-run            # 完整跑一遍：抓取、筛选、精排，打印结果但不发布

uv run gleaner deep 2410.12345            # 本地深读，报告打印到终端（读取但不写入报告缓存）
uv run gleaner deep 1 --day 2026-10-25    # 序号按某一天的日报解析，默认最近一份
uv run gleaner compare 1 2                # 本地对比；只给一篇时与相关工作对比
uv run gleaner handle-comment --event event.json --dry-run   # 用 issue_comment 事件文件调试，只打印回复

uv run ruff check && uv run ruff format --check
uv run pytest
```

本地也可以把 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 或 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 写入仓库根目录的 `.env`；默认优先使用 DeepSeek，设置 `LLM_PROVIDER=openai` 可切换到 OpenAI。模型名可用 `DEEPSEEK_FAST_MODEL` / `DEEPSEEK_DEEP_MODEL` 或对应的 `OPENAI_*` 变量覆盖。GitHub Actions 中继续使用 `LLM_API_KEY` 和 `LLM_BASE_URL` Secrets。

## 目录结构

```text
gleaner/
├── cli.py                   # 命令行入口
├── config.py                # 配置加载与校验
├── models.py                # 数据模型
├── filters.py               # 去重与关键词过滤
├── agent/commands.py        # Issue 指令的解析与分发
├── llm/client.py            # OpenAI 兼容 LLM 客户端：模型分级、用量统计与预算、结构化输出
├── notify/github_issue.py   # 发布日报 Issue、回复评论、添加表情
├── pipelines/daily.py       # 每日推送流水线
├── pipelines/interact.py    # Issue 指令交互流水线
├── profile/anchors.py       # 兴趣描述 → 子方向（兴趣锚点）
├── ranking/                 # 嵌入、向量粗筛、LLM 精排、日报概览
├── reader/                  # 全文获取与章节切分、深读、对比
├── render/                  # 日报和指令回复的 Markdown 渲染与转义
├── sources/                 # arXiv（RSS 与 API）、Semantic Scholar
└── store/data_branch.py     # gleaner-data 分支的读写与提交
```
