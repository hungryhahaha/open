#!/usr/bin/env python3
"""AI 데일리 브리프 생성기 (AX 기획자용).

ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY 중 **하나만** 있으면 동작한다.
각 제공자의 내장 웹검색 도구로 최근 24~48시간 AI 소식을 조사해 AX 기획자가 꼭 알아야 할
3~5개와 '오늘의 개념' 1개를 고른 뒤, data/briefs/YYYY-MM-DD.json 과 data/index.json 을 갱신한다.

사용:
  python ai-brief/scripts/daily_brief.py                 # 오늘(KST) 브리핑 생성
  python ai-brief/scripts/daily_brief.py --date 2026-11-04
  python ai-brief/scripts/daily_brief.py --provider openai
  python ai-brief/scripts/daily_brief.py --mock          # API 없이 파이프라인 점검(예시 데이터)
  python ai-brief/scripts/daily_brief.py --rebuild-index # briefs/*.json 으로 index.json 만 재생성

환경 변수 (로컬에서는 ai-brief/.env 또는 저장소 루트 .env 에 두면 자동으로 읽는다. 둘 다 .gitignore 대상):
  BRIEF_PROVIDER      anthropic | openai | gemini (비우면 키가 있는 첫 제공자 자동 선택)
  ANTHROPIC_MODEL / OPENAI_MODEL / GEMINI_MODEL  모델 ID 강제 지정(비우면 기본 후보를 차례로 시도)
  BRIEF_MAX_SEARCHES  검색 호출 상한 (기본 10, 4~20)
  BRIEF_MIN_SEARCHES  이보다 검색을 적게 한 초안은 반려하고 재시도 (기본 6)
  BRIEF_EFFORT        추론 강도 low | medium | high (기본 medium)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]  # ai-brief/
DEFAULT_DATA_DIR = ROOT / "data"
KST = dt.timezone(dt.timedelta(hours=9), "KST")

CATEGORIES = ("model", "market", "policy", "practice", "tech", "korea")
CATEGORY_ALIASES = {
    "모델·제품": "model",
    "모델": "model",
    "제품": "model",
    "product": "model",
    "models": "model",
    "시장·투자": "market",
    "시장": "market",
    "투자": "market",
    "investment": "market",
    "정책·규제": "policy",
    "정책": "policy",
    "규제": "policy",
    "regulation": "policy",
    "도입·실무": "practice",
    "도입": "practice",
    "실무": "practice",
    "adoption": "practice",
    "case": "practice",
    "enterprise": "practice",
    "기술·개념": "tech",
    "기술": "tech",
    "개념": "tech",
    "technology": "tech",
    "concept": "tech",
    "국내 동향": "korea",
    "국내": "korea",
    "한국": "korea",
    "domestic": "korea",
    "kr": "korea",
}
MIN_ITEMS, MAX_ITEMS = 3, 5
MAX_PER_CATEGORY = 2
INDEX_LIMIT = 400  # index.json 에 담는 최대 브리핑 수 (약 1년치 이상)
RECENT_FOR_PROMPT = 10

PROVIDER_ORDER = ("anthropic", "openai", "gemini")
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
MODEL_ENV = {
    "anthropic": "ANTHROPIC_MODEL",
    "openai": "OPENAI_MODEL",
    "gemini": "GEMINI_MODEL",
}
# 앞에서부터 시도하고, 모델이 없다는 오류(404 등)가 나면 다음 후보로 넘어간다.
# 비용을 고려한 '절약형' 기본값. 더 좋은 모델을 쓰려면 *_MODEL 환경 변수로 지정한다
# (예: ANTHROPIC_MODEL=claude-opus-5-5, OPENAI_MODEL=gpt-6.1-sol, GEMINI_MODEL=gemini-3.1-pro-preview).
MODEL_CHAINS = {
    "anthropic": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-sonnet-4-6"],
    "openai": ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-5.6-sol", "gpt-5.5"],
    "gemini": [
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-2.5-pro",
    ],
}
MAX_OUTPUT_TOKENS = 24000
# 검색 호출 수 상한과 추론 강도. 비용의 큰 부분이라 환경 변수로 조절한다.
MAX_SEARCHES = max(4, min(20, int(os.environ.get("BRIEF_MAX_SEARCHES") or 10)))
EFFORT = (os.environ.get("BRIEF_EFFORT") or "medium").strip().lower()
if EFFORT not in ("low", "medium", "high"):
    EFFORT = "medium"
# 이보다 검색을 적게 하고 쓴 초안은 반려하고 다시 시킨다(품질 가드).
MIN_SEARCHES = max(0, min(MAX_SEARCHES, int(os.environ.get("BRIEF_MIN_SEARCHES") or 6)))
# 모델이 검색만 하고 최종 JSON 없이 턴을 끝내면, 같은 대화(검색 결과가 남아 있는 상태)에서 이어서 쓰게 한다.
CONTINUE_MSG = (
    "조사한 내용을 바탕으로 지금 바로 최종 결과를 작성하세요. 추가 검색은 꼭 필요할 때만 하고, "
    "마지막에는 반드시 지정된 구조의 JSON 하나를 ```json 펜스 안에 출력하고 끝내세요. "
    "질문이나 확인 요청은 하지 마세요."
)
MAX_CONTINUATIONS = 2
# 요약에 이런 회피 문구가 있으면 사실 확인을 안 한 것이므로 그 항목은 버린다.
VAGUE_PHRASES = (
    "원문에서 확인",
    "확인해야 합니다",
    "확인이 필요합니다",
    "자세한 내용은",
    "원문을 참고",
)
# 10일 넘게 지난 소식은 '배경' 성격으로 하루 1개까지만 허용한다.
MAX_STALE_DAYS = 10
MAX_STALE_ITEMS = 1
# 뉴스레터·집계·커뮤니티 사이트는 출처로 인정하지 않는다(원문을 찾아 인용해야 함).
AGGREGATOR_DOMAINS = {
    "theneuron.ai",
    "aiweekly.co",
    "substack.com",
    "dev.to",
    "medium.com",
    "linkedin.com",
    "reddit.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "youtube.com",
    "wikipedia.org",
    "biztoc.com",
    "digg.com",
    "manifold.markets",
    "news.ycombinator.com",
    "tldr.tech",
    "beehiiv.com",
    "explainx.ai",
    "aitoolsrecap.com",
    "simplifyaitools.com",
}

SYSTEM_PROMPT = """당신은 AX(AI 전환) 기획자 한 사람만을 위한 'AI 데일리 브리프'의 편집자입니다.
목표: 사용자가 매일 3분 안에 꼭 알아야 할 AI 소식을 읽고, 꾸준히 쌓아 AI 전문가가 되게 하는 것.

## 조사 원칙
- 웹검색 도구를 최소 8회 이상, 서로 다른 각도로 사용합니다: 종합 AI 뉴스(영문), 주요 벤더(OpenAI·Anthropic·Google·Microsoft·Meta 등) 발표, 기업 도입 사례·보고서, 규제·정책(EU AI Act, 미국, 한국 AI기본법 등), 국내 AX 동향(한국어 검색), 핵심 수치 확인용 검색.
- 숫자·날짜·회사명은 1차 출처(공식 발표, 공시, 정부 자료)가 있거나 신뢰할 수 있는 매체 2곳 이상이 일치할 때만 씁니다.
- 출처 다양성: 항목마다 서로 다른 매체를 인용합니다. 뉴스레터·집계·커뮤니티 사이트(The Neuron, Substack, dev.to, Medium, Reddit, LinkedIn 등)는 출처로 쓰지 않습니다. 거기서 본 소식은 원문(벤더 공식 블로그, 공시, 정부 자료, Reuters·Bloomberg·연합뉴스 같은 주요 매체)을 다시 검색해 인용합니다.
- 예측시장, 루머, 출처 불명 SEO 사이트, 미확인 모델명은 쓰지 않습니다. 확인이 안 되는 수치는 '보도 기준'이라고 표시합니다.
- 최근 24~48시간 소식을 우선하되, 느린 날에는 억지로 채우지 말고 최근 발표된 신뢰할 수 있는 보고서나 기획자에게 중요한 배경 지식으로 최소 3개를 맞춥니다.
- 이전 브리핑에 이미 실린 소식은 큰 후속 변화가 있을 때만 다루고, 그 경우 제목에 '(후속)'을 붙입니다.

## 선별 기준 (가장 중요)
AX 기획자가 '몰라도 되는 것'은 과감히 버리고 3~5개만 고릅니다.
- 포함: 주요 모델·제품 출시와 가격 변화, 기업 도입 사례와 성과 수치, 규제·정책 변화, 전략에 영향을 주는 시장·투자 동향, 국내 AX 동향, 기획자가 알아야 할 기술 개념의 전환점.
- 제외: 특정 개발자만 쓰는 세부 기능, 소규모 스타트업 펀딩, 연예성 기사, 단순 벤치마크 수치 갱신, 개인 의견 칼럼.
- 카테고리를 섞습니다. 같은 카테고리가 3개 이상이면 안 됩니다.

## 작성 규칙 (모두 한국어, 존댓말)
- title: 핵심이 드러나는 제목, 40자 내외, 숫자 포함 권장.
- summary: 사실 중심 2~4문장, 수치·날짜·주체를 명확히, 150~250자. 고른 소식은 web_fetch(또는 확인 검색)로 원문을 읽고 씁니다. '원문에서 확인해야 합니다', '자세한 내용은 …' 같은 회피 문구 금지. 구체적 사실이 없으면 그 소식은 고르지 않습니다.
- 최근성: 최근 24~48시간 소식이 기본입니다. 10일 넘게 지난 소식은 '배경'으로 하루 1개까지만 허용합니다.
- why: '기획자 시사점' 2~3문장. 이 소식이 AX 과제 기획·벤더 선택·거버넌스·예산에 어떤 행동 변화를 요구하는지 구체적으로, 120~200자.
- source_url: 검색 결과에 실제로 있던 URL만 씁니다. 지어내지 않습니다. 가능하면 1차 출처나 주요 매체.
- concept(오늘의 개념): 이전 브리핑에서 다룬 적 없는 용어 1개. 비전문가도 이해할 비유 1개를 포함한 3~4문장(200~300자)과, 기획자가 알아야 하는 이유 1~2문장(가능하면 오늘 소식과 연결).
  후보 순서 예시: 에이전트 오케스트레이션, 컨텍스트 윈도우, 파인튜닝 vs 프롬프트, 평가(evals), 가드레일, 추론 모델, 멀티모달, 토큰 경제학, AI 거버넌스 프레임워크, 컴퓨터 사용 에이전트, 합성 데이터, 증류, 온디바이스 AI, 벡터 DB, RAG, MCP.
- 추측, 과장, 광고성 표현 금지.

## 출력 형식
조사가 끝나면 마지막에 아래 구조의 JSON 하나를 ```json 코드 펜스 안에 출력합니다. 펜스 밖에는 한두 줄 메모만 허용됩니다.
{
  "date": "YYYY-MM-DD",
  "oneliner": "오늘 소식 전체를 꿰는 한 문장 (60자 이내)",
  "items": [
    {
      "category": "model | market | policy | practice | tech | korea 중 하나 (영문 키 그대로)",
      "title": "...",
      "summary": "...",
      "why": "...",
      "source_name": "출처 매체명 (예: Reuters, Anthropic, 연합뉴스)",
      "source_date": "MM.DD",
      "source_url": "https://..."
    }
  ],
  "concept": {"term": "...", "en": "한 줄 부제(영문 원어 또는 짧은 정의)", "body": "...", "why": "..."}
}
카테고리 의미: model=모델·제품, market=시장·투자, policy=정책·규제, practice=도입·실무, tech=기술·개념, korea=국내 동향."""


# ----------------------------------------------------------------------------- 유틸
def log(msg: str) -> None:
    print(f"[{dt.datetime.now(KST).strftime('%H:%M:%S')}] {msg}", flush=True)


def today_kst() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m-%d")


def now_iso_kst() -> str:
    return dt.datetime.now(KST).replace(microsecond=0).isoformat()


# API 키는 코드·워크플로·커밋에 넣지 않는다. 로컬에서는 아래 .env 파일(둘 다 .gitignore 대상)에서 읽고,
# GitHub Actions 에서는 암호화된 Secrets 가 환경 변수로 들어온다.
ENV_FILES = (ROOT / ".env", ROOT.parent / ".env")


def load_env_file(path: Path, override: bool = False) -> int:
    """KEY=VALUE 형식의 .env 를 환경 변수에 넣는다. 이미 있는 변수는 유지. 넣은 개수를 돌려준다."""
    if not path.is_file():
        return 0
    count = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or not value or (key in os.environ and not override):
            continue
        os.environ[key] = value
        count += 1
    return count


def warn_if_tracked(path: Path) -> None:
    """.env 가 실수로 git 에 올라가 있으면 크게 경고한다."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", path.name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode == 0:
        log(
            f"경고: {path} 가 git 에 추적되고 있습니다! `git rm --cached {path}` 로 빼고 키를 재발급하세요."
        )


def load_env_files() -> None:
    for path in ENV_FILES:
        n = load_env_file(path)
        if n:
            log(f"{path} 에서 설정 {n}개 로드 (키 값은 출력하지 않음)")
            warn_if_tracked(path)


def normalize_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, p.query, ""))


def registered_domain(url: str) -> str:
    host = urlsplit(normalize_url(url) or "").netloc
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    second_level = {
        "co",
        "com",
        "or",
        "go",
        "ne",
        "ac",
        "org",
        "net",
        "gov",
        "edu",
        "re",
        "pe",
    }
    if len(labels[-1]) == 2 and labels[-2] in second_level:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def extract_json(text: str) -> dict | None:
    """응답 텍스트에서 마지막 ```json 펜스(없으면 마지막 최상위 객체)를 파싱한다."""
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = list(reversed(fences))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ----------------------------------------------------------------------------- 검증
def days_old(source_date: str, date: str) -> int | None:
    """'MM.DD' 또는 'YYYY-MM-DD' 형식의 출처 날짜가 브리핑 날짜보다 며칠 전인지. 모르면 None."""
    sd = str(source_date or "").strip()
    try:
        today = dt.date.fromisoformat(date)
    except ValueError:
        return None
    m = re.fullmatch(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", sd)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})", sd)
        if not m:
            return None
        mo, d = (int(x) for x in m.groups())
        y = today.year
    try:
        when = dt.date(y, mo, d)
    except ValueError:
        return None
    if when > today + dt.timedelta(days=7):  # 연말·연초 경계: 작년 날짜
        when = when.replace(year=when.year - 1)
    return (today - when).days


def validate_brief(
    raw: dict,
    date: str,
    seen_urls: set[str],
    recent_titles: set[str] | None = None,
) -> tuple[dict | None, list[str]]:
    """모델 출력을 페이지 스키마로 정리한다. 통과하지 못한 항목은 problems 에 이유를 남긴다."""
    problems: list[str] = []
    recent_titles = recent_titles or set()
    seen_norm = {normalize_url(u) for u in seen_urls}
    seen_norm.discard("")
    seen_domains = {registered_domain(u) for u in seen_norm}

    def text(v, limit: int) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]

    items_in = raw.get("items") if isinstance(raw.get("items"), list) else []
    items: list[dict] = []
    per_cat: dict[str, int] = {}
    used_domains: set[str] = set()
    stale_items = 0
    for i, it in enumerate(items_in, 1):
        if not isinstance(it, dict):
            problems.append(f"item {i}: 객체가 아님")
            continue
        cat = str(it.get("category", "")).strip()
        cat = (
            cat
            if cat in CATEGORIES
            else CATEGORY_ALIASES.get(cat.lower(), CATEGORY_ALIASES.get(cat, ""))
        )
        title, summary, why = (
            text(it.get("title"), 120),
            text(it.get("summary"), 600),
            text(it.get("why"), 500),
        )
        url = normalize_url(str(it.get("source_url", "")))
        if not cat:
            problems.append(f"item {i}: category 가 허용 값이 아님 ({it.get('category')!r})")
            continue
        if not (title and summary and why):
            problems.append(f"item {i}: title/summary/why 중 비어 있음")
            continue
        if not url:
            problems.append(f"item {i}: source_url 이 http(s) URL 이 아님")
            continue
        if seen_norm and url not in seen_norm and registered_domain(url) not in seen_domains:
            problems.append(f"item {i}: source_url 이 검색 결과에 없음 ({url})")
            continue
        dom = registered_domain(url)
        if dom in AGGREGATOR_DOMAINS:
            problems.append(
                f"item {i}: 뉴스레터/집계 사이트 출처({dom})는 인정하지 않음, 원문 출처 필요"
            )
            continue
        if dom in used_domains:
            problems.append(f"item {i}: 같은 출처({dom}) 중복, 다른 매체 필요")
            continue
        if title in recent_titles:
            problems.append(f"item {i}: 이전 브리핑과 제목이 같음")
            continue
        if any(ph in summary or ph in why for ph in VAGUE_PHRASES):
            problems.append(
                f"item {i}: 요약이 구체적이지 않음(회피 문구). 원문을 읽고 수치·날짜를 넣어야 함"
            )
            continue
        age = days_old(str(it.get("source_date", "")), date)
        if age is not None and age > MAX_STALE_DAYS:
            if stale_items >= MAX_STALE_ITEMS:
                problems.append(
                    f"item {i}: {age}일 지난 소식은 하루 {MAX_STALE_ITEMS}개까지만. 최근 소식으로 교체"
                )
                continue
            stale_items += 1
        if per_cat.get(cat, 0) >= MAX_PER_CATEGORY:
            problems.append(f"item {i}: '{cat}' 카테고리 초과로 제외")
            continue
        per_cat[cat] = per_cat.get(cat, 0) + 1
        used_domains.add(dom)
        items.append(
            {
                "category": cat,
                "title": title,
                "summary": summary,
                "why": why,
                "source_name": text(it.get("source_name"), 60),
                "source_date": text(it.get("source_date"), 12),
                "source_url": url,
            }
        )
        if len(items) >= MAX_ITEMS:
            break

    concept_in = raw.get("concept") if isinstance(raw.get("concept"), dict) else {}
    concept = {
        "term": text(concept_in.get("term"), 80),
        "en": text(concept_in.get("en"), 120),
        "body": text(concept_in.get("body"), 700),
        "why": text(concept_in.get("why"), 400),
    }
    if not (concept["term"] and concept["body"]):
        problems.append("concept: term/body 가 비어 있음")
        concept = {}

    if len(items) < MIN_ITEMS:
        problems.append(f"유효한 항목이 {len(items)}개뿐 (최소 {MIN_ITEMS}개)")
        return None, problems

    brief = {
        "date": date,
        "oneliner": text(raw.get("oneliner"), 120),
        "items": items,
        "concept": concept,
    }
    return brief, problems


# ----------------------------------------------------------------------------- 저장소
def load_briefs(data_dir: Path) -> list[dict]:
    out = []
    for p in sorted((data_dir / "briefs").glob("*.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log(f"경고: {p.name} 을 읽지 못해 건너뜀 ({e})")
            continue
        if isinstance(b, dict) and b.get("date"):
            out.append(b)
    out.sort(key=lambda b: str(b["date"]), reverse=True)
    return out


def build_index(data_dir: Path) -> Path:
    briefs = load_briefs(data_dir)[:INDEX_LIMIT]
    payload = {"updated_at": now_iso_kst(), "count": len(briefs), "briefs": briefs}
    data_dir.mkdir(parents=True, exist_ok=True)
    index = data_dir / "index.json"
    index.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return index


def save_brief(data_dir: Path, brief: dict) -> Path:
    briefs_dir = data_dir / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"{brief['date']}.json"
    path.write_text(json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def recent_context(briefs: list[dict], limit: int = RECENT_FOR_PROMPT) -> str:
    if not briefs:
        return "(아직 없음)"
    lines = []
    for b in briefs[:limit]:
        titles = "; ".join(
            str(i.get("title", "")) for i in b.get("items", []) if isinstance(i, dict)
        )
        term = (b.get("concept") or {}).get("term", "")
        lines.append(f"- {b.get('date')}: {titles} | 개념: {term}")
    return "\n".join(lines)


def build_user_prompt(date: str, briefs: list[dict], extra: str = "") -> str:
    note = f"\n\n## 주의\n{extra}" if extra else ""
    return f"""오늘 날짜(한국 시간): {date}

## 최근 브리핑 (중복 금지, 개념도 겹치지 않게)
{recent_context(briefs)}

## 추천 검색어 (각각 실제로 검색한 뒤, 후보 소식의 수치를 확인하는 검색을 더 합니다)
1. "AI news {date}" 와 "biggest AI news this week"
2. OpenAI 최근 발표 / Anthropic·Claude 최근 발표
3. Google Gemini 최근 발표 / Microsoft Copilot / Meta AI
4. enterprise AI agents adoption report / AI ROI case study (최근 보고서·사례)
5. AI regulation (EU AI Act, 미국) / 한국 AI 기본법·정부 정책
6. 한국어: "AI 뉴스 오늘", "AX 기업 도입 사례"
7. 후보로 고른 소식마다 원문(벤더 공식 블로그, 공시, 주요 매체) 확인 검색

## 할 일
1. 위 검색어를 포함해 웹검색을 최소 8회 이상 수행해 최근 24~48시간의 AI 소식을 조사합니다.
2. 선별 기준에 따라 3~5개를 고르고(항목마다 다른 매체, 집계 사이트 금지, 10일 넘은 소식은 1개까지), 고른 소식마다 원문을 읽어 수치·날짜를 요약에 넣습니다. '오늘의 개념' 1개를 정합니다.
3. 마지막에 지정된 JSON 하나를 ```json 펜스 안에 출력합니다. "date" 는 "{date}" 로 씁니다.{note}"""


def build_repair_prompt(
    draft_text: str, problems: list[str], seen_urls: set[str], date: str
) -> str:
    urls = (
        "\n".join(sorted(u for u in seen_urls if u)[:120])
        or "(목록 없음: 초안에 있는 URL 을 그대로 유지)"
    )
    return f"""아래 '초안'은 방금 생성된 브리핑이고, '검증 오류'는 형식 검사에서 걸린 내용입니다.
새로운 사실을 추가하지 말고 형식만 고쳐 지정된 구조의 JSON 하나만 ```json 펜스 안에 출력하세요.
- category 는 model/market/policy/practice/tech/korea 영문 키만 사용.
- source_url 은 반드시 아래 '확인된 URL 목록'에 있는 것(또는 같은 도메인)만 사용. 맞는 URL 이 없으면 그 항목은 삭제.
- 항목마다 서로 다른 매체(도메인)를 인용. 뉴스레터·집계·커뮤니티 사이트(The Neuron, Substack, dev.to, Medium, Reddit, LinkedIn 등) URL 은 쓰지 않음.
- 항목은 최소 3개, 최대 5개. "date" 는 "{date}".

## 검증 오류
{chr(10).join("- " + p for p in problems) or "- (없음)"}

## 확인된 URL 목록
{urls}

## 초안
{draft_text[-12000:]}"""


# ----------------------------------------------------------------------------- 제공자
class ModelUnavailable(Exception):
    """모델 ID 가 없거나 이 키로 쓸 수 없을 때. 다음 후보 모델로 넘어간다."""


def _looks_like_missing_model(err: Exception) -> bool:
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    msg = str(err).lower()
    return status == 404 or any(
        s in msg
        for s in (
            "not_found",
            "not found",
            "does not exist",
            "unsupported model",
            "unknown model",
            "no model",
        )
    )


def run_anthropic(model: str, system: str, user: str, use_tools: bool) -> tuple[str, set[str], int]:
    import anthropic

    client = anthropic.Anthropic()
    # Haiku 계열은 기본형 web_search_20250305 만 지원한다(web_fetch 는 생략).
    legacy = "haiku" in model
    search_tool = {
        "type": "web_search_20250305" if legacy else "web_search_20260209",
        "name": "web_search",
        "max_uses": MAX_SEARCHES,
        "user_location": {
            "type": "approximate",
            "country": "KR",
            "timezone": "Asia/Seoul",
        },
    }
    fetch_tool = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4}
    tools = ([search_tool] + ([] if legacy else [fetch_tool])) if use_tools else []
    messages: list[dict] = [{"role": "user", "content": user}]
    seen: set[str] = set()
    texts: list[str] = []
    searches = 0
    continuations = 0

    def call(extra: dict):
        with client.beta.messages.stream(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=tools,
            output_config={"effort": EFFORT},
            **extra,
        ) as stream:
            return stream.get_final_message()

    # Claude Opus 5.5 의 서버측 안전장치 폴백(refusal 시 다른 모델로 이어서 답함)을 기본으로 켜고,
    # 이 베타를 받지 않는 환경이면 같은 요청을 폴백 없이 다시 보낸다.
    extras = [
        {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"},
        {},
    ]
    for _ in range(12):  # pause_turn(서버측 도구 반복 한도) 과 이어쓰기
        last_err: Exception | None = None
        resp = None
        for extra in extras:
            try:
                resp = call(extra)
                extras = [extra]
                break
            except anthropic.NotFoundError as e:
                raise ModelUnavailable(str(e)) from e
            except anthropic.BadRequestError as e:
                last_err = e
                if _looks_like_missing_model(e):
                    raise ModelUnavailable(str(e)) from e
                continue
        if resp is None:
            raise RuntimeError(f"Anthropic 요청 실패: {last_err}")
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):
                    for r in content:
                        if getattr(r, "url", None):
                            seen.add(r.url)
            elif btype == "web_fetch_tool_result":
                content = getattr(block, "content", None)
                if getattr(content, "url", None):
                    seen.add(content.url)
            elif btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
                searches += 1
            elif btype == "text" and getattr(block, "text", ""):
                texts.append(block.text)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Anthropic refusal: {getattr(resp, 'stop_details', None)}")
        usage = getattr(resp, "usage", None)
        if usage:
            log(
                f"anthropic usage: in={usage.input_tokens} out={usage.output_tokens} "
                f"model={resp.model} stop={resp.stop_reason}"
            )
        if (
            use_tools
            and extract_json("\n".join(texts)) is None
            and continuations < MAX_CONTINUATIONS
        ):
            continuations += 1
            log(
                f"최종 JSON 없이 턴 종료, 같은 대화에서 이어쓰기 {continuations}/{MAX_CONTINUATIONS}"
            )
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": CONTINUE_MSG})
            continue
        break
    return "\n".join(texts), seen, searches


def run_openai(model: str, system: str, user: str, use_tools: bool) -> tuple[str, set[str], int]:
    import openai

    client = openai.OpenAI()
    tools = (
        [
            {
                "type": "web_search",
                "search_context_size": "medium" if EFFORT != "high" else "high",
                "user_location": {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"},
            }
        ]
        if use_tools
        else []
    )
    variants = [
        {
            "tools": tools,
            "reasoning": {"effort": EFFORT},
            "include": ["web_search_call.action.sources"],
        },
        {"tools": tools, "reasoning": {"effort": EFFORT}},
        {"tools": tools},
    ]
    seen: set[str] = set()
    texts: list[str] = []
    searches = 0
    continuations = 0
    chosen: dict | None = None
    prev_id: str | None = None
    pending_input = user
    for _ in range(1 + MAX_CONTINUATIONS):
        resp = None
        last_err: Exception | None = None
        for extra in [chosen] if chosen is not None else variants:
            kwargs = {
                "model": model,
                "instructions": system,
                "input": pending_input,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                **extra,
            }
            if prev_id:
                kwargs["previous_response_id"] = prev_id
            try:
                resp = client.responses.create(**kwargs)
                chosen = extra
                break
            except openai.NotFoundError as e:
                raise ModelUnavailable(str(e)) from e
            except openai.BadRequestError as e:
                if _looks_like_missing_model(e):
                    raise ModelUnavailable(str(e)) from e
                last_err = e
                continue
        if resp is None:
            raise RuntimeError(f"OpenAI 요청 실패: {last_err}")
        parts: list[str] = []
        for item in getattr(resp, "output", None) or []:
            itype = getattr(item, "type", "")
            if itype == "web_search_call":
                searches += 1
                action = getattr(item, "action", None)
                for src in getattr(action, "sources", None) or []:
                    if getattr(src, "url", None):
                        seen.add(src.url)
                if getattr(action, "url", None):
                    seen.add(action.url)
            elif itype == "message":
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "text", None):
                        parts.append(c.text)
                    for a in getattr(c, "annotations", None) or []:
                        if getattr(a, "type", "") == "url_citation" and getattr(a, "url", None):
                            seen.add(a.url)
        texts.append(getattr(resp, "output_text", None) or "\n".join(parts))
        usage = getattr(resp, "usage", None)
        if usage:
            log(
                f"openai usage: in={usage.input_tokens} out={usage.output_tokens} model={resp.model}"
            )
        prev_id = getattr(resp, "id", None)
        if (
            use_tools
            and prev_id
            and extract_json("\n".join(texts)) is None
            and continuations < MAX_CONTINUATIONS
        ):
            continuations += 1
            log(
                f"최종 JSON 없이 응답 종료, 같은 대화에서 이어쓰기 {continuations}/{MAX_CONTINUATIONS}"
            )
            pending_input = CONTINUE_MSG
            continue
        break
    return "\n".join(texts), seen, searches


def run_gemini(model: str, system: str, user: str, use_tools: bool) -> tuple[str, set[str], int]:
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_tools else []
    variants = [
        {"thinking_config": types.ThinkingConfig(thinking_level=EFFORT)},
        {"thinking_config": types.ThinkingConfig(thinking_budget=-1)},
        {},
    ]
    contents = [types.Content(role="user", parts=[types.Part(text=user)])]
    seen: set[str] = set()
    texts: list[str] = []
    searches = 0
    continuations = 0
    chosen: dict | None = None
    for _ in range(1 + MAX_CONTINUATIONS):
        resp = None
        last_err: Exception | None = None
        for extra in [chosen] if chosen is not None else variants:
            cfg = types.GenerateContentConfig(
                system_instruction=system, tools=tools, max_output_tokens=MAX_OUTPUT_TOKENS, **extra
            )
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=cfg)
                chosen = extra
                break
            except errors.ClientError as e:
                if _looks_like_missing_model(e):
                    raise ModelUnavailable(str(e)) from e
                last_err = e
                continue
        if resp is None:
            raise RuntimeError(f"Gemini 요청 실패: {last_err}")
        for cand in getattr(resp, "candidates", None) or []:
            gm = getattr(cand, "grounding_metadata", None)
            searches += len(getattr(gm, "web_search_queries", None) or [])
            for ch in getattr(gm, "grounding_chunks", None) or []:
                web = getattr(ch, "web", None)
                if not web:
                    continue
                if getattr(web, "uri", None):
                    seen.add(web.uri)
                if getattr(web, "domain", None):
                    seen.add(f"https://{web.domain}/")
        texts.append(getattr(resp, "text", None) or "")
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            log(
                f"gemini usage: in={getattr(usage, 'prompt_token_count', '?')} "
                f"out={getattr(usage, 'candidates_token_count', '?')} model={model}"
            )
        if (
            use_tools
            and extract_json("\n".join(texts)) is None
            and continuations < MAX_CONTINUATIONS
        ):
            continuations += 1
            log(
                f"최종 JSON 없이 응답 종료, 같은 대화에서 이어쓰기 {continuations}/{MAX_CONTINUATIONS}"
            )
            cand0 = (getattr(resp, "candidates", None) or [None])[0]
            if cand0 is not None and getattr(cand0, "content", None):
                contents.append(cand0.content)
            contents.append(types.Content(role="user", parts=[types.Part(text=CONTINUE_MSG)]))
            continue
        break
    return "\n".join(texts), seen, searches


RUNNERS: dict[str, Callable[[str, str, str, bool], tuple[str, set[str], int]]] = {
    "anthropic": run_anthropic,
    "openai": run_openai,
    "gemini": run_gemini,
}


def pick_provider(explicit: str | None) -> str:
    if explicit:
        if explicit not in RUNNERS:
            raise SystemExit(f"알 수 없는 제공자: {explicit} (anthropic | openai | gemini)")
        if not os.environ.get(KEY_ENV[explicit]):
            raise SystemExit(f"{KEY_ENV[explicit]} 가 비어 있습니다.")
        return explicit
    for p in PROVIDER_ORDER:
        if os.environ.get(KEY_ENV[p]):
            return p
    raise SystemExit(
        "API 키가 없습니다. ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY 중 하나를 "
        "ai-brief/.env (gitignore 대상) 또는 GitHub Secrets 에 설정하세요."
    )


def model_candidates(provider: str) -> list[str]:
    forced = os.environ.get(MODEL_ENV[provider], "").strip()
    return [forced] if forced else list(MODEL_CHAINS[provider])


def run_with_model_chain(
    provider: str, system: str, user: str, use_tools: bool
) -> tuple[str, set[str], int, str]:
    runner = RUNNERS[provider]
    errors: list[str] = []
    for model in model_candidates(provider):
        try:
            log(f"{provider}/{model} 호출 (tools={'on' if use_tools else 'off'})")
            text, seen, searches = runner(model, system, user, use_tools)
            return text, seen, searches, model
        except ModelUnavailable as e:
            errors.append(f"{model}: {str(e)[:160]}")
            log(f"{provider}/{model} 사용 불가, 다음 후보로: {str(e)[:160]}")
    raise RuntimeError(f"{provider}: 쓸 수 있는 모델이 없습니다.\n" + "\n".join(errors))


# ----------------------------------------------------------------------------- 생성
def generate(provider: str, date: str, briefs: list[dict], attempts: int = 3) -> dict:
    recent_titles = {
        str(i.get("title", "")) for b in briefs for i in b.get("items", []) if isinstance(i, dict)
    }
    user = build_user_prompt(date, briefs)
    last_problems: list[str] = []
    for attempt in range(1, attempts + 1):
        log(f"생성 시도 {attempt}/{attempts}")
        text, seen, searches, model = run_with_model_chain(
            provider, SYSTEM_PROMPT, user, use_tools=True
        )
        log(f"웹검색 {searches}회, 확인된 URL {len(seen)}개, 응답 {len(text)}자")
        if searches < MIN_SEARCHES:
            last_problems = [f"웹검색이 {searches}회뿐 (최소 {MIN_SEARCHES}회)"]
            log("이번 시도 반려: " + last_problems[0])
            user = build_user_prompt(
                date,
                briefs,
                extra=(
                    f"이전 시도는 웹검색이 {searches}회에 그쳐 반려되었습니다. "
                    f"이번에는 추천 검색어를 각각 실제로 검색해 최소 {max(MIN_SEARCHES, 8)}회 이상 "
                    "검색한 뒤에만 작성하세요."
                ),
            )
            time.sleep(3)
            continue
        raw = extract_json(text)
        brief, problems = (
            (None, ["JSON 을 찾지 못함"])
            if raw is None
            else validate_brief(raw, date, seen, recent_titles)
        )
        if brief is None:
            log("1차 검증 실패: " + "; ".join(problems))
            repair = build_repair_prompt(text, problems, seen, date)
            text2, _, _, model = run_with_model_chain(
                provider, SYSTEM_PROMPT, repair, use_tools=False
            )
            raw2 = extract_json(text2)
            brief, problems = (
                (None, ["수정본에서도 JSON 을 찾지 못함"])
                if raw2 is None
                else validate_brief(raw2, date, seen, recent_titles)
            )
        if brief is not None:
            if problems:
                log("경고(제외된 항목): " + "; ".join(problems))
            brief["generated_at"] = now_iso_kst()
            brief["generated_by"] = f"{provider}/{model}"
            return brief
        last_problems = problems
        log("이번 시도 실패: " + "; ".join(problems))
        time.sleep(5)
    raise RuntimeError("브리핑 생성 실패: " + "; ".join(last_problems))


def mock_brief(date: str) -> dict:
    """API 없이 파이프라인을 점검하기 위한 예시 브리핑(내용은 예시임을 명시)."""
    return {
        "date": date,
        "oneliner": "(예시) 파이프라인 점검용 브리핑입니다. 실제 소식이 아닙니다.",
        "items": [
            {
                "category": c,
                "title": f"(예시 {i}) {label} 카테고리 항목",
                "summary": "이 항목은 --mock 실행이 만든 예시입니다. 실제 뉴스가 아니며, 페이지 렌더링과 저장 경로를 확인하는 용도입니다.",
                "why": "실제 실행에서는 이 자리에 기획자 시사점이 들어갑니다.",
                "source_name": "예시",
                "source_date": date[5:].replace("-", "."),
                "source_url": "https://example.com/mock",
            }
            for i, (c, label) in enumerate(
                [
                    ("model", "모델·제품"),
                    ("policy", "정책·규제"),
                    ("practice", "도입·실무"),
                ],
                1,
            )
        ],
        "concept": {
            "term": "(예시) 개념",
            "en": "mock concept",
            "body": "예시 개념 설명입니다. 실제 실행에서는 비유를 포함한 3~4문장이 들어갑니다.",
            "why": "예시입니다.",
        },
        "generated_at": now_iso_kst(),
        "generated_by": "mock",
    }


def write_summary(brief: dict, path_written: Path) -> None:
    lines = [
        f"## AI 데일리 브리프 {brief['date']}",
        "",
        f"**오늘 한 줄:** {brief.get('oneliner', '')}",
        "",
        *[f"- [{i['category']}] {i['title']}" for i in brief["items"]],
        "",
        f"**오늘의 개념:** {(brief.get('concept') or {}).get('term', '')}",
        "",
        f"생성: `{brief.get('generated_by', '')}` · 파일: `{path_written}`",
    ]
    text = "\n".join(lines)
    print("\n" + text + "\n", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI 데일리 브리프 생성기")
    ap.add_argument("--date", help="브리핑 날짜 YYYY-MM-DD (기본: 오늘, KST)")
    ap.add_argument("--provider", choices=list(RUNNERS), help="anthropic | openai | gemini")
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="data 디렉터리 (기본: ai-brief/data)",
    )
    ap.add_argument(
        "--mock",
        action="store_true",
        help="API 호출 없이 예시 브리핑으로 파이프라인 점검",
    )
    ap.add_argument("--rebuild-index", action="store_true", help="index.json 만 다시 만든다")
    args = ap.parse_args(argv)
    load_env_files()

    data_dir: Path = args.data_dir
    if args.rebuild_index:
        index = build_index(data_dir)
        log(f"index.json 재생성: {index}")
        return 0

    date = args.date or today_kst()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise SystemExit(f"날짜 형식 오류: {date}")
    briefs = [b for b in load_briefs(data_dir) if b.get("date") != date]

    if args.mock:
        brief = mock_brief(date)
    else:
        provider = pick_provider(args.provider or os.environ.get("BRIEF_PROVIDER") or None)
        log(f"제공자: {provider}")
        brief = generate(provider, date, briefs)

    path = save_brief(data_dir, brief)
    build_index(data_dir)
    write_summary(brief, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
