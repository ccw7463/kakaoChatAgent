from collections import OrderedDict
from contextlib import asynccontextmanager

from modules.agent import ChatbotAgent
from modules.db import UserData
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from utils.util import GREEN, RED, YELLOW, RESET, REFERENCE_MARKER
import asyncio
import os
import uvicorn
import httpx
import time

# Railway 등 PaaS 는 PORT 환경변수로 포트를 지정한다.
PORT = int(os.getenv("PORT", "7860"))

# Supabase 무료 플랜의 7일 미사용 일시정지를 막기 위한 주기 (초)
KEEPALIVE_INTERVAL = 60 * 60 * 12


async def _keepalive_loop():
    """
    Des:
        Supabase 프로젝트가 미사용으로 일시정지되지 않도록 주기적으로 쿼리를 던지는 루프
    """
    user_data = UserData()
    while True:
        try:
            await asyncio.to_thread(user_data.keepalive)
            print(f"{YELLOW}[app.py] DB keepalive 완료{RESET}")
        except Exception as e:
            print(f"{RED}[app.py] DB keepalive 실패: {type(e).__name__}: {e}{RESET}")
        await asyncio.sleep(KEEPALIVE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_keepalive_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

# 사용자별 에이전트 캐시.
# 에이전트마다 컴파일된 그래프와 체크포인터를 들고 있어 무한정 쌓이면 메모리가 샌다.
# LRU 로 상한을 두고, 오래 쓰이지 않은 것부터 정리한다.
# 정리되어도 개인정보/선호도는 Supabase 에서 다시 불러오므로 손실은 대화 맥락뿐이다.
MAX_AGENTS = int(os.getenv("MAX_AGENTS", "500"))
user_agents: "OrderedDict[str, ChatbotAgent]" = OrderedDict()


def get_or_create_agent(user_id: str) -> ChatbotAgent:
    """
    Des:
        사용자별 에이전트를 가져오거나 생성하는 함수 (LRU)
    Args:
        user_id: 사용자 ID
    Returns:
        ChatbotAgent: 해당 사용자의 에이전트
    """
    agent = user_agents.get(user_id)
    if agent is None:
        agent = ChatbotAgent()
        print(
            f"{GREEN}[app.py] 새로운 사용자 에이전트를 생성했습니다. 사용자 id : {user_id}{RESET}"
        )
    user_agents[user_id] = agent
    user_agents.move_to_end(user_id)

    while len(user_agents) > MAX_AGENTS:
        evicted, _ = user_agents.popitem(last=False)
        print(
            f"{YELLOW}[app.py] 오래된 에이전트를 정리했습니다. 사용자 id : {evicted}{RESET}"
        )

    return agent


# 서버가 스스로를 호출하는 웹훅 주소.
# 기본값은 루프백이라 외부로 나갔다 오지 않는다. (배포 환경에서도 그대로 두면 된다)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"http://127.0.0.1:{PORT}/webhook")

# 카카오 simpleText 는 말풍선당 1,000자, 응답당 output 최대 3개.
# 넘기면 잘리므로 문단/문장 경계에서 나눠 담는다.
KAKAO_TEXT_LIMIT = 1000
KAKAO_MAX_OUTPUTS = 3


def _split_plain(text: str, max_chunks: int) -> tuple[list[str], str]:
    """
    Des:
        일반 문장을 말풍선 크기로 나누는 함수
            - 문단 -> 문장 -> 공백 순으로 자연스러운 경계를 찾는다.
    Args:
        text: 나눌 문장
        max_chunks: 최대 말풍선 수
    Returns:
        tuple[list[str], str]: (나눈 조각들, 담지 못하고 남은 문자열)
    """
    chunks: list[str] = []
    remaining = text.strip()

    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= KAKAO_TEXT_LIMIT:
            chunks.append(remaining)
            return chunks, ""

        window = remaining[:KAKAO_TEXT_LIMIT]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < KAKAO_TEXT_LIMIT // 2:
            cut = max(window.rfind(". "), window.rfind("다. "), window.rfind("요. "))
            if cut != -1:
                cut += 1
        if cut < KAKAO_TEXT_LIMIT // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = KAKAO_TEXT_LIMIT

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    return chunks, remaining


def _pack_references(refs: str) -> list[str]:
    """
    Des:
        참고내용 목록을 말풍선에 담는 함수
            - 출처 하나가 말풍선 경계에 걸려 제목과 링크가 찢어지면 안 되므로
              블록 단위로만 나눈다.
    Args:
        refs: 참고내용 전문
    Returns:
        list[str]: 말풍선별 참고내용
    """
    blocks = [REFERENCE_MARKER + b for b in refs.split(REFERENCE_MARKER) if b.strip()]
    chunks: list[str] = []
    for block in blocks:
        block = block.strip()
        if chunks and len(chunks[-1]) + 2 + len(block) <= KAKAO_TEXT_LIMIT:
            chunks[-1] += "\n\n" + block
        else:
            chunks.append(block[:KAKAO_TEXT_LIMIT])
    return chunks


def build_kakao_outputs(text: str) -> list[dict]:
    """
    Des:
        답변을 카카오 응답 형식(simpleText 목록)으로 변환하는 함수
            - 말풍선당 1,000자, 최대 3개까지만 허용된다.
            - 참고내용이 있으면 길이와 무관하게 항상 본문과 분리한다.
              길이에 따라 붙었다 떨어졌다 하면 레이아웃이 매번 달라진다.
            - 출처 목록은 블록 단위로만 나눠 여러 말풍선에 흩어지지 않게 한다.
              (예전에는 글자 수만 보고 잘라 참고내용 1,2 는 앞 말풍선,
               3 만 뒤 말풍선으로 떨어졌다)
    Args:
        text: 보낼 답변 전문
    Returns:
        list[dict]: simpleText output 목록 (1~3개)
    """
    text = text.strip()
    if not text:
        return []

    marker_at = text.find(REFERENCE_MARKER)
    if marker_at == -1:
        # 참고내용이 없으면 길 때만 나눈다.
        if len(text) <= KAKAO_TEXT_LIMIT:
            return [{"simpleText": {"text": text}}]
        body, refs = text, ""
    else:
        # 참고내용은 길이와 무관하게 항상 별도 말풍선으로 보낸다.
        # 길이에 따라 붙었다 떨어졌다 하면 사용자가 볼 때마다 레이아웃이 달라진다.
        body, refs = text[:marker_at].rstrip(), text[marker_at:].strip()

    # 참고내용 몫을 먼저 확보한다. 본문이 길다고 출처가 통째로 사라지면 곤란하다.
    ref_chunks = _pack_references(refs)[: KAKAO_MAX_OUTPUTS - 1] if refs else []
    body_chunks, leftover = _split_plain(body, KAKAO_MAX_OUTPUTS - len(ref_chunks))

    if leftover and body_chunks:
        tail = "\n\n(내용이 길어 일부만 표시했어요)"
        last = body_chunks[-1]
        if len(last) + len(tail) > KAKAO_TEXT_LIMIT:
            last = last[: KAKAO_TEXT_LIMIT - len(tail)]
        body_chunks[-1] = last + tail

    return [
        {"simpleText": {"text": c}} for c in (body_chunks + ref_chunks) if c.strip()
    ]


# 답변 생성이 실패했을 때 사용자에게 보낼 메시지
FALLBACK_MESSAGE = (
    "죄송해요, 답변을 만드는 중에 문제가 생겼어요 😢\n잠시 후 다시 말씀해주시겠어요?"
)


async def get_answer(agent: ChatbotAgent, question: str, kakao_callback_url: str):
    """
    Des:
        GPT 응답 생성 및 Webhook 호출
    Args:
        agent: ChatbotAgent 인스턴스
        question: 사용자 질문
        kakao_callback_url: 카카오 콜백 URL
    """
    START_TIME = time.time()
    generated = False
    try:
        response = await _generate_response(agent, question, START_TIME)
        generated = True
    except Exception as e:
        # 여기서 예외가 새어나가면 콜백이 전송되지 않아 사용자가 무한 대기하게 된다.
        print(f"{RED}[app.py] 답변 생성 중 에러 발생: {type(e).__name__}: {e}{RESET}")
        response = FALLBACK_MESSAGE

    await send_to_webhook(
        webhook_url=WEBHOOK_URL,
        response_data={"response": response, "kakao_callback_url": kakao_callback_url},
    )

    # 개인정보/선호도 갱신은 다음 턴부터 쓰이므로 답변을 보낸 뒤에 처리한다.
    if generated:
        try:
            await agent.update_memory()
            print(
                f"{GREEN}[app.py] 응답 후 메모리 갱신 완료 ({time.time() - START_TIME:.1f}초){RESET}"
            )
        except Exception as e:
            print(f"{RED}[app.py] 메모리 갱신 실패: {type(e).__name__}: {e}{RESET}")


async def _generate_response(agent: ChatbotAgent, question: str, START_TIME: float):
    """
    Des:
        질문 유형에 따라 실제 응답 문자열을 만드는 함수
    Args:
        agent: ChatbotAgent 인스턴스
        question: 사용자 질문
        START_TIME: 요청 시작 시각
    Returns:
        str: 사용자에게 보낼 응답
    """
    if "새로운 대화 시작할래요!" in question:
        agent._build_graph()
        response = "안녕하세요🤗 무엇을 도와드릴까요?"
    elif ("사용법" == question) or ("사용법 안내" in question):
        response = """사용법에 대해 간략히 알려드릴게요!

궁금하거나 도움이 필요한 내용을 저한테 말씀주시면 돼요 😊

예를 들어서, '삼성전자에 대해 알려줘'라고 물어보시면 삼성전자에 대한 최신 정보를 기반으로 답변해드릴 수 있어요. 그리고 번역하거나 요약하는 요청도 가능해요!

만약 리스트 메뉴에서 '💬 새로운 대화 시작할래요!'를 선택하면, 이전 대화를 초기화하고 새롭게 시작할 수 있어요. 물론 사용자님의 정보나 답변 선호도와 같은 수집된 정보는 초기화되지 않아요!

그럼 이제 무엇을 도와드릴까요? 🤗"""
    else:
        response = await agent.get_response(question=question)
        END_TIME = time.time()
        print(f"{GREEN}[app.py] Response length : {len(response)}{RESET}")
        print(f"{GREEN}[app.py] Generation Time : {END_TIME - START_TIME}{RESET}")
    return response


async def send_to_webhook(webhook_url: str, response_data: dict):
    """
    Des:
        Webhook 호출 함수
            - AI 답변 생성완료 후 호출
    Args:
        webhook_url: Webhook URL
        response_data: Webhook 호출 시 전달할 데이터
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json=response_data)
    except Exception as e:
        print(f"{RED}Webhook 호출 중 에러 발생: {e}{RESET}")


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Des:
        카카오 서버로 콜백
    Args:
        request: Webhook 호출 시 전달된 데이터
            - response: AI 답변
            - kakao_callback_url: 카카오 콜백 URL
    """
    request_data = await request.json()
    # 동기 requests 를 쓰면 이벤트 루프가 막혀 다른 사용자의 요청이 5초 제한에 걸린다.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            call_back = await client.post(
                request_data["kakao_callback_url"],
                json={
                    "version": "2.0",
                    "template": {
                        "outputs": build_kakao_outputs(request_data["response"])
                    },
                },
            )
        print(f"{GREEN}[app.py] call_back: {call_back.status_code}{RESET}")
    except Exception as e:
        print(f"{RED}[app.py] 카카오 콜백 전송 실패: {type(e).__name__}: {e}{RESET}")
    return "OK"


@app.get("/health")
async def health():
    """
    Des:
        배포 플랫폼의 헬스체크용 엔드포인트
    """
    return {"status": "ok"}


@app.post("/question")
async def handle_question(request: Request, background_tasks: BackgroundTasks):
    """
    Des:
        실제 사용자 요청 처리 함수
    Args:
        request: 사용자 요청
        background_tasks: 백그라운드 작업 태스크
    Returns:
        JSONResponse: 카카오 서버에 응답 반환
            - version: 2.0 필수
            - useCallback: True 필수 -> 콜백함수 사용할것을 의미
    """
    request_data = await request.json()
    user_request = request_data.get("userRequest")
    user_id = user_request.get("user").get("id")
    kakao_callback_url = user_request.get("callbackUrl")

    # 오픈빌더에서 콜백을 켜지 않으면 callbackUrl 이 오지 않는다.
    # 이 경우 콜백을 기다려봐야 응답이 없으므로 즉시 안내한다.
    if not kakao_callback_url:
        print(
            f"{RED}[app.py] callbackUrl 이 없습니다. 오픈빌더 콜백 설정을 확인하세요.{RESET}"
        )
        return JSONResponse(
            {
                "version": "2.0",
                "template": {"outputs": [{"simpleText": {"text": FALLBACK_MESSAGE}}]},
            }
        )

    # 사용자별로 개별적으로 에이전트 할당
    agent = get_or_create_agent(user_id)
    agent.set_config(user_id=user_id)
    background_tasks.add_task(
        get_answer,
        agent=agent,
        question=user_request.get("utterance").strip(),
        kakao_callback_url=kakao_callback_url,
    )

    # print(f"{GREEN}[app.py] useCallback:True 를 먼저 리턴합니다. {RESET}")
    return JSONResponse({"version": "2.0", "useCallback": True})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
