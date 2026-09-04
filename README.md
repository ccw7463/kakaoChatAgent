# kakao-agent

LangGraph 로 만든 카카오톡 챗봇입니다. 멀티턴 대화를 이어가면서 필요할 때만 웹을
검색하고, 사용자의 개인정보와 답변 선호도를 기억합니다. LLM 은 OpenRouter 를
경유합니다.

```
                  ┌─▶ decide_personal    ─┐
                  ├─▶ decide_preference  ─┤
initialize ───────┤                       ├──▶ search ──▶ answer ──▶ optimize_memory
                  ├─▶ decide_search      ─┤     Tavily     응답        이력 정리
                  └─▶ recall             ─┘
```

`initialize` 이후의 네 갈래는 **병렬로 실행**됩니다. 개인정보 여부, 선호도 여부,
검색 필요 여부, 과거 대화 회상은 서로 의존하지 않으므로 순차로 돌릴 이유가 없고,
카카오가 요구하는 응답 시간 안에 들어가려면 이 병렬화가 필요합니다.

개인정보·선호도를 실제로 **쓰는** 작업은 그래프 밖으로 뺐습니다. 답변을 사용자에게
보낸 뒤 백그라운드에서 처리합니다. 저장 결과는 다음 턴부터 쓰이므로 답변을 붙잡아
둘 이유가 없고, 그래프 안에 있던 시절에는 매 턴 1~2회의 LLM 왕복이 그대로 대기시간이
됐습니다.

## 동작

### 대화 회상

대화 이력은 최근 12개만 유지되지만, 창 밖으로 밀려난 대화도 임베딩으로 저장해 두었다가
의미가 비슷한 질문이 오면 찾아서 답변에 참고합니다.

| 항목 | 사용 |
| --- | --- |
| 임베딩 | OpenRouter `/embeddings` 의 `google/gemini-embedding-001` (3072차원) |
| 저장 | Supabase `kakao_agent.conversations` (`halfvec` + HNSW 색인) |
| 유사도 하한 | `RECALL_MIN_SIMILARITY`, 기본값 0.66 |

회상 판단은 위 그림처럼 라우팅과 병렬로 실행되므로 응답이 느려지지 않습니다.
pgvector 를 쓸 수 없는 환경에서는 회상만 자동으로 비활성화되고 나머지는 정상
동작합니다.

### 검색

검색 필요 여부, 검색어 생성, 자료 최신성 범위 결정을 **한 번의 LLM 호출로** 처리합니다.
예전에는 판단과 검색어 생성을 따로 불러 왕복이 2회였습니다. `TAVILY_API_KEY` 가 없으면
LLM 을 부르지도 않고 바로 검색 없이 답변합니다.

시세·환율처럼 매일 바뀌는 정보는 최신 자료로 범위를 좁혀 검색합니다. 범위를 주지 않으면
몇 달 전 숫자가 그대로 답변에 실리기 때문입니다.

### 카카오 응답 형식

카카오 `simpleText` 는 말풍선 하나에 1,000자, 응답당 3개까지만 허용합니다. 긴 답변은
자동으로 분할하고, 검색 출처는 길이와 무관하게 항상 별도 말풍선으로 분리합니다.

## 시작하기

```bash
uv sync                       # uv.lock 기준으로 .venv 까지 자동 생성
cp .env.example .env          # OPENROUTER_API_KEY, DATABASE_URL 채우기
uv run python app.py
```

필요한 것은 Python 3.11 이상, [uv](https://docs.astral.sh/uv/), OpenRouter API 키,
Supabase 프로젝트입니다. Tavily 키는 웹 검색을 쓸 때만 필요합니다.

| 변수 | 필수 | 설명 |
| --- | :---: | --- |
| `OPENROUTER_API_KEY` | ✓ | 없으면 서버가 시작되지 않습니다. |
| `DATABASE_URL` | ✓ | Supabase Postgres 연결 문자열. 없으면 서버가 시작되지 않습니다. |
| `TAVILY_API_KEY` | | 없으면 웹 검색만 비활성화됩니다. |
| `LLM_MODEL` | | OpenRouter 모델 슬러그. 기본값 `google/gemini-3-flash-preview`. |
| `DB_SCHEMA` | | 테이블을 격리할 스키마. 기본값 `kakao_agent`. |
| `RECALL_MIN_SIMILARITY` | | 회상 유사도 하한. 기본값 0.66. |
| `MAX_AGENTS` | | 메모리에 유지할 에이전트 수 상한(LRU). 기본값 500. |
| `WEBHOOK_URL` | | 비워두면 루프백을 씁니다. 보통 손댈 필요 없습니다. |
| `PORT` | | 배포 플랫폼이 자동 주입합니다. 로컬 기본값 7860. |

## 모델

모든 LLM 호출은 [OpenRouter](https://openrouter.ai) 를 지나갑니다. OpenAI 호환 API 라
`LLM_MODEL` 하나만 바꾸면 다른 모델로 즉시 교체됩니다.

```bash
LLM_MODEL=google/gemini-3-flash-preview   # 기본값
```

기본값은 *preview* 모델이라 예고 없이 내려갈 수 있습니다.

## 데이터베이스

사용자 개인정보, 답변 선호도, 회상용 대화 임베딩은 Supabase(PostgreSQL)에 저장됩니다.
스키마와 테이블은 서버 최초 기동 시 자동 생성되므로 별도 마이그레이션은 없습니다.

1. [Supabase](https://supabase.com) 에서 프로젝트를 생성합니다.
2. **Project Settings → Database → Connection string → Transaction pooler** 의 URI 를
   복사합니다. 포트 `6543` 짜리를 쓰세요 — 직결 주소는 IPv6 문제가 생길 수 있습니다.
3. 비밀번호를 채워 `.env` 의 `DATABASE_URL` 에 넣습니다.

기존 Supabase 프로젝트를 재사용해도 됩니다. 테이블이 `public` 이 아니라 전용
스키마(`kakao_agent`)에 만들어지기 때문에, 기존 `public.users` 등과 이름이 충돌하지
않고 — 더 중요하게는 — Supabase 가 `public` 스키마만 PostgREST 로 공개하므로
개인정보 테이블이 anon 키로 외부에서 조회되지 않습니다. 스키마 이름은 `DB_SCHEMA` 로
바꿀 수 있습니다.

무료 플랜은 7일간 쿼리가 없으면 프로젝트가 자동 일시정지됩니다. 앱이 12시간마다
keepalive 쿼리를 보내므로 서버가 떠 있는 한 멈추지 않습니다.

## 배포 (Railway)

카카오 서버가 호출할 공개 HTTPS 주소가 필요합니다. `Dockerfile` 과 `railway.json` 이
들어 있어 저장소만 연결하면 배포됩니다. 현재 `main` 브랜치는 Railway 에 연결되어 있어
푸시하면 자동 배포되고, 카카오/Supabase(서울) 와의 지연을 줄이려고 서비스는
**Southeast Asia** 리전에 있습니다.

1. [Railway](https://railway.app) 에서 **New Project → Deploy from GitHub repo** 로
   저장소를 선택합니다.
2. **Variables** 탭에 `OPENROUTER_API_KEY`, `DATABASE_URL`, `TAVILY_API_KEY` 를
   등록합니다. `PORT` 는 Railway 가 자동 주입하므로 넣지 마세요.
3. **Settings → Networking → Generate Domain** 으로 공개 도메인을 발급받습니다.
4. `curl https://<도메인>/health` 가 `{"status":"ok"}` 를 주면 배포된 것입니다.

리전을 서울 가까이 옮기려면:

```bash
railway service scale --service <서비스명> southeast-asia=1 us-west=0
```

### 카카오톡 채널 연결

[챗봇 관리자센터](https://chatbot.kakao.com) 에서 스킬을 만들고 URL 에
`https://<도메인>/question` 을 등록한 뒤, 해당 블록의 **콜백 사용** 옵션을 켭니다.

콜백을 켜지 않으면 `callbackUrl` 이 전달되지 않아 답변이 오지 않습니다. 카카오는 스킬
서버가 5초 안에 1차 응답을 주지 않으면 연결을 끊고, 발급된 콜백 URL 은 1분간 1회만
유효합니다.

## 구조

```
app.py                  FastAPI 서버, 카카오 스킬/콜백 처리, 말풍선 분할
modules/agent.py        LangGraph 그래프와 노드
modules/db.py           Supabase 접근 (사용자 정보, 대화 임베딩)
configs/                프롬프트 템플릿
utils/                  유틸 함수
docs/                   작업 기록
```

## 알려진 문제

- **대화 이력이 프로세스 메모리에 있습니다.** `MemorySaver` 를 쓰기 때문에 서버가
  재시작되면 멀티턴 맥락이 초기화됩니다. 개인정보·선호도·회상용 임베딩은 Supabase 에
  있어 복구되지만, 직전 대화 흐름은 사라집니다. Postgres 체크포인터로 옮기면 해결됩니다.
- **`railway.json` 은 2026-12-01 까지만 지원됩니다.** 이후 `.railway/railway.ts` (IaC)
  로 이전이 필요합니다.

## 예정

사용자 인증, 챗봇 페르소나/답변 스타일 지정, STT 후 답변, 카카오톡 채널 EventAPI 를
이용한 일일 뉴스 발송.

## 문제 해결

| 증상 | 원인 / 조치 |
| --- | --- |
| 시작 시 `OPENROUTER_API_KEY 가 설정되지 않았습니다` | `.env` 가 없습니다. `cp .env.example .env` 후 키 입력 |
| 시작 시 `DATABASE_URL 가 설정되지 않았습니다` | Supabase 연결 문자열 미설정 |
| DB 연결 타임아웃 | Supabase 프로젝트가 일시정지됐을 수 있습니다. 대시보드에서 Restore |
| 카톡에서 답이 아예 안 옴 | 스킬 URL 이 옛 주소이거나 서버가 내려갔습니다. `/health` 로 확인 |
| `callbackUrl 이 없습니다` 로그 | 오픈빌더에서 해당 블록의 콜백 옵션이 꺼져 있습니다 |
| 검색만 동작하지 않음 | `TAVILY_API_KEY` 미설정. 없으면 검색 없이 답변합니다 |

## 참고

LangGraph 를 처음 보신다면 [공식 사이트](https://www.langchain.com/langgraph) 와
[공부 기록](https://github.com/ccw7463/Langgraph) 을 참고하세요.
