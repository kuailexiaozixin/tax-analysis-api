"""tax-analysis-skill 的 HTTP 接口。

设计边界：
- 路由函数是同步的：引擎取数是阻塞 IO，FastAPI 会把 sync 路由放进线程池，
  不能写成 async 后在事件循环里直接发 requests。
- 统一信封：成功 {"data", "meta"?}，失败 {"error": {"code","message","details"?}}。
- 分析是长任务：POST /v1/analyses 返回 202 + Location，轮询状态与结果分开取。
- 付费模型解读不开放；本服务不会调用任何外部模型。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import engine
from .schemas import (
    AnalysisGatherRequest,
    AnalysisPlanRequest,
    AnalysisRequest,
    CalculationRequest,
    CaseSearchRequest,
    ClassifyRequest,
    ControlRequest,
    CoverageRequest,
    GovSearchRequest,
    InspectionRequest,
    IntakeRequest,
    LedgerCheckRequest,
    ReconcileRequest,
    SearchRequest,
)

svc = engine.tax_service
ServiceError = svc.ServiceError
logger = logging.getLogger("tax_engine_api")


# ── 任务与限流 ─────────────────────────────────────────────────────────────

class JobStore:
    """进程内任务表。服务重启即丢，这是本地单进程适配器的明确边界。"""

    def __init__(self, max_jobs: int = 100):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max = max_jobs

    def create(self, question: str) -> dict:
        with self._lock:
            if len(self._jobs) >= self._max:
                oldest = min(self._jobs.values(), key=lambda j: j["created_ts"])
                self._jobs.pop(oldest["id"], None)
            job = {
                "id": uuid.uuid4().hex,
                "status": "queued",
                "question": question,
                "created_ts": time.time(),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "result": None,
                "error": None,
                "status_code": None,
            }
            self._jobs[job["id"]] = job
            return dict(job)

    def start(self, job_id: str):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "running"

    def finish(self, job_id: str, result: dict):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "succeeded"
                job["result"] = result
                job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def fail(self, job_id: str, error: dict, status_code: int):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = error
                job["status_code"] = status_code
                job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def public(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return {k: job[k] for k in
                    ("id", "status", "question", "created_at",
                     "finished_at", "error")}

    def result_of(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if not job else {
                "status": job["status"],
                "result": job["result"],
                "error": job["error"],
                "status_code": job["status_code"],
            }


class RateLimiter:
    """按客户端地址的固定窗口限流；只在本进程内有效。"""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, int(self.window - (now - hits[0]) + 0.999))
                return retry
            hits.append(now)
            if len(self._hits) > 10000:
                for k in [k for k, q in self._hits.items() if not q]:
                    self._hits.pop(k, None)
            return None


def get_jobs(request: Request) -> JobStore:
    return request.app.state.jobs


Jobs = Annotated[JobStore, Depends(get_jobs)]


# ── 响应与异常 ─────────────────────────────────────────────────────────────

def ok(data: Any, meta: dict | None = None) -> dict:
    body: dict[str, Any] = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return body


def _run_analysis(jobs: JobStore, job_id: str, req: AnalysisRequest):
    jobs.start(job_id)
    try:
        result = svc.analyze_answer(
            req.question, at=req.at, read_body=req.read_body,
            check_citations=req.check_citations,
            max_per_round=req.max_per_round)
    except ServiceError as exc:
        jobs.fail(job_id, exc.as_dict()["error"], exc.status)
    except Exception as exc:  # 任务失败必须留下状态与原因，不能静默消失
        logger.exception("analysis job %s failed", job_id)
        jobs.fail(job_id, {
            "code": "internal_error",
            "message": f"{type(exc).__name__}: {exc}",
        }, 500)
    else:
        jobs.finish(job_id, result)


def _install_error_handlers(application: FastAPI):
    @application.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError):
        return JSONResponse(exc.as_dict(), status_code=exc.status)

    @application.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        details = [{"loc": list(err.get("loc", [])),
                    "msg": err.get("msg", ""),
                    "type": err.get("type", "")}
                   for err in exc.errors()]
        return JSONResponse({
            "error": {
                "code": "validation_error",
                "message": "请求参数不合法",
                "details": details,
            }
        }, status_code=422)

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=exc.status_code, headers=exc.headers)

    @application.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "服务内部错误"}},
            status_code=500)


# ── 路由 ───────────────────────────────────────────────────────────────────

router = APIRouter()


@router.get("/health")
def health():
    return ok(svc.health())


@router.get("/tax-types")
def tax_types():
    rows = svc.tax_types()
    return ok(rows, {"count": len(rows)})


@router.get("/templates")
def templates():
    rows = [{"key": key, "title": heading}
            for key, heading in svc.TEMPLATE_KEYS.items()
            if heading in svc.template_index()]
    return ok(rows, {"count": len(rows)})


@router.get("/templates/{key}")
def template(key: str):
    return ok({"key": key, "template": svc.template_text(key)})


@router.post("/classifications")
def classify(req: ClassifyRequest):
    return ok(svc.classify(req.question, max_probes=req.max_probes))


@router.post("/coverage-checks")
def coverage(req: CoverageRequest):
    return ok(svc.coverage(
        req.qtype, question=req.question, evidence=req.evidence,
        skeleton=req.skeleton, params=req.params))


@router.post("/searches")
def search(req: SearchRequest):
    result = svc.search(
        req.query, sources=req.sources, size=req.size, page=req.page,
        status=req.status, scope=req.scope, exact=req.exact,
        sort=req.sort, date_from=req.date_from, date_to=req.date_to)
    meta = {
        "query": result["query"],
        "page": result["page"],
        "page_size": result["page_size"],
        "total": result["total"],
        "has_next": result["has_next"],
        # 引擎两条分支的 meta 字段不同（NPC 带缓存与可靠性，聚合带 date_filter），
        # 手挑字段会把没列到的丢掉，调用方读不到这一轮是否命中缓存、日期是否生效。
        **(result.get("meta") or {}),
    }
    return ok(result["items"], meta)


@router.post("/gov-searches")
def gov_search(req: GovSearchRequest):
    result = svc.search_gov(
        req.query, source=req.source, size=req.size, page=req.page,
        scope=req.scope, exact=req.exact, sort=req.sort,
        date_from=req.date_from, date_to=req.date_to, aging=req.aging,
        body=req.body, file_only=req.file_only, tax_type=req.tax_type,
        doc_type=req.doc_type, doc_year=req.doc_year, doc_no=req.doc_no,
        effect_level=req.effect_level)
    meta = {
        "query": result["query"],
        "source": result["source"],
        "page": result["page"],
        "page_size": result["page_size"],
        "total": result["total"],
        "has_next": result["has_next"],
        **(result.get("meta") or {}),
    }
    return ok(result["items"], meta)


@router.get("/laws/{law_id}")
def law_info(law_id: str):
    return ok(svc.law_info(law_id))


@router.get("/laws/{law_id}/text")
def law_text(law_id: str):
    return ok(svc.law_text(law_id))


@router.get("/laws/{law_id}/web-related")
def law_web_related(
    law_id: str,
    keyword: Annotated[str, Query()] = "",
):
    result = svc.web_related(law_id, keyword=keyword)
    return ok(result, {"count": result.get("total", 0)})


@router.get("/interpretations")
def interpretations(
    title: Annotated[str, Query(min_length=1)],
    keyword: Annotated[str, Query()] = "",
    province: Annotated[str, Query()] = "",
    source: Annotated[list[str] | None, Query()] = None,
):
    result = svc.law_interpretations(
        title, keyword=keyword, province=province, sources=source)
    return ok(result, {"count": result.get("total", 0)})


@router.get("/calc-skeletons")
def calc_skeletons():
    rows = svc.calc_list()
    return ok(rows, {"count": len(rows)})


@router.post("/calculations")
def calculation(req: CalculationRequest):
    return ok(svc.calc_run(req.skeleton, req.params))


@router.post("/case-searches")
def case_searches(req: CaseSearchRequest):
    result = svc.cases(
        req.topic, elements=req.elements, mode=req.mode, pages=req.pages,
        order=req.order, limit=req.limit, as_of=req.as_of, shui5=req.shui5)
    return ok(result, {"candidate_count": result.get("candidate_count", 0)})


@router.post("/ledger-checks")
def ledger_check(req: LedgerCheckRequest):
    return ok(svc.ledger_check(
        req.ledger, params=req.params, codes=req.codes,
        tolerance=req.tolerance))


@router.post("/inspections")
def inspection(req: InspectionRequest):
    return ok(svc.inspection(
        answers=req.answers, domains=req.domains, codes=req.codes,
        scenario=req.scenario, signal=req.signal, tolerance=req.tolerance))


@router.get("/intakes/blank")
def intake_blank():
    doc = svc.intake_blank()
    return ok({"document": doc},
              {"contracts": len(doc.get("合同") or []),
               "units": len(doc.get("判断单元") or [])})


@router.post("/intakes")
def intake(req: IntakeRequest):
    return ok(svc.intake(req.document))


@router.post("/reconciliations")
def reconcile(req: ReconcileRequest):
    return ok(svc.reconcile(
        req.document, confirmed=req.confirmed, dims=req.dims))


@router.post("/controls")
def controls(req: ControlRequest):
    return ok(svc.controls(
        diagnosis=req.diagnosis, assumptions=req.assumptions,
        links=req.links, codes=req.codes))


@router.post("/analysis-plans")
def analysis_plan(req: AnalysisPlanRequest):
    return ok(svc.analyze_plan(req.question))


@router.post("/analysis-gathers")
def analysis_gather(req: AnalysisGatherRequest):
    result = svc.analyze_gather(
        req.question, at=req.at, read_body=req.read_body,
        check_citations=req.check_citations,
        max_per_round=req.max_per_round)
    meta = {"evidence_count": len(result.get("evidence") or []),
            "errors": result.get("errors") or []}
    return ok(result, meta)


@router.post("/analyses", status_code=202)
def create_analysis(
    req: AnalysisRequest,
    response: Response,
    background: BackgroundTasks,
    jobs: Jobs,
):
    job = jobs.create(req.question)
    response.headers["Location"] = f"/v1/analyses/{job['id']}"
    background.add_task(_run_analysis, jobs, job["id"], req)
    return ok({"id": job["id"], "status": "queued"},
              {"poll": f"/v1/analyses/{job['id']}"})


@router.get("/analyses/{job_id}")
def analysis_status(job_id: str, jobs: Jobs):
    job = jobs.public(job_id)
    if job is None:
        raise ServiceError("not_found", f"没有这个任务：{job_id}", 404)
    return ok(job)


@router.get("/analyses/{job_id}/result")
def analysis_result(job_id: str, jobs: Jobs):
    record = jobs.result_of(job_id)
    if record is None:
        raise ServiceError("not_found", f"没有这个任务：{job_id}", 404)
    status = record["status"]
    if status == "failed":
        return JSONResponse(
            {"error": record["error"]},
            status_code=record.get("status_code") or 500)
    if status != "succeeded":
        return JSONResponse(
            {"error": {
                "code": "job_not_ready",
                "message": "分析还没有完成，稍后再取结果",
                "details": {"status": status},
            }},
            status_code=409, headers={"Retry-After": "5"})
    return ok(record["result"])


# ── 应用工厂 ───────────────────────────────────────────────────────────────

def create_app(*, rate_limit: int | None = None,
               rate_window_s: float | None = None,
               max_jobs: int = 100) -> FastAPI:
    limit = int(os.getenv("TAX_API_RATE_LIMIT", "60")) if rate_limit is None else rate_limit
    window = float(os.getenv("TAX_API_RATE_WINDOW_S", "60")) if rate_window_s is None else rate_window_s

    application = FastAPI(
        title="tax-analysis-skill API",
        version="1.0.0",
        description=(
            "税收政策分析引擎的 HTTP 适配器。返回的是结构化底稿，"
            "不调用外部模型、不生成最终成稿。"
        ),
    )
    application.state.jobs = JobStore(max_jobs=max_jobs)
    application.state.limiter = RateLimiter(limit, window) if limit > 0 else None

    @application.middleware("http")
    async def _rate_limit(request: Request, call_next):
        limiter: RateLimiter | None = application.state.limiter
        if limiter is not None and request.url.path.startswith("/v1"):
            key = request.client.host if request.client else "unknown"
            retry = limiter.hit(key)
            if retry is not None:
                return JSONResponse(
                    {"error": {
                        "code": "rate_limited",
                        "message": f"请求过于频繁，{retry} 秒后重试",
                        "details": {"retry_after_s": retry},
                    }},
                    status_code=429,
                    headers={"Retry-After": str(retry)})
        return await call_next(request)

    _install_error_handlers(application)
    application.include_router(router, prefix="/v1")
    return application


app = create_app()


def main():
    uvicorn.run(
        "tax_engine_api.app:app",
        host=os.getenv("TAX_API_HOST", "127.0.0.1"),
        port=int(os.getenv("TAX_API_PORT", "8000")),
        log_level=os.getenv("TAX_API_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
