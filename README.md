
# kakao Chatbot Agent

## About The Project

Langgraph 기반으로 카카오톡 챗봇을 구현해보았습니다. 

- Langgraph 관련하여 먼저 공부하실분은 아래 링크 참고바랍니다.

    - [Langgraph 공식사이트](https://www.langchain.com/langgraph)

    - [Langgraph 공부기록](https://github.com/ccw7463/Langgraph)

#### 📍 기본 기능

- 싱글턴, 멀티턴 수행

- 텍스트 관련 작업 수행

#### 📍 추가 기능

- 웹 검색 기능 수행 (Tavily API)

- 사용자 개인정보, 답변 선호도 저장 및 정보기반 답변 수행

#### 📅 추가 예정 기능

- 사용자 인증

- 챗봇 페르소나 또는 답변 스타일 지정

- STT(Speech-to-Text) 후 답변

- 일일 뉴스 제공 (카카오톡 채널에서 EventAPI 사용)

#### 🔍 디버깅/최적화필요

- `railway.json` (Config as Code) 은 2026-12-01 까지만 지원되므로
  이후 `.railway/railway.ts` (IaC) 로 이전 필요

- 대화 이력이 프로세스 메모리(`MemorySaver`)에 저장되어 서버 재시작 시 초기화됨
  (사용자 개인정보/선호도는 Supabase 에 저장되어 복구됨)

- 대화 이력을 Postgres 체크포인터로 옮기면 재시작에도 멀티턴이 유지됨

## Demo

- 유튜브 링크와 연결됩니다.

[![kakaoChatAgent Test](./public/snapshot.png)](https://youtu.be/VluxN_yeFmA)


## Project Structure

- `configs` : 프롬프트 템플릿 저장 레포

- `modules` : 프로세스 및 데이터베이스 모듈 (사용자 정보는 Supabase 에 저장)

- `utils` : util 함수 모음

## Getting Started

### Requirements

- Python 3.11 이상
- [uv](https://docs.astral.sh/uv/) (의존성 관리)
- OpenRouter API 키 (필수)
- Supabase 프로젝트 (필수 — 사용자 정보 저장)
- Tavily API 키 (선택 — 웹 검색 기능에만 필요)

### Installation

_의존성 관리는 `uv` 를 사용합니다._

1. uv 설치

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

2. 의존성 설치 (`uv.lock` 기준으로 가상환경까지 자동 생성)

    ```bash
    uv sync
    ```

3. **환경변수 설정**

    ```bash
    cp .env.example .env
    ```

    | 변수 | 필수 | 설명 |
    |---|:---:|---|
    | `OPENROUTER_API_KEY` | ✅ | 없으면 서버가 시작되지 않습니다. |
    | `DATABASE_URL` | ✅ | Supabase Postgres 연결 문자열. 없으면 서버가 시작되지 않습니다. |
    | `TAVILY_API_KEY` | — | 없으면 웹 검색만 비활성화되고 나머지는 정상 동작합니다. |
    | `WEBHOOK_URL` | — | 비워두면 루프백을 사용합니다. 보통 손댈 필요 없습니다. |
    | `DB_SCHEMA` | — | 테이블을 격리할 스키마. 기본값 `kakao_agent`. |
    | `LLM_MODEL` | — | OpenRouter 모델 슬러그. 기본값 `google/gemini-3-flash-preview`. |
    | `MAX_AGENTS` | — | 메모리에 유지할 에이전트 수 상한(LRU). 기본값 500. |
    | `PORT` | — | 배포 플랫폼이 자동 주입합니다. 로컬 기본값 7860. |

4. 실행

   ```bash
   uv run python app.py
   ```

### 모델 (OpenRouter)

LLM 호출은 [OpenRouter](https://openrouter.ai) 를 경유합니다. OpenAI 호환 API 라
`LLM_MODEL` 환경변수만 바꾸면 다른 모델로 즉시 교체할 수 있습니다.

기본 모델은 `google/gemini-3-flash-preview` 입니다.

### 데이터베이스 (Supabase)

사용자 개인정보와 답변 선호도는 Supabase(PostgreSQL)에 저장됩니다.
스키마와 테이블은 서버 최초 기동 시 자동 생성되므로 별도 마이그레이션은 필요 없습니다.

기존 Supabase 프로젝트를 재사용해도 됩니다. 테이블은 `public` 이 아니라
전용 스키마(`kakao_agent.users`)에 만들어지므로 두 가지가 보장됩니다.

- 기존 `public.users` 등과 이름이 충돌하지 않습니다.
- Supabase 는 `public` 스키마만 PostgREST 로 공개하므로,
  개인정보 테이블이 anon 키로 외부에서 조회되지 않습니다.

스키마 이름은 `DB_SCHEMA` 환경변수로 바꿀 수 있습니다.

1. [Supabase](https://supabase.com) 에서 프로젝트를 생성합니다.
2. **Project Settings → Database → Connection string → Transaction pooler** 의 URI 를 복사합니다.
   (포트 `6543` 짜리를 쓰세요. 직결 주소는 IPv6 문제가 생길 수 있습니다.)
3. 비밀번호를 채워 `.env` 의 `DATABASE_URL` 에 넣습니다.

> ℹ️ 무료 플랜은 **7일간 쿼리가 없으면 프로젝트가 자동 일시정지**됩니다.
> 앱이 12시간마다 keepalive 쿼리를 보내 이를 방지하므로, 서버가 떠 있는 한 멈추지 않습니다.

## Deployment (Railway)

카카오 서버가 호출할 수 있는 공개 HTTPS 주소가 필요합니다.
`Dockerfile` 과 `railway.json` 이 포함되어 있어 저장소만 연결하면 배포됩니다.

현재 `main` 브랜치는 Railway 에 연결되어 있어 **푸시하면 자동 배포**됩니다.
서비스는 카카오/Supabase(서울) 와의 지연을 줄이기 위해 **Southeast Asia** 리전에 있습니다.

1. [Railway](https://railway.app) 에서 **New Project → Deploy from GitHub repo** 로 이 저장소를 선택합니다.

2. **Variables** 탭에서 환경변수를 등록합니다. (`PORT` 는 Railway 가 자동 주입하므로 넣지 마세요)

    ```
    OPENROUTER_API_KEY=sk-or-v1-...
    DATABASE_URL=postgresql://...
    TAVILY_API_KEY=tvly-...
    ```

3. **Settings → Networking → Generate Domain** 으로 공개 도메인을 발급받습니다.
   (`https://<프로젝트명>.up.railway.app`)

4. 배포 확인

    ```bash
    curl https://<발급받은-도메인>/health
    # {"status":"ok"}
    ```

5. (권장) 리전을 서울과 가까운 곳으로 옮깁니다.

    ```bash
    railway service scale --service <서비스명> southeast-asia=1 us-west=0
    ```

### 카카오톡 채널 연결

1. [챗봇 관리자센터](https://chatbot.kakao.com) 에서 스킬을 만들고, URL 에
   `https://<발급받은-도메인>/question` 을 등록합니다.

2. 해당 블록의 **콜백 사용** 옵션을 켭니다.

> ⚠️ 콜백을 켜지 않으면 `callbackUrl` 이 전달되지 않아 답변이 오지 않습니다.
> 카카오는 스킬 서버가 **5초 안에** 1차 응답을 주지 않으면 연결을 끊고,
> 발급된 콜백 URL 은 **1분간 1회만** 유효합니다.

## Troubleshooting

| 증상 | 원인 / 조치 |
|---|---|
| 시작 시 `OPENROUTER_API_KEY 가 설정되지 않았습니다` | `.env` 파일이 없습니다. `cp .env.example .env` 후 키 입력 |
| 시작 시 `DATABASE_URL 가 설정되지 않았습니다` | Supabase 연결 문자열 미설정 |
| DB 연결 타임아웃 | Supabase 프로젝트가 일시정지됐을 수 있습니다. 대시보드에서 Restore 하세요. |
| 카톡에서 답이 아예 안 옴 | 오픈빌더의 스킬 URL 이 옛날 주소이거나 서버가 내려갔습니다. `/health` 로 확인하세요. |
| `callbackUrl 이 없습니다` 로그 | 오픈빌더에서 해당 블록의 콜백 옵션이 꺼져 있습니다. |
| 검색 기능만 동작하지 않음 | `TAVILY_API_KEY` 미설정. 없으면 검색 없이 답변합니다. |
