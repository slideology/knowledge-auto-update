# Knowledge Auto Update

一个多信息源个人知识库项目。当前已经接入即刻收藏、AIHOT 公开热点、SQLite 本地知识库、OpenAI 兼容 LLM、飞书日报通知、飞书文档镜像和飞书机器人问答。

当前架构里，**SQLite + KB chunks/embeddings 是事实源和问答主链路**；飞书文档只是方便人工浏览的镜像，不承担检索问答主链路。

## 当前状态

截至 `2026-05-14`，本地库当前状态如下：

- 总条目：`2060`
- 即刻收藏：`1838`
- AIHOT 条目：`222`
- KB chunks：`2057`
- 即刻 chunks：`1835 / 1838`
- AIHOT chunks：`222 / 222`
- embeddings：`2056`
- 飞书文档镜像已同步即刻收藏：`1262`
- 最新即刻收藏时间：`2026-05-13T06:26:59.483Z`
- 最新日报：`2026-05-13`，状态 `success`，飞书 webhook 已发送

已完成能力：

- 即刻收藏同步到 SQLite，支持增量、全量、详情补抓和 token 自动刷新。
- AIHOT 公开 API 接入，支持 selected 流同步和按日报回填。
- 统一 `items` 表支持 `jike` 与 `aihot` 多源共存。
- KB chunk / embedding / digest run / delivery run 等本地表结构已经落地。
- 本地全文搜索支持 `--source jike|aihot|all`。
- 本地 `ask` 命令支持多源问答路由。
- 每天 `10:00` 通过本机 `launchd` 自动执行前一天日报。
- 飞书群 webhook 通知链路可用。
- 飞书机器人服务入口 `serve-bot` 已接入基础事件处理和消息回复。
- 飞书文档镜像保留，但不是问答主链路。

当前已知限制：

- 即刻还有 `3` 条未生成 chunk，`1` 条缺 embedding；最近 `kb-sync` 会以 `partial_failure` 收尾，但大部分问答和日报不受影响。
- 飞书文档镜像仍保留旧问题：单个 block children 过多时会命中 `too many children in block`。
- 飞书机器人目前按明文事件回调实现；如果飞书后台启用事件加密，需要补解密支持。
- 当前 bot 公网回调如果使用 ngrok，地址会变化；长期稳定运行建议改为固定域名或 Cloudflare Worker。
- 当前还没有正式接入 Twitter / Reddit，但数据模型和 source adapter 已按可扩展方式设计。

## 环境变量

复制配置模板：

```bash
cp .env.example .env
```

最少需要补：

```bash
JIKE_ACCESS_TOKEN=
JIKE_REFRESH_TOKEN=
FEISHU_WEBHOOK_URL=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_REDIRECT_URI=http://127.0.0.1:8787/callback
```

如果要启用知识库问答、embedding 和摘要增强，再补：

```bash
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_CHAT_MODEL=
LLM_EMBEDDING_MODEL=
LLM_TIMEOUT=60
```

如果要启用飞书机器人，再补：

```bash
FEISHU_BOT_APP_ID=
FEISHU_BOT_APP_SECRET=
FEISHU_EVENT_VERIFY_TOKEN=
FEISHU_EVENT_ENCRYPT_KEY=
FEISHU_ALLOWED_OPEN_ID=
BOT_PUBLIC_BASE_URL=
```

## 即刻登录态

在已登录的即刻网页控制台执行：

```js
localStorage.getItem('JK_ACCESS_TOKEN')
localStorage.getItem('JK_REFRESH_TOKEN')
```

把结果写进 `.env`。后续同步会优先使用本地 token 状态文件，并在可用时自动刷新。

## 数据同步

首次同步即刻收藏：

```bash
python3 -m jike_collection sync --full
```

日常增量同步即刻收藏：

```bash
python3 -m jike_collection sync
```

同步 AIHOT 公开流：

```bash
python3 -m jike_collection aihot-sync --days 2
```

回填 AIHOT 近几天日报：

```bash
python3 -m jike_collection aihot-sync --backfill-days 7
```

## 知识库索引

配置好 LLM 后，首次全量索引：

```bash
python3 -m jike_collection kb-reindex --source all
```

之后日常只需要增量：

```bash
python3 -m jike_collection kb-sync --source all
```

可用 `--source jike|aihot|all` 控制索引范围，调试时可加 `--limit`。

## 搜索和问答

关键词搜索：

```bash
python3 -m jike_collection search "OpenAI" --source aihot
python3 -m jike_collection search "浏览器功能" --source jike
python3 -m jike_collection search "agent" --source all
```

本地问答：

```bash
python3 -m jike_collection ask "OpenAI 最近发了什么"
python3 -m jike_collection ask "我之前收藏过 Claude Code 相关内容吗"
python3 -m jike_collection ask "我收藏里和最近 AI 热点重合的主题是什么" --source all
```

如果没有配置 LLM，`ask` 会退回成候选命中列表模式。

## 每日摘要

生成前一天摘要：

```bash
python3 -m jike_collection digest
```

生成指定日期摘要：

```bash
python3 -m jike_collection digest --date 2026-05-13
```

生成后发送到飞书群：

```bash
python3 -m jike_collection digest --send
```

主入口：

```bash
python3 -m jike_collection run-daily
```

`run-daily` 会执行：

1. 即刻增量同步
2. AIHOT 增量同步
3. KB 增量索引
4. 前一天摘要生成
5. 飞书群通知

如果还要尝试飞书文档镜像：

```bash
python3 -m jike_collection run-daily --include-doc-sync
```

## 本机定时任务

项目附带一个 `launchd` 模板：

```text
deploy/launchd/com.dahuang.knowledge-auto-update.daily.plist
```

执行脚本：

```text
scripts/run_daily_digest.sh
```

默认每天 `10:00` 执行：

```bash
python3 -m jike_collection run-daily
```

日志默认写到：

```text
~/Library/Logs/knowledge-auto-update/
```

## 飞书集成

文档 OAuth 授权：

```bash
python3 -m jike_collection feishu-auth --open-browser
```

即刻收藏镜像到飞书文档：

```bash
python3 -m jike_collection feishu-backfill
python3 -m jike_collection feishu-sync-doc
```

这条链路默认只同步 `jike`，不会把 AIHOT 写进飞书文档。

启动飞书机器人：

```bash
python3 -m jike_collection serve-bot --host 0.0.0.0 --port 8788
```

健康检查：

```bash
curl -s http://127.0.0.1:8788/healthz
```

飞书开放平台事件回调地址：

```text
POST {BOT_PUBLIC_BASE_URL}/feishu/events
```

机器人默认只响应 `.env` 里的 `FEISHU_ALLOWED_OPEN_ID`。

## 常用命令

```bash
python3 -m jike_collection --help
python3 -m jike_collection stats
python3 -m jike_collection sync
python3 -m jike_collection sync --full
python3 -m jike_collection aihot-sync --days 2
python3 -m jike_collection aihot-sync --backfill-days 7
python3 -m jike_collection kb-sync --source all
python3 -m jike_collection kb-reindex --source all
python3 -m jike_collection search "OpenAI" --source all
python3 -m jike_collection ask "OpenAI 最近发了什么"
python3 -m jike_collection digest --send
python3 -m jike_collection serve-bot
python3 -m jike_collection run-daily
```

## 数据文件

- SQLite：`data/jike_collection.db`
- 即刻 token 缓存：`data/jike_auth.json`
- 飞书用户 token：`data/feishu_user_token.json`
- 飞书文档状态：`data/feishu_doc_state.json`
- Markdown 报告：`reports/`

这些文件默认不提交到 Git。

## 浏览器约定

需要操作登录态网页时，默认使用 Chrome Canary 的调试端口：

```bash
curl -s http://127.0.0.1:9444/json/version
```

不要随意操作用户正在使用的普通 Chrome 窗口。

## 提交前检查

最小检查：

```bash
python3 -m compileall jike_collection
python3 -m jike_collection --help
python3 -m jike_collection stats
git status --short
```

如果改了同步、KB、日报或飞书相关逻辑，按影响范围补跑对应命令，优先使用 `--limit` 或指定日期，避免误发通知或大批量写文档。

## 后续待办

- 修掉 KB 索引里残留的 `3` 条 chunk 和 `1` 条 embedding 尾巴。
- 修飞书文档 `too many children in block`，把剩余即刻收藏镜像补齐。
- 给飞书机器人补事件加密支持。
- 将 bot 回调从临时 ngrok 地址迁到固定公网入口。
- 接入 Twitter / Reddit source adapter。
- 为外链正文增加可选抓取层。
