# tax-analysis-api

税务问题分析引擎的 HTTP 适配器。项目自带引擎的运行时快照（取数脚本、判据
注册表与输出模板），不读取外部引擎目录；只做边界校验、任务编排与响应成形。

## 运行

- Python ≥ 3.10；已验证 Python 3.13.14、FastAPI 0.143.1、Uvicorn 0.54.0、
  Pydantic 2.13.4。
- 运行时依赖 `requests`、`urllib3`、`playwright`（`playwright` 只在税屋
  WAF 挑战那条路用，浏览器本体取本机已装的 Edge/Chrome/Firefox）。

```bash
cd tax-analysis-api
PYTHONPATH=src python -m uvicorn tax_engine_api.app:app --host 127.0.0.1 --port 8000
```

或用控制台脚本：`PYTHONPATH=src python -m tax_engine_api.app`（安装后为
`tax-analysis-api`）。Swagger：<http://127.0.0.1:8000/docs>。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TAX_API_HOST` / `TAX_API_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `TAX_API_RATE_LIMIT` | `60` | 每客户端每窗口请求数；0 关闭 |
| `TAX_API_RATE_WINDOW_S` | `60` | 限流窗口秒数 |

## 运行时快照与独立性

- 本包 `src/tax_engine_api/runtime/` 是引擎的只读快照：24 个取数与判据脚本、
  7 个判据注册表、2 份输出模板与风险框架。引导层只把这一个目录放进
  `sys.path`，不 import 任何外部引擎包。
- `runtime/MANIFEST.json` 记录快照对应的引擎版本与每个文件的 SHA-256；
  `tests/test_api.py::test_runtime_snapshot_matches_manifest` 逐文件核对，
  缺文件或改一个字节都会报红。
- 本项目另带两份实现：`tax_service.py`（服务契约层：参数边界、错误码、
  结果形状）与 `tax_doc.py`（正文下载解析、官方解读检索）。CLI、API、MCP
  各带一份，三份要求逐字节相同，由
  `tests/test_api.py::test_vendored_copies_match_fingerprint` 用 SHA-256 钉住。
- 与引擎仓库没有运行时依赖：引擎升级后，本项目的行为不变，直到快照被重新
  同步并更新清单。`test_no_external_engine_lookup` 禁止代码回退到环境变量
  或同级目录查找。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 离线健康检查 |
| GET | `/v1/tax-types` | 33 个税种/专题 |
| GET | `/v1/templates` / `/v1/templates/{key}` | 输出模板 |
| POST | `/v1/classifications` | ① 判型 |
| POST | `/v1/coverage-checks` | ② 覆盖率 |
| POST | `/v1/searches` | ③④ 检索；多源无页码 |
| GET | `/v1/laws/{id}` / `/v1/laws/{id}/text` | 元数据 / 全文 |
| GET | `/v1/interpretations?title=…` | 官方解读 |
| POST | `/v1/calculations` | 算式展开 |
| POST | `/v1/case-searches` | 类案台账 |
| POST | `/v1/ledger-checks` | 账套巡检 |
| POST | `/v1/inspections` | 稽查模拟 |
| POST | `/v1/intakes` / `/v1/reconciliations` | 合同受理 / 对账 |
| POST | `/v1/controls` | 内控生成 |
| POST | `/v1/analyses` | ⑥ 分析长任务，202 + `Location` |
| GET | `/v1/analyses/{id}` / `/v1/analyses/{id}/result` | 状态 / 结果 |

## 约定

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
- 分析任务在进程内异步执行：202 返回任务 ID，`/result` 未完成时返回 409
  `job_not_ready` + `Retry-After`。任务表上限 100 条，服务重启即丢。
- 限流按客户端地址的固定窗口计数，进程内有效；不信任 `X-Forwarded-For`。
- 域文档（覆盖率、对账、稽查等）保持引擎的中文字段名，这是三个适配器共用的
  输出契约；信封、分页与错误码用英文字段。

## 测试

```bash
PYTHONPATH=src python -m pytest -q
```

用例覆盖信封、参数校验、404/502/429、任务生命周期与错误报告；除被动触发的
校验路径外不联网，不调用付费模型。

## 边界

- 不提供认证与 TLS；默认只监听回环地址。要对外提供服务，必须在反向代理层
  加认证、TLS 与更可靠的限流。
- 任务表与限流表都在进程内，多 worker 之间不共享；生产多实例需要外部存储。
- 不开放付费模型解读；引擎本身也不调用模型。
- 路由函数刻意用同步 `def`：引擎是阻塞 IO，FastAPI 会在线程池执行；写成
  `async def` 反而会阻塞事件循环。
