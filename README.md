# Paper Gleaner

一个 fork 即用、不需要服务器的论文追踪 Agent。

- 写一段研究方向描述即可，Zotero 可选；
- 嵌入粗筛 + LLM 精排，每篇推荐都说明「为什么和你相关」；
- 每日推送发成 GitHub Issue，评论 `/deep`、`/compare` 让 Agent 深读、对比；
- 根据 👍/👎 自动更新兴趣画像，记忆以文件形式存在仓库中；
- 支持邮件、飞书、企业微信推送，以及 Claude Code Skill/MCP；
- 适配 DeepSeek、Qwen 等低价模型。

> 项目处于早期开发阶段（Phase 0 已完成），总体架构与实施路线见 [docs/architecture.md](docs/architecture.md)。

## 本地开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                                   # 安装依赖
cp config.example.yaml config.yaml        # 按需修改
cp profile/interests.example.md profile/interests.md

export LLM_API_KEY=sk-...                 # 可选：LLM_BASE_URL 覆盖配置中的 base_url
uv run gleaner check --ping-llm           # 检查配置，并验证 API Key 与模型名
uv run gleaner daily --dry-run            # 抓取今天的 arXiv 新论文并打印

uv run ruff check && uv run ruff format --check
uv run pytest
```

## 目录结构

```text
gleaner/
├── cli.py              # 命令行入口
├── config.py           # 配置加载与校验
├── models.py           # 数据模型
├── llm/client.py       # OpenAI 兼容 LLM 客户端：模型分级、用量统计与预算、结构化输出
├── pipelines/daily.py  # 每日推送流水线
└── sources/arxiv.py    # arXiv RSS 抓取
```
