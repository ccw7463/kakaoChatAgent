# uv 공식 이미지를 사용해 의존성 설치 (lock 파일 기준으로 재현 가능한 빌드)
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 의존성만 먼저 설치해 레이어 캐시를 활용한다.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Railway 가 PORT 를 주입한다. 로컬에서는 7860 으로 뜬다.
CMD ["uv", "run", "--no-dev", "python", "app.py"]
