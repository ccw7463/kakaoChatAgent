import os

import httpx
from dotenv import load_dotenv
from tavily import TavilyClient

RESET = "\033[0m"  # Reset to default
RED = "\033[91m"  # Bright Red
BLUE = "\033[94m"  # Bright Blue
GREEN = "\033[92m"  # Bright Green
YELLOW = "\033[93m"  # Bright Yellow
PINK = "\033[95m"  # Bright Pink

# 문서당 본문 길이 상한.
# 답변은 카카오 말풍선 크기에 맞춰 짧게 나가므로 컨텍스트를 크게 넣을 이유가 없다.
# 크게 넣으면 최종 답변 생성만 느려진다. (16K -> 4.5K 로 줄여 측정)
MAX_CONTENT_LENGTH = 1500

# 임베딩 설정 (OpenRouter 의 OpenAI 호환 /embeddings 사용)
EMBED_MODEL = os.getenv("EMBED_MODEL", "google/gemini-embedding-001")
EMBED_DIM = 3072
# 임베딩 입력 토큰 상한을 넘지 않도록 보수적으로 자른다.
EMBED_INPUT_LIMIT = 2000

_tavily_client = None


def set_env():
    """
    Des:
        .env 를 읽어 환경변수를 세팅하는 함수
            - OPENROUTER_API_KEY, DATABASE_URL 은 필수이며 없으면 즉시 종료한다.
            - TAVILY_API_KEY 는 선택이며, 없으면 웹 검색 기능만 비활성화된다.
    """
    load_dotenv()

    for key in ("OPENROUTER_API_KEY", "DATABASE_URL"):
        if not os.getenv(key):
            raise RuntimeError(
                f"{key} 가 설정되지 않았습니다. "
                ".env.example 을 .env 로 복사한 뒤 값을 입력해주세요."
            )

    if not os.getenv("TAVILY_API_KEY"):
        print(
            f"{YELLOW}[util.py] TAVILY_API_KEY 가 없어 웹 검색 기능이 비활성화됩니다.{RESET}"
        )


def is_search_available() -> bool:
    """
    Des:
        웹 검색 기능 사용 가능 여부
    Returns:
        bool: TAVILY_API_KEY 설정 여부
    """
    return bool(os.getenv("TAVILY_API_KEY"))


def web_search(search_term: str, SEARCH_RESULT_COUNT: int = 5) -> list[dict]:
    """
    Des:
        Tavily API 기반 웹 검색 함수
            - 검색과 본문 추출이 한 번에 끝나므로 별도의 스크래핑이 필요없다.
            - 실패 시 예외를 던지지 않고 빈 리스트를 반환한다. (호출측에서 검색 없이 답변)
    Args:
        search_term (str): 검색할 키워드
        SEARCH_RESULT_COUNT (int): 검색 결과 수
    Returns:
        list[dict]: {"title", "link", "content"} 형태의 검색 결과 리스트
    """
    if not is_search_available():
        return []

    global _tavily_client
    if _tavily_client is None:
        _tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

    try:
        response = _tavily_client.search(
            query=search_term,
            max_results=SEARCH_RESULT_COUNT,
            search_depth="advanced",
            include_raw_content="text",
            country="south korea",
            timeout=30,
        )
    except Exception as e:
        print(f"{RED}[util.py] 검색 실패: {type(e).__name__}: {e}{RESET}")
        return []

    results = []
    for item in response.get("results", []):
        # raw_content 가 없는 페이지(동적 렌더링 등)는 요약 스니펫으로 대체
        content = item.get("raw_content") or item.get("content") or ""
        results.append(
            {
                "title": item.get("title", ""),
                "link": item.get("url", ""),
                "content": content[:MAX_CONTENT_LENGTH],
            }
        )
    return results


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """
    Des:
        문장들을 임베딩 벡터로 변환하는 함수
            - langchain 의 OpenAIEmbeddings 대신 httpx 로 직접 호출한다.
              (tiktoken 기반 사전 절단이 한국어에서 엉뚱한 자리를 끊고,
               응답의 usage/비용 정보도 버려지기 때문)
            - 실패 시 예외를 던지지 않고 None 을 반환한다. (호출측에서 회상 없이 진행)
    Args:
        texts: 임베딩할 문장 목록
    Returns:
        list[list[float]] | None: 입력 순서와 같은 벡터 목록. 실패 시 None
    """
    if not texts:
        return []

    payload = [t[:EMBED_INPUT_LIMIT] for t in texts]
    try:
        res = httpx.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"},
            json={"model": EMBED_MODEL, "input": payload},
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()["data"]
    except Exception as e:
        print(f"{RED}[util.py] 임베딩 실패: {type(e).__name__}: {e}{RESET}")
        return None

    # API 가 순서를 보장하지 않을 수 있으므로 index 로 정렬한다.
    return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
