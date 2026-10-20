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


def test_service_layer_lives_here_not_in_engine():
    """服务契约层和文档读取层是本项目的副本。

    引擎目录里出现这两个模块就说明适配器与引擎又缠回去了：适配器会跟着
    引擎的提交一起变，四个项目的独立性就没有了。
    """
    project_root = Path(__file__).resolve().parents[1]
    assert Path(engine.tax_service.__file__).is_relative_to(project_root / "src")
    assert Path(engine.tax_service.tax_doc.__file__).is_relative_to(project_root / "src")
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_service.py").exists()
    assert not (engine.ENGINE_ROOT / "scripts" / "tax_doc.py").exists()
    assert engine.ENGINE_ROOT != project_root


def test_vendored_copies_match_fingerprint():
    """三个项目各带一份副本，指纹相同才算同一份。

    改动任一处都要同步另外两个项目；先在这里报红，而不是等到某个适配器
    的行为跟别人不一样。
    """
    import hashlib

    pins = {
        "tax_service": "b8235637b8937cc3c948d7ef4a8e16e887453d9d0f1ca07bbed15e54199f1841",
        "tax_doc": "ef3f8cb2b5c59181a367f368e18cb92a77ed3e9bd9441eec40cca670dd2913de",
    }
    got = {
        "tax_service": hashlib.sha256(
            Path(engine.tax_service.__file__).read_bytes()).hexdigest(),
        "tax_doc": hashlib.sha256(
            Path(engine.tax_service.tax_doc.__file__).read_bytes()).hexdigest(),
    }
    assert got == pins, got
