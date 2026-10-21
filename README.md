# tax-analysis-api

把税务问题分析做成 HTTP 接口：客户端提交问题、法规或材料，拿到判型、依据
覆盖、检索结果、法规正文、算式、类案与检查件；耗时较长的整题分析可以提交
为异步任务，再轮询结果。

适合前端工作台、案件/合同管理系统、脚本或其他语言的服务调用。默认只监听
回环地址，不内置认证与 TLS。

## 快速开始

```bash
cd tax-analysis-api
python -m pip install -e .
tax-analysis-api
```

服务默认监听 `http://127.0.0.1:8000`。另开一个终端确认：

```bash
curl -s http://127.0.0.1:8000/v1/health
```

返回的 `data.status` 为 `"ok"`，同时给出 `engine_root`、税种数、骨架数与
模板数。提交一个问题：

```bash
printf '%s' '{"question":"研发费用加计扣除比例是多少"}' > body.json
curl -s -X POST http://127.0.0.1:8000/v1/classifications \
  -H 'Content-Type: application/json' --data-binary @body.json
```

`body.json` 必须是 UTF-8。Windows 终端把中文直接放进 `-d` 时可能按本地
编码发送，服务端报 `There was an error parsing the body`；用文件传请求体
可以避开这个问题。Windows PowerShell 5.1 的 `curl` 是
`Invoke-WebRequest` 的别名，不认 `-s`、`-X`，请改用 `curl.exe`；
`body.json` 用编辑器保存为 UTF-8（无 BOM）。

返回（节选）：

```json
{
  "data": {
    "type": {"type": "lookup", "label": "政策查询", "confidence": 0.85},
    "context": {"missing": ["time", "entity", "place", "scale"]},
    "must_answer": "结论 + 效力位阶 + 文号 + 生效时点"
  }
}
```

Swagger 在 `http://127.0.0.1:8000/docs`。`TAX_API_HOST`、`TAX_API_PORT`
改监听地址；PowerShell 用 `$env:TAX_API_PORT='9000'; tax-analysis-api`，
Git Bash 用 `TAX_API_PORT=9000 tax-analysis-api`。脚本不在 `PATH` 时，
用 `python -m tax_engine_api.app` 启动。

从源码开发时不必安装，在仓库根目录运行：

```bash
PYTHONPATH=src python -m uvicorn tax_engine_api.app:app --host 127.0.0.1 --port 8000
```

## 环境与配置

- Python ≥ 3.10；依赖见 `pyproject.toml`。
- `playwright` 只在税屋 WAF 挑战那条路用，浏览器取系统已安装的
  Edge/Chrome/Firefox。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TAX_API_HOST` / `TAX_API_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `TAX_API_RATE_LIMIT` | `60` | 每客户端每窗口请求数；0 关闭 |
| `TAX_API_RATE_WINDOW_S` | `60` | 限流窗口秒数 |
| `TAX_API_LOG_LEVEL` | `info` | uvicorn 日志级别 |

## 能做什么

| 任务 | 接口 | 说明 |
|---|---|---|
| 健康与词表 | `GET /v1/health`、`GET /v1/tax-types` | 引擎状态；33 个税种/专题 |
| 模板 | `GET /v1/templates`、`GET /v1/templates/{key}` | 模板清单与原文 |
| 判型 | `POST /v1/classifications` | 题型、前提缺口、追问探针 |
| 依据覆盖 | `POST /v1/coverage-checks` | 必备依据的逐项状态与覆盖率 |
| 检索 | `POST /v1/searches`、`POST /v1/gov-searches` | 六源聚合；或税务总局／法规库单源检索 |
| 法规 | `GET /v1/laws/{law_id}`、`GET /v1/laws/{law_id}/text`、`GET /v1/laws/{law_id}/web-related`、`GET /v1/interpretations` | 元数据、全文、相关网页、官方解读（`interpretations` 的 `title` 必填） |
| 算式 | `GET /v1/calc-skeletons`、`POST /v1/calculations` | 骨架清单与参数化展开 |
| 类案 | `POST /v1/case-searches` | 查处通报与曝光典型台账 |
| 检查件 | `POST /v1/ledger-checks`、`POST /v1/inspections`、`GET /v1/intakes/blank`、`POST /v1/intakes`、`POST /v1/reconciliations`、`POST /v1/controls` | 账套巡检、稽查模拟、合同受理、条款对账、内控生成 |
| 分析 | `POST /v1/analysis-plans`、`POST /v1/analysis-gathers`、`POST /v1/analyses`、`GET /v1/analyses/{job_id}`、`GET /v1/analyses/{job_id}/result` | 检索计划、逐轮取数、异步整题分析 |

## 响应与错误

成功：

```json
{"data": "...", "meta": {"total": 12, "page": 1, "page_size": 10, "has_next": false}}
```

失败：

```json
{"error": {"code": "invalid_parameter", "message": "size 要在 1–50 之间",
           "details": {"field": "size"}}}
```

- 参数边界 422，业务校验 400，资源不存在 404，上游失败 502，限流 429
  （带 `Retry-After`）。
- 搜索的 `data` 是条目数组，分页与各源状况在 `meta`；多源聚合传 `page>1`
  返回 400 `page_not_supported`，不静默停在第一页。
- `gov-searches` 的 `source` 取 `chinatax` 或 `fgk`。`fgk` 不支持页码；
  `body` 只对 `fgk` 有效；`scope=fulltext` 改按正文匹配。成文日期、时效、
  税种、文种、年份、文号、效力等级等条件对两路都生效。
- `web-related` 的 `data.sources` 是合并去重后的条目，`meta.count` 是条数；
  税屋／公众号与 360 全网两路都失败时仍返回 200，原因写在
  `engine_error`，不能读成「没有相关内容」。
- `intakes/blank` 返回 `{"document": …}`；填好合同名称后原样 POST 到
  `/v1/intakes`，未填名返回 400。
- 分析任务在进程内异步执行：202 返回任务 ID 与 `Location`，`/result` 未
  完成时返回 409 `job_not_ready` + `Retry-After`。任务表上限 100 条，服务
  重启即丢。
- 限流按客户端地址的固定窗口计数，进程内有效；不信任 `X-Forwarded-For`。
- 域文档（覆盖率、对账、稽查等）保持中文业务字段名；信封、分页与错误码用
  英文字段。

## 限制

- 不提供认证与 TLS；要对外服务，需在反向代理层加认证、TLS 与更可靠的限流。
- 任务表与限流表都在进程内，多 worker 不共享；多实例部署需要外部存储。
- 不开放付费模型解读；服务本身不调用模型。
- 检索与分析会访问外部站点，失败按 502 或其他错误码返回，不会伪装成空结果。

## 独立运行与测试

包内 `src/tax_engine_api/runtime/` 是取数脚本、判据注册表与输出模板的只读
快照，`MANIFEST.json` 记录每个文件的 SHA-256；运行时不查环境变量，也不找
同级目录。

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

用例覆盖信封、参数校验、404/502/429、任务生命周期与错误报告；除被动触发的
校验路径外不联网。
