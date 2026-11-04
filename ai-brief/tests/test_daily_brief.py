"""ai-brief/scripts/daily_brief.py 의 순수 로직(파싱·검증·인덱스) 테스트. API 호출은 하지 않는다."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_brief.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("daily_brief", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(i, category="model", url="https://example.com/a"):
    return {
        "category": category,
        "title": f"제목 {i}",
        "summary": "요약 " * 10,
        "why": "시사점 " * 8,
        "source_name": "예시",
        "source_date": "11.03",
        "source_url": url,
    }


def test_extract_json_prefers_last_fence(mod):
    text = 'memo ```json {"a": 1} ``` more ```json\n{"date": "2026-11-03", "items": []}\n```\n끝'
    assert mod.extract_json(text) == {"date": "2026-11-03", "items": []}


def test_extract_json_without_fence(mod):
    assert mod.extract_json('x {"k": [1, 2]} y') == {"k": [1, 2]}
    assert mod.extract_json("no json here") is None


def test_validate_maps_aliases_and_checks_sources(mod):
    seen = {
        "https://www.example.com/a/",
        "https://news.site.co.kr/story/1",
        "https://fortune.com/x",
    }
    raw = {
        "oneliner": "한 줄",
        "items": [
            _item(1, "모델·제품", "https://example.com/a"),
            _item(2, "policy", "https://site.co.kr/other"),  # 같은 등록 도메인 → 통과
            _item(3, "practice", "https://unknown.example.org/x"),  # 검색 결과에 없음 → 제외
            _item(4, "korea", "https://fortune.com/x"),
            {
                "category": "market",
                "title": "",
                "summary": "s",
                "why": "w",
                "source_url": "https://example.com/a",
            },
        ],
        "concept": {"term": "RAG", "body": "설명"},
    }
    brief, problems = mod.validate_brief(raw, "2026-11-03", seen)
    assert brief is not None
    assert [i["category"] for i in brief["items"]] == ["model", "policy", "korea"]
    assert brief["date"] == "2026-11-03"
    assert brief["concept"]["term"] == "RAG"
    assert any("검색 결과에 없음" in p for p in problems)
    assert any("비어 있음" in p for p in problems)


def test_validate_requires_min_items_and_limits_category(mod):
    raw = {
        "items": [_item(i, "market") for i in range(1, 5)],
        "concept": {"term": "t", "body": "b"},
    }
    brief, problems = mod.validate_brief(raw, "2026-11-03", set())
    # market 2개까지만 허용 → 2개뿐이라 최소 3개 미달
    assert brief is None
    assert any("최소" in p for p in problems)


def test_validate_drops_repeated_titles(mod):
    raw = {
        "items": [
            _item(1, url="https://a.com/1"),
            _item(2, "policy", "https://b.com/2"),
            _item(3, "korea", "https://c.com/3"),
            _item(4, "tech", "https://d.com/4"),
        ]
    }
    brief, _ = mod.validate_brief(raw, "2026-11-03", set(), recent_titles={"제목 1"})
    assert brief is not None
    assert [i["title"] for i in brief["items"]] == ["제목 2", "제목 3", "제목 4"]


def test_mock_run_builds_files_and_index(mod, tmp_path):
    data = tmp_path / "data"
    assert mod.main(["--mock", "--date", "2026-11-02", "--data-dir", str(data)]) == 0
    assert mod.main(["--mock", "--date", "2026-11-03", "--data-dir", str(data)]) == 0
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == 2
    assert [b["date"] for b in index["briefs"]] == ["2026-11-03", "2026-11-02"]
    assert (data / "briefs" / "2026-11-03.json").exists()
    assert len(index["briefs"][0]["items"]) == 3


def test_pick_provider_uses_first_available_key(mod, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert mod.pick_provider(None) == "gemini"
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    assert mod.pick_provider(None) == "openai"
    with pytest.raises(SystemExit):
        mod.pick_provider("anthropic")  # 키 없음


def test_model_candidates_respects_override(mod, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-custom")
    assert mod.model_candidates("openai") == ["gpt-custom"]
    monkeypatch.delenv("OPENAI_MODEL")
    assert mod.model_candidates("openai")[0] == "gpt-6.1-terra"


def test_load_env_file_sets_missing_keys_only(mod, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        '# 주석\nOPENAI_API_KEY="sk-test"\nexport GEMINI_API_KEY=g1\nEMPTY=\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "keep-me")
    try:
        assert mod.load_env_file(env) == 1
        assert os.environ["OPENAI_API_KEY"] == "sk-test"
        assert os.environ["GEMINI_API_KEY"] == "keep-me"
        assert "EMPTY" not in os.environ
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
    assert mod.load_env_file(tmp_path / "missing.env") == 0


def test_env_files_are_gitignored():
    repo = Path(__file__).resolve().parents[2]
    if shutil.which("git") is None or not (repo / ".git").exists():
        pytest.skip("git 저장소가 아님")
    for rel in ("ai-brief/.env", ".env"):
        r = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", rel], check=False)
        assert r.returncode == 0, f"{rel} 가 .gitignore 에 없음: API 키가 커밋될 수 있음"
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "ai-brief/.env", ".env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == "", "실제 .env 파일이 git 에 추적되고 있음"


def test_validate_rejects_aggregators_and_duplicate_domains(mod):
    raw = {
        "items": [
            _item(1, "model", "https://theneuron.ai/digest/x"),  # 집계 사이트 → 제외
            _item(2, "policy", "https://reuters.com/a"),
            _item(3, "korea", "https://www.reuters.com/b"),  # 같은 매체 중복 → 제외
            _item(4, "market", "https://fortune.com/c"),
            _item(5, "tech", "https://anthropic.com/news/d"),
        ],
        "concept": {"term": "t", "body": "b"},
    }
    brief, problems = mod.validate_brief(raw, "2026-11-03", set())
    assert brief is not None
    assert [i["source_url"] for i in brief["items"]] == [
        "https://reuters.com/a",
        "https://fortune.com/c",
        "https://anthropic.com/news/d",
    ]
    assert any("집계" in p for p in problems)
    assert any("중복" in p for p in problems)
