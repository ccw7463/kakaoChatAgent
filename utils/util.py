import os
from dotenv import load_dotenv
from tavily import TavilyClient

RESET = "\033[0m"  # Reset to default
RED = "\033[91m"  # Bright Red
BLUE = "\033[94m"  # Bright Blue
GREEN = "\033[92m"  # Bright Green
YELLOW = "\033[93m"  # Bright Yellow
PINK = "\033[95m"  # Bright Pink

# LLM 컨텍스트 초과를 막기 위한 문서당 본문 길이 상한
MAX_CONTENT_LENGTH = 4000

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
