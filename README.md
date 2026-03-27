# Jike Collection Knowledge Base

一个本地工具，用来把你自己的即刻收藏持续同步到 SQLite，并提供：

- 增量抓取
- 自动刷新 access token
- 中文/英文全文搜索
- 按主题、来源、作者做基础分析
- 生成 Markdown 报告

整个项目只依赖 Python 标准库，适合长期本地运行。

## 最近进展

最近这轮工作，已经把项目从“本地脚本抓收藏”推进成了一条可持续运行的知识库链路：

- 打通了即刻收藏抓取，支持增量同步、全量校准、详情补抓和 token 自动刷新
- 建好了本地 SQLite + FTS 搜索，可直接搜索历史收藏正文、作者、主题和链接
- 增加了 Markdown 分析报告能力，能按时间窗口输出收藏洞察
- 接入了飞书 OAuth、普通飞书文档写入和群机器人通知
- 新增了 `run-daily`、`feishu-backfill`、`feishu-sync-doc` 等命令
- 补上了通知链路，现在 `feishu-backfill`、`feishu-sync-doc`、`run-daily` 跑完都会自动发飞书群通知

## 当前状态

截至 2026-03-27，当前项目和数据状态如下：

- Git 状态：本地与远端 `main` 已同步
- 最近一次即刻同步：`2026-03-20`
- 即刻收藏总数：`1618`
- 当前有效收藏：`1618`
- 图片类收藏：`913`
- 视频类收藏：`52`
- 音频类收藏：`9`
- 飞书文档已写入：`1262`
- 飞书文档待重试：`356`
- 最近一次飞书投递：`2026-03-20`，结果为 `partial_failure`

当前已经创建好的飞书知识库文档：

- [即刻收藏知识库](https://feishu.cn/docx/XE1qdRh1soW8DzxPDeGcHiB6nDb)

## 已知问题

- 飞书普通文档存在单个 block 子节点数量限制，当前回填过程中会命中 `too many children in block`
- 因为这个限制，历史收藏还没有 100% 全量写入飞书文档，仍有一部分失败记录等待重试
- 飞书文档接口存在限流，批量回填时需要更细的分片和节流策略
- 2026-03-20 之后还没有再次执行同步，因此当前库内最新收藏时间仍停在 `2026-03-20T07:13:56.685Z`

## 下一步待办

- 优先解决飞书文档的 `too many children in block`，重构文档层级，补齐剩余 `356` 条失败收藏
- 给飞书回填增加更稳的分片、退避和失败重试策略，降低 `429` 对整批任务的影响
- 再跑一次即刻同步，确认 `2026-03-20` 之后是否有新增收藏进入本地库
- 跑通一轮完整的 `run-daily`，验证“即刻增量 -> 飞书写入 -> 群通知”整条链路
- 接一个稳定的定时调度，让 `run-daily` 每天自动执行
- 视使用体验补一个本地网页搜索界面，而不只是命令行搜索

## 1. 准备登录态

在你已经登录的即刻网页里打开浏览器控制台，执行：

```js
localStorage.getItem('JK_ACCESS_TOKEN')
localStorage.getItem('JK_REFRESH_TOKEN')
```

把结果填到当前目录的 `.env` 里：

```bash
cp .env.example .env
```

## 2. 首次同步

```bash
python3 -m jike_collection sync --full
```

首次跑完后，程序会把最新 token 缓存在 `data/jike_auth.json`。之后如果 access token 过期，会自动用 refresh token 刷新。

## 3. 搜索自己的收藏

```bash
python3 -m jike_collection search OpenAI
python3 -m jike_collection search 浏览器功能
python3 -m jike_collection search "自动化 agent"
```

## 4. 生成分析报告

```bash
python3 -m jike_collection report --days 30
```

报告会输出到 `reports/` 目录，包含：

- 收藏总量和最近周期统计
- 高频来源域名
- 高频主题 / 作者
- 值得二次整理的候选内容
- 按主题分组的线索

## 5. 推荐的长期运行方式

日常增量同步：

```bash
python3 -m jike_collection sync
```

每周做一次全量校准，顺便清理已取消收藏的条目：

```bash
python3 -m jike_collection sync --full
```

你也可以把下面这条命令放进定时任务：

```bash
python3 -m jike_collection sync && python3 -m jike_collection report --days 30
```

macOS 上可以用 `launchd`、`crontab`，或者 Codex 自动化来跑。

## 6. 飞书集成

### 6.1 准备环境变量

在 `.env` 里补充：

```bash
FEISHU_WEBHOOK_URL=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_REDIRECT_URI=http://127.0.0.1:8787/callback
```

### 6.2 一次性做飞书 OAuth 授权

```bash
python3 -m jike_collection feishu-auth --open-browser
```

如果你已经手动拿到了授权 code，也可以：

```bash
python3 -m jike_collection feishu-auth --code <oauth_code>
```

### 6.3 首次把历史收藏回填到飞书文档

```bash
python3 -m jike_collection feishu-backfill
```

首次会自动创建一份普通飞书文档，并把文档状态保存到 `data/feishu_doc_state.json`。

### 6.4 每日增量同步 + 飞书通知

```bash
python3 -m jike_collection run-daily
```

这条命令会：

1. 增量同步即刻收藏到 SQLite
2. 把未同步过的收藏追加到飞书文档
3. 给飞书群机器人发一条“今日是否有更新”的通知

另外这两条命令现在也会在执行结束后自动发飞书通知：

- `python3 -m jike_collection feishu-backfill`
- `python3 -m jike_collection feishu-sync-doc`

## 7. 常用命令

```bash
python3 -m jike_collection sync
python3 -m jike_collection sync --full
python3 -m jike_collection search "MCP"
python3 -m jike_collection report --days 7
python3 -m jike_collection stats
python3 -m jike_collection feishu-auth --open-browser
python3 -m jike_collection feishu-backfill
python3 -m jike_collection feishu-sync-doc
python3 -m jike_collection run-daily
```

## 8. 数据文件

- SQLite 数据库：`data/jike_collection.db`
- token 缓存：`data/jike_auth.json`
- 飞书用户 token：`data/feishu_user_token.json`
- 飞书文档状态：`data/feishu_doc_state.json`
- Markdown 报告：`reports/`

## 9. 说明

- 当前实现依赖即刻网页端私有接口 `POST /1.0/collections/list`
- 这不是公开 API，所以未来如果即刻改接口，抓取逻辑可能需要调整
- 工具默认只处理“当前登录账号自己的收藏”
- 飞书文档采用普通文档，不是电子表格
- 历史收藏写入飞书后默认不因为“取消收藏”而从文档中删除
