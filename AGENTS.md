# AGENTS.md

本文件给后续在本仓库工作的 Codex/Agent 使用。开始修改前先读本文件，再读 `README.md` 和相关模块源码。

## 当前项目状态快照

截至 `2026-05-15`：

- 本地知识库事实源是 `data/jike_collection.db`，飞书文档只是浏览镜像。
- 当前数据规模约为 `2120` 条：`jike=1848`，`aihot=272`。
- KB chunks 约为 `2117`，其中 `jike=1845`，`aihot=272`。
- embeddings 约为 `2116`，仍有少量即刻历史条目未完全补齐。
- 每日任务通过本机 `launchd` 在每天 `10:00` 执行 `python3 -m jike_collection run-daily`。
- 最近的日报链路是可用的，`2026-05-14` 日报已在 `2026-05-15` 补发成功；飞书 webhook 负责发送日报。
- 日报生成已支持 LLM 失败兜底：APIMart/LLM 临时 5xx 或通道不可用时，使用基础摘要模板继续生成。
- 飞书 bot 回调若使用 ngrok，地址可能随进程重启变化。
- 飞书文档镜像仍可能遇到 `too many children in block`，不要把文档镜像当作问答主链路。

如果这些数字对当前任务重要，先运行 `python3 -m jike_collection stats` 和相关 SQLite 查询刷新，不要只依赖本快照。

## 1. 项目目录说明

- `jike_collection/`：主 Python 包，所有 CLI、同步、检索、飞书集成都在这里。
- `jike_collection/cli.py`：命令行入口和子命令注册，`python3 -m jike_collection ...` 最终都会走这里。
- `jike_collection/config.py`：读取 `.env` 和环境变量，集中生成 `Settings`。
- `jike_collection/db.py`：SQLite schema、迁移、查询和写入封装，是本地知识库事实源。
- `jike_collection/models.py`：规范化后的 item、同步摘要、工具函数等数据模型。
- `jike_collection/jike_api.py`：即刻接口客户端和详情补抓逻辑。
- `jike_collection/sources/`：多信息源适配器，目前包含 `jike` 和 `aihot`。
- `jike_collection/kb/`：知识库 chunk、embedding、检索和问答逻辑。
- `jike_collection/llm/`：OpenAI 兼容 LLM/embedding 客户端。
- `jike_collection/digest/`：每日摘要生成逻辑。
- `jike_collection/bot/`：飞书机器人 HTTP 回调服务。
- `jike_collection/feishu_client.py`：飞书开放平台 token 和 API 调用。
- `jike_collection/feishu_doc.py`：飞书文档镜像写入逻辑。
- `jike_collection/feishu_webhook.py`：飞书群机器人 webhook 通知。
- `jike_collection/workflows.py`：组合型工作流，例如即刻同步、飞书文档同步、每日任务。
- `scripts/run_daily_digest.sh`：本机定时任务实际调用的脚本。
- `deploy/launchd/`：macOS `launchd` 定时任务模板。
- `data/`：本地运行数据目录，包含 SQLite、token 缓存、飞书文档状态等，默认不提交。
- `reports/`：本地生成的 Markdown 报告目录，默认不提交。

## 2. 启动命令

常用入口都使用当前目录执行：

```bash
python3 -m jike_collection --help
```

启动飞书机器人回调服务：

```bash
python3 -m jike_collection serve-bot --host 0.0.0.0 --port 8788
```

本地健康检查：

```bash
curl -s http://127.0.0.1:8788/healthz
```

每日主流程：

```bash
python3 -m jike_collection run-daily
```

如果需要同时尝试飞书文档镜像：

```bash
python3 -m jike_collection run-daily --include-doc-sync
```

本机定时任务脚本：

```bash
bash scripts/run_daily_digest.sh
```

浏览器相关操作默认使用 Chrome Canary 的调试端口：

```bash
curl -s http://127.0.0.1:9444/json/version
```

不要操作用户的普通 Chrome 窗口；需要飞书后台、即刻页面或登录态浏览器时，只使用 `Chrome Canary + 127.0.0.1:9444`，除非用户明确改口。

## 3. 其他项目访问本地知识库

其他 Codex 项目需要查询用户的即刻收藏或 AIHOT 本地知识库时，优先调用本项目 CLI。不要直接读写 `data/jike_collection.db`。

推荐问答：

```bash
(cd /Users/dahuang/CascadeProjects/knowledge-auto-update && python3 -m jike_collection ask "问题")
```

推荐关键词搜索：

```bash
(cd /Users/dahuang/CascadeProjects/knowledge-auto-update && python3 -m jike_collection search "关键词" --source all)
```

限定来源：

```bash
(cd /Users/dahuang/CascadeProjects/knowledge-auto-update && python3 -m jike_collection ask "问题" --source jike)
(cd /Users/dahuang/CascadeProjects/knowledge-auto-update && python3 -m jike_collection ask "问题" --source aihot)
```

如果需要把这段能力写入其他仓库的 `AGENTS.md`，只复制上述调用方式即可；不要复制本项目 `.env`、token 或数据库文件。

## 4. 构建/测试/lint 命令

本仓库目前没有 `pyproject.toml`、`requirements.txt`、`pytest` 或 `ruff` 配置；不要凭空引入新的工具链。

基础语法检查：

```bash
python3 -m compileall jike_collection
```

CLI 冒烟检查：

```bash
python3 -m jike_collection --help
python3 -m jike_collection stats
```

数据源同步冒烟检查，可能依赖 `.env`、网络和外部 API：

```bash
python3 -m jike_collection sync --max-pages 1
python3 -m jike_collection aihot-sync --days 1
```

知识库检查，依赖 LLM/embedding 配置：

```bash
python3 -m jike_collection kb-sync --source all --limit 5
python3 -m jike_collection ask "OpenAI 最近发了什么"
```

飞书相关检查，可能会真实发送通知或写文档；执行前确认风险：

```bash
python3 -m jike_collection digest --date YYYY-MM-DD
python3 -m jike_collection digest --date YYYY-MM-DD --send
python3 -m jike_collection feishu-sync-doc --limit 1
```

## 5. 代码风格约定

- 使用 Python 标准库优先，保持当前轻依赖风格。
- 新模块统一使用 `from __future__ import annotations`。
- 使用 `dataclasses` 和显式类型标注表达数据结构，避免在业务逻辑里传递不透明 dict。
- 所有配置从 `load_settings()` / `Settings` 读取，不要在业务模块里散落读取 `.env`。
- SQLite 结构变更放在 `jike_collection/db.py` 的 schema 和迁移逻辑里，并保证旧库可平滑升级。
- 外部 API 客户端保持边界清晰：即刻在 `jike_api.py`/`sources/jike.py`，AIHOT 在 `sources/aihot.py`，飞书在 `feishu_*`，LLM 在 `llm/`。
- CLI 输出保持简洁可读，失败时返回非 0 exit code。
- 网络调用要设置超时，异常要转成清晰的错误摘要，避免静默跳过。
- 写入飞书、发送 webhook、跑全量同步等有外部副作用的命令，改动后优先用小 `--limit` 或指定日期做验证。
- 日报生成应尽量可降级；LLM 调用失败时保留基础摘要 fallback，不要因为单个模型通道异常导致整条日报丢失。
- 代码注释只解释不直观的业务约束，例如 API 限流、飞书 block 限制、SQLite 迁移原因。

## 6. 提交前检查项

提交前至少执行：

```bash
python3 -m compileall jike_collection
python3 -m jike_collection --help
python3 -m jike_collection stats
git status --short
```

如果改了同步逻辑，补充执行：

```bash
python3 -m jike_collection sync --max-pages 1
python3 -m jike_collection aihot-sync --days 1
```

如果改了知识库、LLM 或检索逻辑，补充执行：

```bash
python3 -m jike_collection kb-sync --source all --limit 5
python3 -m jike_collection ask "我之前收藏过 Claude Code 相关内容吗"
```

如果改了每日摘要，补充执行：

```bash
python3 -m jike_collection digest --date YYYY-MM-DD
```

如果改了飞书 bot，补充执行：

```bash
python3 -m jike_collection serve-bot --host 127.0.0.1 --port 8788
curl -s http://127.0.0.1:8788/healthz
```

检查确认：

- 不提交 `.env`、`data/`、`reports/` 或任何 token/API key。
- 不把真实 webhook、App Secret、LLM key 写进 README、AGENTS 或示例代码。
- 不把用户本地运行产物当作源码改动提交。
- 若命令会真实发飞书通知、写飞书文档或改飞书后台配置，先明确告知用户。
- 工作区可能已有用户或其他 Agent 的未提交改动；只提交本次任务相关文件。
- 如果用户要求“推送到 git”，先用 `git status --short` 和 `.gitignore` 确认不会提交本地敏感文件，再提交并通过 SSH remote 推送。

## 7. 禁止随意改动的文件

以下文件和目录包含敏感数据、运行状态或本机调度配置，除非任务明确要求，否则不要修改、删除、格式化或重建：

- `.env`
- `data/`
- `data/jike_collection.db`
- `data/jike_auth.json`
- `data/feishu_user_token.json`
- `data/feishu_doc_state.json`
- `reports/`
- `~/Library/LaunchAgents/com.dahuang.knowledge-auto-update.daily.plist`
- `deploy/launchd/com.dahuang.knowledge-auto-update.daily.plist`
- `scripts/run_daily_digest.sh`

修改这些文件前要先判断影响：

- `.env` 和 `data/*.json` 可能包含长期 token。
- `data/jike_collection.db` 是知识库事实源，删除或重建会丢失同步状态、embedding、日报记录。
- `deploy/launchd/` 和 `scripts/run_daily_digest.sh` 会影响每天 10:00 的自动日报。
- 飞书开放平台回调地址、事件订阅、权限和版本发布属于外部配置，不要在没有明确需求时改动。

## Git 操作约定

- 当前远端使用 SSH：`git@github.com:slideology/knowledge-auto-update.git`。
- 默认分支是 `main`。
- 提交前先检查是否有不属于本次任务的改动；不要回滚用户或其他 Agent 的改动。
- 如果本次任务明确要求同步当前功能状态，可以把相关源码和文档一起提交；否则只提交本次任务涉及的文件。
- 推送前至少跑：

```bash
python3 -m compileall jike_collection
python3 -m jike_collection --help
python3 -m jike_collection stats
git status --short
```
