# Paper Gleaner

一个 fork 即用、不需要服务器的论文追踪 Agent。

- 写一段研究方向描述即可，Zotero 可选；
- 嵌入粗筛 + LLM 精排，每篇推荐都说明「为什么和你相关」；
- 每日推送发成 GitHub Issue，评论 `/deep`、`/compare` 让 Agent 深读、对比；
- 根据 👍/👎 自动更新兴趣画像，记忆以文件形式存在仓库中；
- 支持邮件、飞书、企业微信推送，以及 Claude Code Skill/MCP；
- 适配 DeepSeek、Qwen 等低价模型。

> 项目处于早期开发阶段：每日日报（Phase 1）已可用，Issue 交互、反馈学习等功能还在开发中。总体架构与实施路线见 [docs/architecture.md](docs/architecture.md)。

## 在 GitHub Actions 上使用

1. 在仓库 Settings → Secrets and variables → Actions 中添加 Secret `LLM_API_KEY`；不用 DeepSeek 时再添加 `LLM_BASE_URL`（如 `https://dashscope.aliyuncs.com/compatible-mode/v1`），并在 `config.yaml` 或 Actions variables（`LLM_FAST_MODEL` / `LLM_DEEP_MODEL`）中改模型名；
2. 把 `profile/interests.example.md` 复制为 `profile/interests.md`，用自然语言写下你的研究方向；
3. 把 `config.example.yaml` 复制为 `config.yaml`，至少改一下要关注的 arXiv 分类；
4. 在 Actions 页面打开 **Daily digest**，点 Run workflow 手动运行一次。可以先勾选 dry_run 试跑：只跑流程、不发 Issue；周末和节假日 arXiv 没有新公告时，试运行会改用最近提交的 20 篇论文，照样能检查大模型和嵌入模型是否配置正确。

之后每个工作日北京时间 09:30 自动运行，日报发成带 `daily` 标签的 Issue。运行产生的数据（去重记录、兴趣拆解缓存、每日结果和统计）保存在 `gleaner-data` 分支，不会污染 `main`。

## 本地开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                                   # 安装依赖
cp config.example.yaml config.yaml        # 按需修改
cp profile/interests.example.md profile/interests.md

export LLM_API_KEY=sk-...                 # 可选：LLM_BASE_URL 覆盖配置中的 base_url
uv run gleaner check --ping-llm           # 检查配置，并验证 API Key 与模型名
uv run gleaner daily --dry-run            # 完整跑一遍：抓取、筛选、精排，打印结果但不发布

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
├── llm/client.py            # OpenAI 兼容 LLM 客户端：模型分级、用量统计与预算、结构化输出
├── notify/github_issue.py   # 发布日报 Issue
├── pipelines/daily.py       # 每日推送流水线
├── profile/anchors.py       # 兴趣描述 → 子方向（兴趣锚点）
├── ranking/                 # 嵌入、向量粗筛、LLM 精排、日报概览
├── render/markdown.py       # Issue 的 Markdown 渲染
├── sources/arxiv.py         # arXiv RSS 抓取
└── store/data_branch.py     # gleaner-data 分支的读写与提交
```
