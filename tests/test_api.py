import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tax_engine_api import engine  # noqa: E402
from tax_engine_api.app import create_app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_health_envelope(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["tax_types"] == 33
    assert body["data"]["calc_skeletons"] == 6


def test_tax_types_are_a_list_with_leaf_tobacco(client):
    resp = client.get("/v1/tax-types")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["count"] == 33
    assert any(row["name"] == "烟叶税" for row in body["data"])


def test_template_show_and_missing(client):
    ok = client.get("/v1/templates/analysis")
    assert ok.status_code == 200
    assert "分析六段式" in ok.json()["data"]["template"]

    missing = client.get("/v1/templates/no-such-template")
    assert missing.status_code == 404
    body = missing.json()
    assert body["error"]["code"] == "unknown_template"
    assert "allowed" in body["error"]["details"]


def test_template_list_matches_health_and_includes_nested_blocks(client):
    listing = client.get("/v1/templates")
    assert listing.status_code == 200
    body = listing.json()
    keys = {row["key"] for row in body["data"]}
    assert {"ledger", "intake"} <= keys
    assert body["meta"]["count"] == len(keys) == len(engine.tax_service.TEMPLATE_KEYS)
    health = client.get("/v1/health").json()["data"]
    assert health["template_count"] == len(keys)


def test_classify_offline(client):
    resp = client.post("/v1/classifications",
                       json={"question": "研发费用加计扣除比例是多少"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["type"]["type"] == "lookup"
    assert data["probes"]


def test_coverage_unknown_type_is_400(client):
    resp = client.post("/v1/coverage-checks", json={"qtype": "no-such-type"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "unknown_question_type"
    assert "lookup" in body["error"]["details"]["allowed"]


def test_case_search_accepts_engine_order_values(client, monkeypatch):
    captured = {}

    def fake_cases(topic, **kwargs):
        captured.update(kwargs)
        return {"topic": topic, "items": [], "candidate_count": 0}

    monkeypatch.setattr(engine.tax_service, "cases", fake_cases)
    ok = client.post("/v1/case-searches",
                     json={"topic": "虚开发票", "order": "date_desc"})
    assert ok.status_code == 200, ok.text
    assert captured["order"] == "date_desc"

    bad = client.post("/v1/case-searches",
                      json={"topic": "虚开发票", "order": "date"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"


def test_search_envelope_and_pagination_meta(client, monkeypatch):
    def fake_search(query, **kwargs):
        assert query == "增值税"
        assert kwargs["sources"] == ["npc"]
        return {
            "query": query, "page": 2, "page_size": 5, "total": 12,
            "has_next": True,
            "items": [{"id": "1", "title": "中华人民共和国增值税法"}],
            "meta": {"source_summary": {"npc": 5}, "errors": {},
                     "gaps": [], "searched_at": "2026-10-20 12:00:00",
                     "from_cache": False, "reliability": "A1",
                     "reliability_note": "官方发布"},
        }

    monkeypatch.setattr(engine.tax_service, "search", fake_search)
    resp = client.post("/v1/searches", json={
        "query": "增值税", "sources": ["npc"], "page": 2, "size": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"][0]["title"] == "中华人民共和国增值税法"
    assert body["meta"] == {
        "query": "增值税", "page": 2, "page_size": 5, "total": 12,
        "has_next": True, "source_summary": {"npc": 5}, "errors": {},
        "gaps": [], "searched_at": "2026-10-20 12:00:00",
        "from_cache": False, "reliability": "A1",
        "reliability_note": "官方发布",
    }


def test_multi_source_page_two_is_400_without_network(client):
    resp = client.post("/v1/searches", json={
        "query": "增值税", "sources": ["npc", "chinatax"], "page": 2})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "page_not_supported"


def test_gov_search_forwards_filters_and_returns_meta(client, monkeypatch):
    captured = {}

    def fake_gov(query, **kwargs):
        captured.update(kwargs)
        return {
            "query": query, "source": kwargs["source"], "page": 2,
            "page_size": 10, "total": 25, "has_next": True,
            "items": [{"title": "测试文件", "document_number": "公告2026年第1号"}],
            "meta": {"source": kwargs["source"],
                     "filters": {"xxgkAging": "全文有效"},
                     "filter_note": "维度拼窄的说明", "errors": {},
                     "total_hits": 99},
        }

    monkeypatch.setattr(engine.tax_service, "search_gov", fake_gov)
    resp = client.post("/v1/gov-searches", json={
        "query": "增值税", "source": "chinatax", "page": 2,
        "aging": "全文有效", "date_from": "2026-01-01", "sort": "date",
        "tax_type": "增值税",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert captured["aging"] == "全文有效"
    assert captured["date_from"] == "2026-01-01"
    assert captured["sort"] == "date"
    assert captured["tax_type"] == "增值税"
    assert body["meta"]["total"] == 25
    assert body["meta"]["filter_note"] == "维度拼窄的说明"
    assert body["data"][0]["title"] == "测试文件"


def test_gov_search_rejects_fgk_page_before_network(client):
    resp = client.post("/v1/gov-searches", json={
        "query": "增值税", "source": "fgk", "page": 2})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "page_not_supported"


def test_calc_skeleton_list(client):
    resp = client.get("/v1/calc-skeletons")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["count"] == 6
    row = next(item for item in body["data"] if item["name"] == "从价计征")
    assert row["formula"]
    assert "计税依据" in row["inputs"]


def test_analysis_plan_offline(client):
    resp = client.post("/v1/analysis-plans",
                       json={"question": "研发费用加计扣除比例是多少"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["question"] == "研发费用加计扣除比例是多少"
    assert data["rounds"]


def test_analysis_gather_forwards_max_per_round(client, monkeypatch):
    captured = {}

    def fake_gather(question, **kwargs):
        captured.update(kwargs)
        return {"question": question, "evidence": [], "errors": []}

    monkeypatch.setattr(engine.tax_service, "analyze_gather", fake_gather)
    resp = client.post("/v1/analysis-gathers", json={
        "question": "研发费用加计扣除比例是多少",
        "at": "2026-10-20", "read_body": False, "max_per_round": 2,
    })
    assert resp.status_code == 200, resp.text
    assert captured["at"] == "2026-10-20"
    assert captured["read_body"] is False
    assert captured["max_per_round"] == 2
    assert resp.json()["meta"]["evidence_count"] == 0


def test_request_validation_uses_error_envelope(client):
    resp = client.post("/v1/searches", json={"query": "增值税", "size": 0})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]


def test_calculation_run_and_missing_params(client):
    ok = client.post("/v1/calculations", json={
        "skeleton": "从价计征",
        "params": {"计税依据": 100, "税率": 0.05},
    })
    assert ok.status_code == 200
    assert ok.json()["data"]["结果"]["应纳税额"] is not None

    missing = client.post("/v1/calculations", json={
        "skeleton": "从价计征", "params": {"计税依据": 100}})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "missing_params"

    extra = client.post("/v1/calculations", json={
        "skeleton": "从价计征",
        "params": {"计税依据": 100, "税率": 0.05, "多余": 1},
    })
    assert extra.status_code == 400
    assert extra.json()["error"]["code"] == "invalid_parameter"


def test_law_text_patched(client, monkeypatch):
    monkeypatch.setattr(
        engine.tax_service, "law_text",
        lambda law_id: {"law_id": law_id, "available": True,
                        "count": 1, "paragraphs": ["第一条"]},
    )
    resp = client.get("/v1/laws/abc/text")
    assert resp.status_code == 200
    assert resp.json()["data"]["paragraphs"] == ["第一条"]


def test_law_web_related_forwards_keyword(client, monkeypatch):
    captured = {}

    def fake_related(law_id, keyword=""):
        captured.update(law_id=law_id, keyword=keyword)
        return {"law_id": law_id, "law_title": "增值税法", "keyword": keyword,
                "total": 1, "sources": [{"title": "解读A"}]}

    monkeypatch.setattr(engine.tax_service, "web_related", fake_related)
    resp = client.get("/v1/laws/abc/web-related", params={"keyword": "税率"})
    assert resp.status_code == 200, resp.text
    assert captured == {"law_id": "abc", "keyword": "税率"}
    assert resp.json()["data"]["sources"][0]["title"] == "解读A"
    assert resp.json()["meta"]["count"] == 1


def test_law_web_related_upstream_error_is_502(client, monkeypatch):
    from tax_engine_api.engine import tax_service

    def broken(*_a, **_k):
        raise tax_service.ServiceError("upstream_error", "找相关网页失败", 502)

    monkeypatch.setattr(engine.tax_service, "web_related", broken)
    resp = client.get("/v1/laws/abc/web-related")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"


def test_blank_intake_round_trips_through_intake(client):
    blank = client.get("/v1/intakes/blank")
    assert blank.status_code == 200
    body = blank.json()
    doc = body["data"]["document"]
    assert [c["代号"] for c in doc["合同"]] == ["CT-1"]
    assert [u["代号"] for u in doc["判断单元"]] == ["U1"]
    assert body["meta"] == {"contracts": 1, "units": 1}

    doc["合同"][0]["名称"] = "采购合同"
    filled = client.post("/v1/intakes", json={"document": doc})
    assert filled.status_code == 200, filled.text
    assert filled.json()["data"]["缺维度数"] == 9


def test_law_not_found_is_404(client, monkeypatch):
    from tax_engine_api.engine import tax_service

    def missing(_law_id):
        raise tax_service.ServiceError("not_found", "没有这份法规", 404)

    monkeypatch.setattr(engine.tax_service, "law_info", missing)
    resp = client.get("/v1/laws/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_upstream_error_is_502(client, monkeypatch):
    from tax_engine_api.engine import tax_service

    def broken(*_a, **_k):
        raise tax_service.ServiceError("upstream_error", "上游不可用", 502)

    monkeypatch.setattr(engine.tax_service, "search", broken)
    resp = client.post("/v1/searches", json={"query": "增值税"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"


def test_analysis_job_lifecycle(client, monkeypatch):
    monkeypatch.setattr(
        engine.tax_service, "analyze_answer",
        lambda *a, **k: {"question": "研发费用加计扣除比例是多少",
                         "primary": {"title": "公告2023年第7号"},
                         "counts": {"total": 1}},
    )
    created = client.post("/v1/analyses",
                          json={"question": "研发费用加计扣除比例是多少"})
    assert created.status_code == 202
    job_id = created.json()["data"]["id"]
    assert created.headers["Location"].endswith(f"/v1/analyses/{job_id}")

    status = client.get(f"/v1/analyses/{job_id}")
    assert status.status_code == 200
    assert status.json()["data"]["status"] == "succeeded"

    result = client.get(f"/v1/analyses/{job_id}/result")
    assert result.status_code == 200
    assert result.json()["data"]["primary"]["title"] == "公告2023年第7号"


def test_analysis_job_failure_is_reported(client, monkeypatch):
    from tax_engine_api.engine import tax_service

    def broken(*_a, **_k):
        raise tax_service.ServiceError("upstream_error", "取数失败", 502)

    monkeypatch.setattr(engine.tax_service, "analyze_answer", broken)
    created = client.post("/v1/analyses", json={"question": "测试题"})
    job_id = created.json()["data"]["id"]
    result = client.get(f"/v1/analyses/{job_id}/result")
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "upstream_error"


def test_job_not_found(client):
    resp = client.get("/v1/analyses/nope/result")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_rate_limit_returns_429_and_retry_after():
    with TestClient(create_app(rate_limit=2, rate_window_s=60)) as c:
        assert c.get("/v1/health").status_code == 200
        assert c.get("/v1/health").status_code == 200
        blocked = c.get("/v1/health")
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert int(blocked.headers["Retry-After"]) >= 1


def test_unknown_route_uses_error_envelope(client):
    resp = client.get("/v1/no-such-route")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_openapi_is_available(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/v1/searches" in paths
    assert "/v1/analyses/{job_id}/result" in paths
    assert "/v1/intakes/blank" in paths
    assert "/v1/laws/{law_id}/web-related" in paths


def test_service_layer_lives_here_not_in_engine():
    """服务契约层、文档读取层与运行时快照都住在本项目里。

    引擎目录里出现这两个模块就说明适配器与引擎又缠回去了：适配器会跟着
    引擎的提交一起变，四个项目的独立性就没有了。
    """
    project_root = Path(__file__).resolve().parents[1]
    assert Path(engine.tax_service.__file__).is_relative_to(project_root / "src")
    assert Path(engine.tax_service.tax_doc.__file__).is_relative_to(project_root / "src")
    assert engine.ENGINE_ROOT.is_relative_to(project_root / "src")
    assert (engine.ENGINE_ROOT / "MANIFEST.json").is_file()
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_service.py").exists()
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_doc.py").exists()


def test_runtime_snapshot_matches_manifest():
    """运行时快照逐文件与清单里的 SHA-256 对齐。

    少一个脚本、改一个字节都在这里报红，而不是等某个取数分支到运行时才炸。
    """
    runtime = engine.ENGINE_ROOT
    manifest = json.loads((runtime / "MANIFEST.json").read_text(encoding="utf-8"))
    listed = manifest["files"]
    actual = {
        path.relative_to(runtime).as_posix()
        for folder in ("scripts", "data", "references")
        for path in (runtime / folder).glob("*")
        if path.is_file()
    }
    assert set(listed) == actual
    bad = [rel for rel, digest in listed.items()
           if hashlib.sha256((runtime / rel).read_bytes()).hexdigest() != digest]
    assert not bad, bad


def test_no_external_engine_lookup():
    """分发出去的项目不能再从环境变量或同级目录找引擎。"""
    project_root = Path(__file__).resolve().parents[1]
    package = project_root / "src" / "tax_engine_api"
    needles = ("TAX_ENGINE_ROOT", "find_engine_root")
    offenders = []
    for path in package.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders.extend(f"{path.relative_to(project_root).as_posix()}: {needle}"
                         for needle in needles if needle in text)
    assert offenders == []


def test_vendored_copies_match_fingerprint():
    """三个项目各带一份副本，指纹相同才算同一份。

    改动任一处都要同步另外两个项目；先在这里报红，而不是等到某个适配器
    的行为跟别人不一样。
    """
    import hashlib

    pins = {
        "tax_service": "aa156a7db79d44a706c4fba9561b27ba7e71fedcfadd717976b54bdf5cb0fde9",
        "tax_doc": "4ae92d52346fddb1b660b85836a7d8d7cadde5961025369535d262519cff35a0",
    }
    got = {
        "tax_service": hashlib.sha256(
            Path(engine.tax_service.__file__).read_bytes()).hexdigest(),
        "tax_doc": hashlib.sha256(
            Path(engine.tax_service.tax_doc.__file__).read_bytes()).hexdigest(),
    }
    assert got == pins, got
