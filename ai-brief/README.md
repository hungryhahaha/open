# AI 데일리 브리프 — AX 기획자용, API 키 하나로 돌아가는 자동 브리핑

매일 아침 06:45(한국 시간) GitHub Actions 가 웹을 조사해 **AX 기획자가 꼭 알아야 할 AI 소식 3~5개 + 오늘의 개념 1개**를 고르고,
GitHub Pages 에 올립니다. Claude 구독과 무관하게 **ANTHROPIC / OPENAI / GEMINI API 키 중 하나만 있으면** 동작합니다.

- 페이지: **https://hungryhahaha.github.io/open/** (휴대폰에서 열고 "홈 화면에 추가")
- 데이터: `ai-brief/data/briefs/YYYY-MM-DD.json` (하루 1개) + `ai-brief/data/index.json` (페이지가 읽는 묶음)
- 생성기: `ai-brief/scripts/daily_brief.py` (제공자 자동 선택, 웹검색, 검증, 저장)
- 워크플로: `.github/workflows/ai-brief.yml` (매일 실행 → 커밋 → Pages 배포), `ai-brief-pages.yml` (페이지만 재배포), `ai-brief-ci.yml` (테스트)

```
ai-brief/
├── index.html            모바일용 페이지 (오늘 브리핑 / 오늘의 개념 / 지난 브리핑 / 개념 사전)
├── data/
│   ├── briefs/2026-11-03.json
│   └── index.json
├── scripts/daily_brief.py
├── tests/test_daily_brief.py
├── requirements.txt
├── .env.example          로컬 실행용 키 파일의 예시 (실제 키는 .env 에, .env 는 커밋되지 않음)
└── README.md
```

## 1. 설정 (처음 한 번, 5분)

1. **API 키 등록** — 저장소 *Settings → Secrets and variables → Actions → New repository secret* 에 아래 중 **하나**만 넣습니다.

   | Secret 이름 | 제공자 | 기본 모델 후보 (앞에서부터 시도) |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | Claude | `claude-sonnet-5-5` → `claude-opus-5-5` → `claude-sonnet-4-6` |
   | `OPENAI_API_KEY` | GPT | `gpt-6.1-terra` → `gpt-6.1-sol` → `gpt-5.6-sol` → `gpt-5.5` |
   | `GEMINI_API_KEY` | Gemini | `gemini-3-flash-preview` → `gemini-2.5-flash` → `gemini-3.1-pro-preview` → `gemini-2.5-pro` |

   **키는 코드·워크플로 파일·커밋에 절대 넣지 않습니다.** GitHub Secrets 는 암호화되어 저장되고 실행 로그에서도 마스킹됩니다. 로컬에서 돌릴 때는 `ai-brief/.env`(`.gitignore` 대상)에만 둡니다(아래 4번).

   여러 개를 넣으면 Claude → GPT → Gemini 순으로 첫 번째 키를 씁니다. 특정 제공자를 강제하려면 *Variables* 탭에 `BRIEF_PROVIDER` = `anthropic` / `openai` / `gemini` 를, 모델을 고정하려면 `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL` 변수를 추가합니다. 기본값은 비용을 고려한 절약형(검색 10회, 추론 강도 medium)이며, `BRIEF_MAX_SEARCHES`(4~20), `BRIEF_EFFORT`(low/medium/high) 변수로 조절합니다. 최고 품질을 원하면 `ANTHROPIC_MODEL=claude-opus-5-5`, `BRIEF_MAX_SEARCHES=16`, `BRIEF_EFFORT=high`.

2. **Pages 켜기** — *Settings → Pages → Build and deployment → Source* 를 **GitHub Actions** 로 바꿉니다. (워크플로가 자동으로 켜 보지만, 권한 문제로 실패하면 이 설정이 필요합니다.)

3. **첫 실행** — *Actions → AI Daily Brief → Run workflow*. 2~5분 뒤 페이지가 열립니다. 키 없이 페이지만 먼저 보려면 *AI Daily Brief - deploy page only* 를 실행합니다.

> 예약 실행(`schedule`)은 **기본 브랜치(main)** 에서만 돕니다. GitHub 는 60일간 저장소 활동이 없으면 예약 워크플로를 자동 중지하므로, 그때는 Actions 탭에서 다시 켜 주세요.

## 2. 매일 무엇이 일어나나

1. 06:45 KST 에 워크플로가 깨어나 `daily_brief.py` 를 실행합니다.
2. 스크립트가 제공자의 **내장 웹검색**(Claude: web_search + web_fetch, GPT: Responses API web_search, Gemini: Google Search grounding)으로 최근 24~48시간 소식을 검색합니다(기본 10회).
3. 모델이 선별 기준(아래)에 따라 3~5개와 오늘의 개념을 JSON 으로 씁니다.
4. 스크립트가 **검증**합니다: 카테고리 값, 필수 문장, `source_url` 이 실제 검색 결과(또는 같은 도메인)에 있었는지, 이전 브리핑과 제목 중복, 같은 카테고리 3개 이상 금지. 통과 못 하면 형식만 고치는 2차 호출 → 그래도 안 되면 전체 재시도 1회.
5. `data/briefs/날짜.json` 저장, `index.json` 재생성, main 에 커밋, Pages 배포. Actions 실행 요약에 그날의 제목이 표시됩니다.

### 선별 기준 (프롬프트에 그대로 들어 있음)
- 포함: 주요 모델·제품 출시와 가격 변화, 기업 도입 사례와 성과 수치, 규제·정책 변화, 전략에 영향을 주는 시장·투자 동향, 국내 AX 동향, 기획자가 알아야 할 기술 개념의 전환점.
- 제외: 특정 개발자만 쓰는 세부 기능, 소규모 스타트업 펀딩, 연예성 기사, 단순 벤치마크 갱신, 개인 칼럼.
- 숫자·날짜는 1차 출처 또는 신뢰 매체 2곳 이상 일치 시에만. 예측시장·루머·미확인 모델명 금지.
- 느린 날엔 억지로 채우지 않고 보고서·배경 지식으로 최소 3개.

프롬프트와 기준을 바꾸려면 `daily_brief.py` 의 `SYSTEM_PROMPT` 를 고치면 됩니다.

## 3. 비용 (대략)

하루 1회 실행 기준입니다. 기본값(절약형: 검색 10회, medium)과 최고 품질 설정을 나눠 적었습니다. 토큰 추정치라 실제와 20~30% 차이가 날 수 있습니다.

| 구성 | 하루 | 한 달 |
|---|---|---|
| Gemini Flash (기본) | 약 $0.06 | 약 $2 (무료 한도 안이면 $0) |
| GPT gpt-6.1-terra (기본) | 약 $0.15 | 약 $5 |
| Claude Sonnet 5.5 (기본) | 약 $0.25 | 약 $7.5 |
| Claude Opus 5.5 · 검색 16회 · high | 약 $0.55 | 약 $17 |

정확한 금액은 각 제공자의 사용량 대시보드에서 확인하고, 발급 사이트에서 월 사용 한도를 걸어 두면 안전합니다. GitHub Actions 는 공개 저장소에서 무료입니다(실행당 2~5분).

## 4. 로컬에서 돌려 보기

```bash
pip install -r ai-brief/requirements.txt
cp ai-brief/.env.example ai-brief/.env   # 여기에 키를 적는다. .env 는 .gitignore 에 있어 커밋되지 않는다
python ai-brief/scripts/daily_brief.py                     # 오늘 브리핑 생성
python ai-brief/scripts/daily_brief.py --date 2026-11-04   # 날짜 지정
python ai-brief/scripts/daily_brief.py --mock              # API 없이 파이프라인 점검(예시 데이터)
python ai-brief/scripts/daily_brief.py --rebuild-index     # index.json 만 재생성
python -m http.server -d ai-brief 8080                     # http://localhost:8080/ 에서 페이지 확인
```

스크립트는 `ai-brief/.env` → 저장소 루트 `.env` 순으로 읽고, 이미 설정된 환경 변수는 덮어쓰지 않습니다. `.env` 가 실수로 git 에 추적되면 실행 시 경고를 띄우고, 테스트(`test_env_files_are_gitignored`)도 실패합니다.

테스트: `pytest -q ai-brief/tests` (API 호출 없이 파싱·검증·인덱스·.env 로직만 확인).

## 5. 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 워크플로가 "API 키가 없습니다" 로 실패 | Secrets 이름이 정확한지 (`ANTHROPIC_API_KEY` 등). Variables 가 아니라 **Secrets** 에 넣어야 합니다. |
| "쓸 수 있는 모델이 없습니다" | 제공자가 모델명을 바꾼 경우. `*_MODEL` 변수에 현재 모델 ID 를 넣으세요. |
| 페이지가 404 | Settings → Pages → Source 가 GitHub Actions 인지, 배포 작업이 성공했는지. |
| 페이지가 예전 내용 | 당겨서 새로고침. 데이터는 `data/index.json?v=시각` 으로 캐시를 우회합니다. |
| 매일 안 돌아감 | 예약 실행은 main 에서만. 60일간 활동이 없으면 자동 중지되므로 Actions 탭에서 다시 켜 주세요. |
| 항목이 3개 미만이라 실패 | 드문 경우(검색 결과 빈약). 다음 날 자동으로 다시 시도되며, *Run workflow* 로 바로 재실행해도 됩니다. |
