"""请求模型的边界校验。

这里只校验“请求长什么样”（类型、长度、取值域），业务判据仍由引擎
tax_service 负责。两层都做不是重复：这一层挡住 HTTP 上的坏请求，
tax_service 还要保护 CLI、MCP 与未来的进程内调用。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassifyRequest(StrictModel):
    question: str = Field(min_length=1)
    max_probes: int = Field(3, ge=1, le=10)


class CoverageRequest(StrictModel):
    qtype: str = Field(min_length=1)
    question: str = ""
    evidence: list[Any] | None = None
    skeleton: str = ""
    params: dict[str, Any] | None = None


class SearchRequest(StrictModel):
    query: str = Field(min_length=1)
    sources: list[str] | None = None
    size: int = Field(10, ge=1, le=50)
    page: int = Field(1, ge=1, le=1000)
    status: Literal[1, 2, 3, 4] = 3
    scope: Literal["title", "fulltext"] = "title"
    exact: bool = False
    sort: Literal["relevance", "date"] = "relevance"
    date_from: str = ""
    date_to: str = ""


class CalculationRequest(StrictModel):
    skeleton: str = Field(min_length=1)
    params: dict[str, Any]


class CaseSearchRequest(StrictModel):
    topic: str = Field(min_length=1)
    elements: list[str] | None = None
    mode: Literal["exhaustive", "recent"] = "exhaustive"
    pages: int = Field(1, ge=1, le=20)
    order: Literal["relevance", "date_desc", "category", "date_asc"] = "relevance"
    limit: int = Field(10, ge=1, le=100)
    as_of: str = ""
    shui5: bool = False


class LedgerCheckRequest(StrictModel):
    ledger: dict[str, Any]
    params: dict[str, Any] | None = None
    codes: list[str] | None = None
    tolerance: float = Field(0.0, ge=0.0)


class InspectionRequest(StrictModel):
    answers: dict[str, Any] | None = None
    domains: list[str] | None = None
    codes: list[str] | None = None
    scenario: str = ""
    signal: str = ""
    tolerance: float = Field(0.0, ge=0.0)


class IntakeRequest(StrictModel):
    document: dict[str, Any]


class ReconcileRequest(StrictModel):
    document: dict[str, Any]
    confirmed: list[str] | None = None
    dims: list[str] | None = None


class ControlRequest(StrictModel):
    diagnosis: dict[str, Any] | None = None
    assumptions: list[str] | None = None
    links: list[str] | None = None
    codes: list[str] | None = None


class AnalysisRequest(StrictModel):
    question: str = Field(min_length=1)
    at: str = ""
    read_body: bool = True
    check_citations: bool = True
