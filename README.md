# Paper Gleaner

一个 fork 即用、不需要服务器的论文追踪 Agent。

- 写一段研究方向描述即可，Zotero 可选；
- 嵌入粗筛 + LLM 精排，每篇推荐都说明「为什么和你相关」；
- 每日推送发成 GitHub Issue，评论 `/deep`、`/compare` 让 Agent 深读、对比；
- 根据 👍/👎 自动更新兴趣画像，记忆以文件形式存在仓库中；
- 支持邮件、飞书、企业微信推送，以及 Claude Code Skill/MCP；
- 适配 DeepSeek、Qwen 等低价模型。

> 项目处于早期开发阶段：每日日报（Phase 1）、Issue 指令交互（Phase 2）、反馈学习（Phase 3），以及邮件 / 飞书 / 企业微信推送、日报自动深读、Zotero 输入、`/ask`、MCP Server 和 Claude Code Skill（Phase 4）都已可用。总体架构与实施路线见 [docs/architecture.md](docs/architecture.md)。

## 在 GitHub Actions 上使用

1. 在仓库 Settings → Secrets and variables → Actions 中添加 Secret `LLM_API_KEY`；不用 DeepSeek 时再添加 `LLM_BASE_URL`（如 `https://dashscope.aliyuncs.com/compatible-mode/v1`），并在 `config.yaml` 或 Actions variables（`LLM_FAST_MODEL` / `LLM_DEEP_MODEL`）中改模型名；
2. 把 `profile/interests.example.md` 复制为 `profile/interests.md`，用自然语言写下你的研究方向；
3. 把 `config.example.yaml` 复制为 `config.yaml`，至少改一下要关注的 arXiv 分类；
4. 在 Actions 页面打开 **Daily digest**，点 Run workflow 手动运行一次。可以先勾选 dry_run 试跑：只跑流程、不发 Issue；周末和节假日 arXiv 没有新公告时，试运行会改用最近提交的 20 篇论文，照样能检查大模型和嵌入模型是否配置正确。

之后每个工作日北京时间 09:30 自动运行，日报发成带 `daily` 标签的 Issue，排在第一的论文会自动深读、报告折叠在它的评论里（`deep_read.auto_top_m`，设为 0 关闭）；还可以同时推送到[邮件、飞书、企业微信](#推送到邮件飞书企业微信)；每周日晚上 **Weekly preferences** 把一周的反馈总结成「学到的偏好」（见[反馈与记忆](#反馈与记忆)）。运行产生的数据（去重记录、兴趣拆解缓存、每日结果和统计、深读报告、反馈、屏蔽主题、学到的偏好）保存在 `gleaner-data` 分支，不会污染 `main`。

## 推送到邮件、飞书、企业微信

日报发布后，可以把摘要（标题、相关理由、TL;DR）和日报 Issue 的链接推送到邮件、飞书或企业微信，交互仍然在 Issue 里进行。各渠道互不影响：某个渠道失败只在运行结果里给出警告，11:30 的兜底运行会补发失败的渠道，已经发出去的不会重复发送。没有达到 `min_score` 的日子、周末和 dry-run 都不推送。

1. 在仓库 Settings → Secrets and variables → Actions 中添加下面各渠道需要的 Secrets（地址、密码这类密钥只能放在这里，写进 `config.yaml` 会直接报错）；
2. 在 `config.yaml` 中启用对应的渠道，例如 `notify: {feishu: {enabled: true}}`，可以只启用其中一部分；
3. 在 Actions 页面运行 **Daily digest**，勾选 notify_test：只给已启用的渠道各发一条测试消息，不运行日报（本地也可以运行 `gleaner notify-test`）。`gleaner check` 会列出各渠道的配置状态。

**邮件**（`notify.email`，可选 `subject_prefix` 修改邮件标题前缀）

| Secret | 说明 |
| --- | --- |
| `SMTP_HOST` | QQ 邮箱 `smtp.qq.com`，163 邮箱 `smtp.163.com`，Gmail `smtp.gmail.com` |
| `SMTP_PORT` | 可选，默认 465（SSL）；填 587 时走 STARTTLS |
| `SMTP_USER` | 完整的邮箱地址，同时作为发件人 |
| `SMTP_PASSWORD` | 邮箱的**授权码**（Gmail 是应用专用密码），不是登录密码 |
| `EMAIL_TO` | 收件人，多个地址用逗号分隔 |

- QQ 邮箱：网页版「设置 → 账号」，找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」，开启 IMAP/SMTP 服务，按提示验证后生成授权码；
- 163 邮箱：网页版「设置 → POP3/SMTP/IMAP」，开启 IMAP/SMTP 服务，按提示获取授权码；
- Gmail：先开启两步验证，再在 Google 账号的「应用专用密码」中生成一个。

（菜单名称以邮箱当前的版本为准。）

**飞书**（`notify.feishu`，可选 `max_papers`，默认卡片里最多列 10 篇）

1. 在飞书群中打开「设置 → 群机器人 → 添加机器人 → 自定义机器人」，复制 Webhook 地址，添加为 Secret `FEISHU_WEBHOOK`；
2. 在机器人的「安全设置」中勾选「签名校验」，把显示的密钥添加为 Secret `FEISHU_SECRET`（推荐）。不要用 IP 白名单：GitHub Actions 的出口 IP 不固定；如果用自定义关键词，把 `Paper Gleaner` 设为关键词。

**企业微信**（`notify.wecom`，可选 `max_papers`，默认 5 篇：消息最多 4096 字节）

在群聊中打开「… → 添加群机器人 → 新建」，复制 Webhook 地址，添加为 Secret `WECOM_WEBHOOK`。

> Webhook 地址相当于密码，拿到它的人就能往群里发消息。Paper Gleaner 只从 Secrets 读取它，不会打印到日志或写进运行结果；如果不小心泄露了，在群机器人设置中重置地址。

## Zotero（可选）

可以把 Zotero 文库作为兴趣锚点之一，和兴趣描述一起参与粗筛：和文库里的文献越像，粗筛分数越高（权重 `ranking.weights.zotero`，默认 0.5）。它只是补充，兴趣描述仍然必需。

1. **找到 ID**：个人文库登录 zotero.org 后打开 [API Keys 页面](https://www.zotero.org/settings/keys)，页面上的「Your userID for use in API calls is …」就是用户 ID；群组文库在 zotero.org 打开群组页面，地址 `https://www.zotero.org/groups/<数字>/...` 中的数字就是群组 ID。把它添加为 Secret `ZOTERO_ID`；
2. **申请 API Key**：在同一页面点「Create new private key」，勾选「Allow library access」（只读即可）；群组文库还要在「Default Group Permissions」或对应群组中给只读权限。保存后把 Key 添加为 Secret `ZOTERO_KEY`；
3. **配置**：在 `config.yaml` 中：

```yaml
profile:
  zotero:
    enabled: true
    library_type: user             # 群组文库填 group
    collections: [Detection]       # 可选：限定 collection，写名称或 key；不写时使用整个文库
    include_subcollections: true   # 是否包含子 collection
    max_items: 200                 # 最多取多少篇作为锚点
```

- 会跳过附件、笔记、回收站里的条目和重复项（按 DOI、arXiv ID、标题判断）。超过 `max_items` 时按加入文库的时间从新到旧、在各 collection 之间轮流取；
- collection 名称有重名时会报错，请改用 key：在 zotero.org 网页版打开这个 collection，地址栏中 `/collections/` 后面的 8 位字符就是它的 key；
- **隐私**：文库内容只缓存在 GitHub Actions 的缓存里（`~/.cache/paper-gleaner/zotero`，文库没有变化时不重新下载），不会提交到 `gleaner-data` 等任何分支，Issue、日志和推送消息里只显示数量，例如「Zotero：使用 200 / 1342 篇」。注意 Actions 缓存能被本仓库的 workflow 读取，包括你批准运行的 Pull Request 的 workflow；
- 读取失败时改用上次的缓存，没有缓存就这次不用 Zotero，日报照常发出；
- 怎么看效果：`gleaner daily --dry-run` 打印的粗筛分项中会出现「+ Zotero」，日报「本次运行」里显示实际使用的篇数。

## 在 Issue 中交互

在日报 Issue 下评论指令，Interact workflow 会运行 Agent 并回复（要等 Actions 启动和模型生成，一般需要一到几分钟）。收到指令时评论会被加上 👀，完成后加 🚀，出错时加 😕 并回复原因。

| 指令 | 示例 | 说明 |
| --- | --- | --- |
| `/deep <ref>` | `/deep 3`、`/deep 2410.12345` | 读全文（arXiv HTML，没有时读 PDF），生成深读报告：一句话总结、问题与动机、方法要点、实验、代码与资源、与你研究的关系、局限与可追问点。报告缓存在 `gleaner-data` 的 `reports/`，再次 `/deep` 直接返回缓存，`/deep 3 --force` 重新生成 |
| `/compare <ref> <ref> ...` | `/compare 3 5` | 多篇论文（最多 5 篇）横向对比问题、方法、实验设置、结果和代码 |
| `/compare <ref>` | `/compare 3` | 单篇论文与相关工作（Semantic Scholar 的参考文献和推荐论文）对比 |
| `/more [n]` | `/more`、`/more 10` | 展开这份日报中打过分但没有推送的候选，默认 5 篇，不调用 LLM |
| `/like <ref> [原因]` | `/like 3 有开源代码，方法简单` | 喜欢这篇论文：之后的日报会多推荐类似的，原因每周汇总进学到的偏好 |
| `/dislike <ref> [原因]` | `/dislike 5 纯 benchmark` | 不喜欢这篇论文：之后少推荐类似的；会覆盖之前对同一篇的 `/like` |
| `/mute <主题>` | `/mute 医学影像` | 屏蔽一个主题，之后把这类论文排到后面；只写 `/mute` 时列出已屏蔽的主题 |
| `/unmute <主题>` | `/unmute 医学影像` | 取消屏蔽 |
| `/ask <问题>` | `/ask 今天哪篇的方法最适合用在单源域泛化的检测器上？` | 自由提问：deep 档模型自己决定查当天日报、读全文、检索 arXiv 和你的历史，最多 8 步后作答，回复中列出提到的论文的链接，并注明步数和 token 用量 |
| `/help` | | 列出可用指令 |

- `<ref>` 可以是本 Issue 中的序号、arXiv ID（可带版本号，如 `2410.12345v2`）或 arXiv 链接。序号按 Issue 对应的那一天解析，在旧日报下评论同样有效；`/more` 列出的候选接着日报往下编号（日报有 6 篇时，候选从 7 开始）。
- 只响应仓库所有者和协作者的评论，Issue 必须带 `gleaner` 标签（日报自动带上）。
- `/deep`、`/compare` 使用 deep 档模型，单条指令的输入不超过 `deep_read.max_input_tokens`（默认 30000）。可选添加 Secret `S2_API_KEY` 提高 Semantic Scholar 的请求限额；没有时偶尔会被限流，回复中会注明缺少相关工作部分。
- 外部服务失败时指令不会失败：拿不到全文就基于摘要分析，LLM 调用失败就先回复摘要和代码链接。
- 日报自动深读过的论文（默认排在第一的那篇），报告折叠在它的评论里，`/deep` 直接返回这份缓存。
- `/ask` 的问题可以写多行；它只读当前 Issue 中你和协作者的评论（以及机器人自己的回复）作为上下文，工具全部只读。步数和单条 token 上限在 `config.yaml` 的 `ask` 中设置（默认 8 步、60000 token），到达上限时会基于已经查到的信息作答并注明。

## 反馈与记忆

Paper Gleaner 会根据你的反馈调整推荐，所有记忆都以文件形式存在 `gleaner-data` 分支，可以查看、手改、回滚：

| 反馈 | 怎么给 | 作用 |
| --- | --- | --- |
| 👍 / 👎 | 给日报里的论文评论点表情 | ±1。GitHub 的表情不会触发 Actions，下一次运行日报时同步最近 7 天的日报；**只统计仓库所有者的表情**，协作者和其他人点的不算；撤销表情也会同步 |
| `/like`、`/dislike` | 在日报 Issue 下评论 | ±2，立即写入；带原因的反馈最有价值 |
| `/deep`、`/compare` | 同上 | 读过、比过的论文记 +0.5（每篇最多一次） |
| `/mute`、`/unmute` | 同上 | 屏蔽或取消屏蔽一个主题 |

- **每天（向量层）**：同一篇论文的反馈合成一个分数（表情取最新状态，`/like`/`/dislike` 取最新一条，`/deep`/`/compare` 最多 +0.5，三者相加），正的作为正反馈锚点、负的和屏蔽主题作为负向锚点，参与粗筛打分；反馈越新、越多、越一致，影响越大（时间衰减的半衰期是 `ranking.feedback_half_life_days`，默认 90 天）。没有任何反馈时和以前完全一样。
- **每周（语义层）**：Weekly preferences 让 fast 模型把新反馈和原因增量总结进 `gleaner-data` 上的 `profile/learned_preferences.md`，例如「偏好有开源代码的工作（3 次 /like 提到）」，精排时会参考它。没有新反馈时不调用 LLM；你可以直接编辑或删除其中的条目，下次总结会在你修改后的内容上调整。`profile/interests.md` 永远由你自己写，程序不会改它。
- **怎么看效果**：日报 Issue 末尾「本次运行」里有反馈统计（👍、👎、`/like`、`/dislike` 各多少篇，正负反馈锚点和屏蔽主题各多少）；`gleaner daily --dry-run` 会打印每篇论文粗筛分数的分项（兴趣 + 正反馈 − 负向）；本地运行 `gleaner feedback` 可以查看聚合后的反馈、屏蔽的主题和学到的偏好。

## 在 Claude Code 中使用

两种方式都在本地运行、读取仓库的 `gleaner-data` 分支，可以任选其一，也可以都用。下面的 `<仓库路径>` 是你 clone 下来的 Paper Gleaner 仓库；用到 `deep`、`compare`、`ask` 时需要在仓库根目录的 `.env` 中写好 `LLM_API_KEY`（见[本地开发](#本地开发)）。

**Skill（不需要额外进程）**：仓库自带 `.claude/skills/paper-gleaner/SKILL.md`。在仓库目录中打开 Claude Code，直接问「今天推了什么」「深读 2410.12345，再结合我当前的代码库想想怎么用」，Claude Code 会调用 `gleaner digest`、`gleaner deep` 等命令。想在其它项目里也能用，把这个目录复制到 `~/.claude/skills/paper-gleaner/`，并在其中写明仓库路径。

**MCP Server**：

```bash
cd <仓库路径> && uv sync --extra mcp     # mcp 是可选依赖，GitHub Actions 中不会安装
claude mcp add paper-gleaner -- uv run --directory <仓库路径> --extra mcp gleaner-mcp
```

注册后在 Claude Code 中可以直接问「今天推了什么」「帮我找找之前推过的域泛化论文」。提供的工具：

| 工具 | 说明 |
| --- | --- |
| `get_digest(date?)` | 某一天的推送，默认最近一份 |
| `search_history(query)` | 用英文关键词在推送过的、有反馈的论文中检索 |
| `deep_read(arxiv_id, force?)` | 深读，已有报告时直接返回缓存（不写缓存） |
| `compare(ids)` | 对比论文；一篇时与相关工作对比 |
| `give_feedback(arxiv_id, signal, note?)` | 记下 `like` / `dislike`（可附原因），**会提交并推送到 gleaner-data** |
| `get_profile()` | 兴趣描述、学到的偏好和屏蔽的主题 |

- 每次调用前先同步远端的 `gleaner-data`（本地 worktree 在 `.gleaner-data`，`--data-dir` 可以改）；同步失败时用本地已有的数据，并在结果中注明；
- `give_feedback` 需要这个本地 clone 能推送到 GitHub（`git push` 能成功，例如用 SSH 或配置了凭据的 HTTPS）。推送失败时会明确告诉你这条反馈没有保存；
- 只读的工具不会修改任何数据。

## 本地开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                                   # 安装依赖
cp config.example.yaml config.yaml        # 按需修改
cp profile/interests.example.md profile/interests.md

export LLM_API_KEY=sk-...                 # 可选：LLM_BASE_URL 覆盖配置中的 base_url
uv run gleaner check --ping-llm           # 检查配置，并验证 API Key 与模型名；也列出推送渠道和 Zotero 的配置状态
uv run gleaner daily --dry-run            # 完整跑一遍：抓取、筛选、精排、自动深读，打印结果但不发布、不推送
uv run gleaner notify-test                # 给已启用的推送渠道各发一条测试消息；--channel feishu 只测一个

uv run gleaner deep 2410.12345            # 本地深读，报告打印到终端（读取但不写入报告缓存）
uv run gleaner deep 1 --day 2026-10-25    # 序号按某一天的日报解析，默认最近一份
uv run gleaner compare 1 2                # 本地对比；只给一篇时与相关工作对比
uv run gleaner digest                     # 打印最近一份日报；--day 2026-10-25 指定日期，--all 连同候选
uv run gleaner ask "今天哪篇最适合我的检测器？"   # 本地 /ask，回答打印到终端（不写数据）
uv run gleaner handle-comment --event event.json --dry-run   # 用 issue_comment 事件文件调试，只打印回复
uv run gleaner feedback --days 30         # 查看聚合后的反馈、屏蔽的主题和学到的偏好（只读）
uv run gleaner summarize-preferences --dry-run   # 把新反馈总结成学到的偏好，只打印不提交

uv run ruff check && uv run ruff format --check
uv run pytest
```

本地也可以把 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 或 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 写入仓库根目录的 `.env`；默认优先使用 DeepSeek，设置 `LLM_PROVIDER=openai` 可切换到 OpenAI。模型名可用 `DEEPSEEK_FAST_MODEL` / `DEEPSEEK_DEEP_MODEL` 或对应的 `OPENAI_*` 变量覆盖。GitHub Actions 中继续使用 `LLM_API_KEY` 和 `LLM_BASE_URL` Secrets。

## 目录结构

```text
gleaner/
├── cli.py                   # 命令行入口
├── mcp_server.py            # MCP Server（gleaner-mcp）
├── config.py                # 配置加载与校验
├── models.py                # 数据模型
├── filters.py               # 去重与关键词过滤
├── agent/commands.py        # Issue 指令的解析与分发
├── agent/tools.py           # Agent 的只读工具（/ask 和 MCP Server 共用）
├── agent/loop.py            # /ask 的 JSON 协议工具调用循环
├── llm/client.py            # OpenAI 兼容 LLM 客户端：模型分级、用量统计与预算、结构化输出
├── llm/prompt.py            # token 估算、不可信内容的包裹
├── notify/github_issue.py   # 发布日报 Issue、回复评论、添加和读取表情
├── notify/                  # 邮件、飞书、企业微信推送（base.py 接口，dispatch.py 分发）
├── redaction.py             # 密钥脱敏
├── pipelines/daily.py       # 每日推送流水线
├── pipelines/interact.py    # Issue 指令交互流水线
├── pipelines/feedback.py    # 日报第 0 步：同步 👍/👎 表情
├── profile/anchors.py       # 兴趣描述 → 子方向（兴趣锚点）
├── profile/memory.py        # 反馈读写与聚合、学到的偏好
├── profile/muted.py         # 屏蔽主题（/mute、/unmute）
├── ranking/                 # 嵌入、向量粗筛、LLM 精排、日报概览
├── reader/                  # 全文获取与章节切分、深读、对比
├── render/                  # 日报和指令回复的 Markdown 渲染与转义
├── sources/                 # arXiv（RSS 与 API）、Semantic Scholar、Zotero
└── store/data_branch.py     # gleaner-data 分支的读写与提交
```
