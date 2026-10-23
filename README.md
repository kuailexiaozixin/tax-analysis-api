# tax-analysis-api

把税务问题分析做成 HTTP 接口：客户端提交一句问题、一部法规的 ID、一组算式
参数、一份账套或一份合同文档，服务返回判型、依据覆盖、检索结果、法规正文、
算式结果、类案台账或检查报告。整题分析耗时较长，可以提交为后台任务，再轮询
状态和结果。

服务只返回结构化数据，不生成最终答复，也不调用任何模型。默认只监听
`127.0.0.1`，不带认证和 TLS，适合在本机或受控内网给前端、脚本和业务系统调用。

## 它适合谁

- 前端工作台：用户输入税务问题，页面调用判型、检索和法规正文接口，把结果
  展示成卡片或底稿。
- 案件或合同管理系统：把账套、合同 JSON 交给检查接口，拿回逐条风险与缺口。
- 其他语言的脚本：不用安装 Python 包，按 HTTP 约定发 JSON 即可。
- 需要留档的团队：每次请求和返回都是结构化 JSON，检索来源、失败原因和缺口
  都在响应里。

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

返回（节选）：

```json
{
  "data": {
    "question": "研发费用加计扣除比例是多少",
    "type": {"type": "lookup", "label": "政策查询", "confidence": 0.85},
    "context": {"missing": ["time", "entity", "place", "scale"]},
    "must_answer": "结论 + 效力位阶 + 文号 + 生效时点"
  }
}
```

`body.json` 必须是 UTF-8。Windows 终端把中文直接放进 `-d` 时可能按本地
编码发送，服务端报 `There was an error parsing the body`；用文件传请求体
可以避开这个问题。Windows PowerShell 5.1 的 `curl` 是
`Invoke-WebRequest` 的别名，不认 `-s`、`-X`，请改用 `curl.exe`；
`body.json` 用编辑器保存为 UTF-8（无 BOM）。

Swagger 在 `http://127.0.0.1:8000/docs`。`TAX_API_HOST`、`TAX_API_PORT`
改监听地址；PowerShell 用 `$env:TAX_API_PORT='9000'; tax-analysis-api`，
Git Bash 用 `TAX_API_PORT=9000 tax-analysis-api`。脚本不在 `PATH` 时，
用 `python -m tax_engine_api.app` 启动。

从源码开发时不必安装，在仓库根目录运行：

```bash
PYTHONPATH=src python -m uvicorn tax_engine_api.app:app --host 127.0.0.1 --port 8000
```

## 接口清单

所有接口都以 `/v1` 开头，共 26 条。成功响应统一是 `{"data": ...}`，部分接口带
`{"meta": ...}`；失败响应统一是 `{"error": {"code", "message", "details"?}}`。

### 健康、词表与模板

| 方法与路径 | 请求 | 返回什么 |
|---|---|---|
| `GET /v1/health` | 无 | `data.status`、`engine_root`、税种数、骨架数、模板数 |
| `GET /v1/tax-types` | 无 | 33 个税种或专题的名称、别名、上位法、路由归属与备注；`meta.count` 为条数 |
| `GET /v1/templates` | 无 | 11 个输出模板的键与标题；`meta.count` 为条数 |
| `GET /v1/templates/{key}` | 模板键 | `data.key` 与 `data.template`（模板原文） |

### 判型与依据覆盖

| 方法与路径 | 请求体 | 返回什么 |
|---|---|---|
| `POST /v1/classifications` | `question` 必填；`max_probes` 取 1–10，默认 3 | 题型与把握度、判型依据、未交代的前提、追问探针、应覆盖的依据类型、结论要件 |
| `POST /v1/coverage-checks` | `qtype` 必填；`question`、`evidence`、`skeleton`、`params` 可选 | 必备依据逐项状态、覆盖率、补问与下一步；`evidence=null` 表示没检，`[]` 表示检了没有 |

### 检索与法规

| 方法与路径 | 请求 | 返回什么 |
|---|---|---|
| `POST /v1/searches` | `query` 必填；`sources`、`size`、`page`、`status`、`scope`、`exact`、`sort`、`date_from`、`date_to` | `data` 是条目数组；`meta` 含各源条数、失败原因、缺口和分页字段 |
| `POST /v1/gov-searches` | 同上，另有 `source`（`chinatax` 或 `fgk`）、`aging`、`body`、`file_only`、`tax_type`、`doc_type`、`doc_year`、`doc_no`、`effect_level` | 税务总局站内或法规库的条目数组与筛选说明 |
| `GET /v1/laws/{law_id}` | NPC bbbs 编号 | 法规元数据（标题、文号、时效等） |
| `GET /v1/laws/{law_id}/text` | NPC bbbs 编号 | `available`、`count` 与正文段落数组 |
| `GET /v1/laws/{law_id}/web-related` | `keyword` 可选 | 税屋、微信公众号与搜狗全网的合并去重结果；搜狗那一路失败时仍返回 200，原因在 `data.engine_error` |
| `GET /v1/interpretations` | `title` 必填；`keyword`、`province`、`source` 可选 | 只来自 `.gov.cn` 的官方解读条目 |

`/v1/searches` 的六个来源是 `npc`、`chinatax`、`sogou`、`shui5`、`wechat`、
`ctax`，分别覆盖全国人大法规库、税务总局、地方口径、税屋、微信公众号和中国
税网。默认 `status=3`（现行有效）；`1` 已废止、`2` 已修改、`4` 尚未生效。

搜狗网页检索以 `m.sogou.com` 为正常入口，每次只取一页并缓存整页元数据 30 分钟；
`size` 只限制输出，同词同站换条数复用缓存，不增加请求或保证取满。摘要完整保留，
日期缺失时留空。官方解读默认向总局、财政部、中国政府网各问一次，不再单独问法规库；
显式指定法规库或省局仍可使用，父域检索不保证法规库召回。部分失败保留已取回结果，
另报 `engine_error`、`queries_blocked`。各站有序查询，仅复用取数层的 30 分钟缓存，到期后重查，
不另存无期限组合结果、不设 15 秒线程等待上限。网络异常与 5xx 有限重试，其他非 200 状态直接报错。
保留限频与冷却，遇拦截立即返回，
不等待冷却到期，也不通过代理池或端点轮换绕过限制。

### 算式与类案

| 方法与路径 | 请求 | 返回什么 |
|---|---|---|
| `GET /v1/calc-skeletons` | 无 | 六条算式骨架的名称、公式、输入与结果字段 |
| `POST /v1/calculations` | `skeleton` 与 `params` | 代入步骤、分档与最终结果；缺参数报 `missing_params`，多余参数报 `invalid_parameter` |
| `POST /v1/case-searches` | `topic` 必填；`elements`、`mode`、`pages`、`order`、`limit`、`as_of`、`shui5` 可选 | 税务总局查处通报与曝光典型的候选、过滤、去重台账；`meta.candidate_count` 为候选数 |

### 检查件

| 方法与路径 | 请求体 | 返回什么 |
|---|---|---|
| `POST /v1/ledger-checks` | `ledger` 必填；`params`、`codes`、`tolerance` 可选 | 八要素风险条目、判不动清单、参数缺口与数据缺口 |
| `POST /v1/inspections` | `answers`、`domains`、`codes`、`scenario`、`signal`、`tolerance` 均可选 | 逐问的六字段问询单元与三类缺口；没有作答也能生成 |
| `GET /v1/intakes/blank` | 无 | `data.document` 空白受理表；`meta.contracts` 与 `meta.units` 为占位记录数 |
| `POST /v1/intakes` | `document` 必填 | 合同清单、九维度覆盖、待核抽取与没交的要素 |
| `POST /v1/reconciliations` | `document` 必填；`confirmed`、`dims` 可选 | 差异条目、无法确认条目与证据缺口 |
| `POST /v1/controls` | `diagnosis`、`assumptions`、`links`、`codes` 均可选 | 九要素内控制度文本；没有诊断时按通用模板生成并列出假设 |

`ledger` 的顶层形态是 `{"行": [...]}`，每行至少包含行号、科目和金额；具体
规则需要哪些字段，见包内 `runtime/data/ledger_rules.json` 的「账套字段」。
合同文档可以先从 `GET /v1/intakes/blank` 取空白表，填写合同名称等字段后
原样 POST 到 `/v1/intakes` 或 `/v1/reconciliations`。

### 整题分析

| 方法与路径 | 请求体 | 返回什么 |
|---|---|---|
| `POST /v1/analysis-plans` | `question` 必填 | 判型、缺口、检索轮次；每轮的目标、数据源与调用方式 |
| `POST /v1/analysis-gathers` | `question` 必填；`at`、`read_body`、`check_citations`、`max_per_round` 可选 | 逐轮证据、定级结果、缺口与取数失败；`meta.evidence_count` 与 `meta.errors` |
| `POST /v1/analyses` | 同上 | 202，`data.id` 与 `data.status`；`Location` 指向任务状态 |
| `GET /v1/analyses/{job_id}` | 无 | 任务状态、问题、创建时间、结束时间与错误 |
| `GET /v1/analyses/{job_id}/result` | 无 | 完成后的分层底稿；未完成返回 409，失败返回任务错误 |

`analysis-gathers` 只取证据和缺口，`analyses` 取回的是分层底稿（主依据、
其他法定依据、执行口径与实务认定、待核对线索），两者都不写最终答复。

## 异步分析怎么用

提交任务：

```bash
printf '%s' '{"question":"研发费用加计扣除比例是多少","max_per_round":3}' \
  > analysis.json
curl -s -D - -X POST http://127.0.0.1:8000/v1/analyses \
  -H 'Content-Type: application/json' --data-binary @analysis.json
```

响应是 202，`Location` 头给出任务地址，正文形如：

```json
{
  "data": {"id": "<job_id>", "status": "queued"},
  "meta": {"poll": "/v1/analyses/<job_id>"}
}
```

轮询状态：

```bash
curl -s http://127.0.0.1:8000/v1/analyses/<job_id>
```

任务完成后再取结果：

```bash
curl -s http://127.0.0.1:8000/v1/analyses/<job_id>/result
```

任务状态依次经过 `queued`、`running`、`succeeded` 或 `failed`。结果未完成时
返回 409 `job_not_ready`，并带 `Retry-After: 5`；任务失败时 `/result` 返回
错误码和原因，不会返回一个空底稿。

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

| HTTP | 典型错误码 | 含义 |
|---|---|---|
| 400 | `invalid_parameter`、`invalid_request`、`page_not_supported` | 业务参数或请求形态不对 |
| 404 | `not_found`、`unknown_template` | 法规、任务或模板不存在 |
| 409 | `job_not_ready` | 分析任务还没完成 |
| 422 | `validation_error` | 请求体不符合字段类型、长度或取值域 |
| 429 | `rate_limited` | 超过限流，带 `Retry-After` |
| 502 | `upstream_error` | 外部站点取数失败 |
| 500 | `internal_error` | 服务内部错误 |

几个需要按实际结构读取的字段：

- 搜索的 `data` 是条目数组，分页与各源状况在 `meta`；多源聚合传 `page>1`
  返回 400 `page_not_supported`，不会静默停在第一页。
- `gov-searches` 的 `source` 取 `chinatax` 或 `fgk`。`fgk` 不支持页码；
  `body` 只对 `fgk` 有效；`scope=fulltext` 改按正文匹配。成文日期、时效、
  税种、文种、年份、文号、效力等级等条件对两路都生效。
- `web-related` 的 `data.sources` 是合并去重后的条目，`meta.count` 是条数；
  搜狗全网那一路失败时仍返回 200，原因写在 `data.engine_error`，不能读成
  「没有相关内容」。税屋与公众号那一路失败时只体现为条目减少，没有单独的错误字段。
- `intakes/blank` 返回 `{"document": …}`；填好合同名称后原样 POST 到
  `/v1/intakes`，未填名返回 400。
- 限流按客户端地址的固定窗口计数，进程内有效；不信任 `X-Forwarded-For`。
- 域文档（覆盖率、对账、稽查等）保持中文业务字段名；信封、分页与错误码用
  英文字段。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `TAX_API_HOST` / `TAX_API_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `TAX_API_RATE_LIMIT` | `60` | 每客户端每窗口请求数；0 关闭 |
| `TAX_API_RATE_WINDOW_S` | `60` | 限流窗口秒数 |
| `TAX_API_LOG_LEVEL` | `info` | uvicorn 日志级别 |

## 环境

- Python ≥ 3.10；依赖见 `pyproject.toml`。
- `playwright` 只在税屋 WAF 挑战那条路用，优先调用系统已安装的
  Edge、Chrome、Brave、360 或 Firefox，不额外下载内核。

## 限制

- 不提供认证与 TLS；要对外服务，需在反向代理层加认证、TLS 与更可靠的限流。
- 任务表与限流表都在进程内，多 worker 不共享；任务表上限 100 条，服务重启
  即丢，多实例部署需要外部存储。
- 不开放付费模型解读；服务本身不调用模型。
- 检索与分析会访问外部站点：整次取数失败返回 502；多源里的单源失败写在
  响应的 `meta.errors`，不会伪装成空结果。
- 多源聚合没有页码维度；`search-gov` 的 `fgk` 一路没有页码。
- 账套和合同检查按 JSON 字段判定；字段缺失时返回 400，不猜测对应关系。

## 独立运行与测试

包内 `src/tax_engine_api/runtime/` 是取数脚本、判据注册表与输出模板的只读
快照，`MANIFEST.json` 记录每个文件的 SHA-256；运行时不查环境变量，也不找
同级目录。

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

用例覆盖信封、参数校验、404、400、429、502、任务生命周期与错误报告；除被动
触发的校验路径外不联网。
