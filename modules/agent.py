import asyncio
import os
import re
from datetime import datetime

from . import *
from utils.util import web_search, is_search_available, embed_texts
from modules.db import UserData

# OpenRouter 는 OpenAI 호환 API 이므로 ChatOpenAI 에 base_url 만 바꿔 끼우면 된다.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-3-flash-preview")

# 과거 대화 회상 설정.
# 한국어 임베딩은 무관한 문장끼리도 유사도가 0.6 근처까지 나온다.
# 실측상 관련 대화는 0.76 이상, 무관한 대화는 0.66 이하로 갈려서 그 사이를 임계값으로 둔다.
RECALL_LIMIT = int(os.getenv("RECALL_LIMIT", "3"))
RECALL_MIN_SIMILARITY = float(os.getenv("RECALL_MIN_SIMILARITY", "0.66"))
# "고마워", "잘가" 같은 인사말은 회상해봐야 쓸모가 없는데,
# 같은 인사말끼리는 유사도가 0.8 을 넘어 정작 유용한 회상(0.7 근처)을 밀어낸다.
# 이런 짧은 발화는 아예 저장하지 않는다.
RECALL_MIN_QUESTION_CHARS = int(os.getenv("RECALL_MIN_QUESTION_CHARS", "6"))


class State(MessagesState):
    is_search: str
    is_personal: str
    is_preference: str
    search_keyword: str
    recalled: str


class ChatbotAgent:
    def __init__(self):
        self.LIMIT_LENGTH = 12
        self.SEARCH_RESULT_COUNT = 3
        self.system_prompt = prompt_config.system_message
        self.llm = ChatOpenAI(
            model=LLM_MODEL,
            base_url=OPENROUTER_BASE_URL,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            # OpenRouter 대시보드에서 어떤 앱이 호출했는지 구분하기 위한 선택 헤더
            default_headers={"X-Title": "kakao-chat-agent"},
        )
        self.config = {"configurable": {"thread_id": "default", "user_id": "default"}}
        self.user_data = UserData()
        self._build_graph()

    async def get_response(self, question: str) -> str:
        """
        Des:
            사용자 요청에 대한 답변을 생성하는 함수
        Args:
            question: 사용자 요청
        Returns:
            답변
        """
        question = HumanMessage(content=question)
        # 그래프는 동기 실행이므로 별도 스레드로 넘겨 이벤트 루프를 막지 않는다.
        # (블로킹 시 다른 사용자의 요청이 카카오 5초 제한을 넘겨 끊긴다)
        result = await asyncio.to_thread(self._call_graph, [question])
        return result["messages"][-1].content

    async def update_memory(self):
        """
        Des:
            개인정보/선호도를 갱신하는 함수
                - 답변을 사용자에게 보낸 뒤에 호출한다.
                  이 결과는 다음 턴부터 쓰이므로 답변을 붙잡아 둘 이유가 없다.
                  (그래프 안에 있을 때는 매 턴 1~2회의 LLM 왕복이 답변을 지연시켰다)
        """
        await asyncio.to_thread(self._write_memory)

    def _write_memory(self):
        """
        Des:
            직전 턴의 라우팅 결과를 보고 개인정보/선호도를 저장하고,
            대화 내용을 회상용으로 적재하는 함수
        """
        state = self.graph.get_state(self.config).values
        user_id = self.config["configurable"]["user_id"]
        namespace = ("memories", user_id)

        self._save_conversation(state, user_id)

        targets = []
        if state.get("is_personal") == "YES":
            targets.append(
                ("personal_info", prompt_config.create_memory_prompt, "memory")
            )
        if state.get("is_preference") == "YES":
            targets.append(
                (
                    "personal_preference",
                    prompt_config.create_preference_prompt,
                    "preference",
                )
            )

        for key, template, field in targets:
            existing = self._get_memory(namespace=namespace, key=key, store=self.store)
            system_message = template.format(**{field: existing})
            prompt = [SystemMessage(content=system_message)] + [
                HumanMessage(content=self.previous_human_messages_query)
            ]
            result = self.llm.invoke(prompt).content
            self.store.put(namespace=namespace, key=key, value={"memory": result})
            self.user_data.update_user_info(user_id, key, result)

    def set_config(self, user_id: str):
        """
        Des:
            config 설정 함수
        Args:
            user_id: 사용자 ID
        """
        self.config = {
            "configurable": {
                "thread_id": user_id,  # 어차피 카톡은 채팅창 여러개를 띄울수없기에, thread 값도 user_id로 고정
                "user_id": user_id,
            }
        }

    def _build_graph(self):
        """
        Des:
            그래프 생성함수
        """
        builder = StateGraph(State)
        builder.add_node("_node_initialize", self._node_initialize)
        builder.add_node("_node_decide_personal", self._node_decide_personal)
        builder.add_node("_node_decide_preference", self._node_decide_preference)
        builder.add_node("_node_decide_search", self._node_decide_search)
        builder.add_node("_node_recall", self._node_recall)
        builder.add_node("_node_search", self._node_search)
        builder.add_node("_node_answer", self._node_answer)
        builder.add_node("_node_optimize_memory", self._node_optimize_memory)
        builder.add_edge(START, "_node_initialize")
        builder.add_edge("_node_initialize", "_node_decide_personal")
        builder.add_edge("_node_initialize", "_node_decide_preference")
        builder.add_edge("_node_initialize", "_node_decide_search")
        builder.add_edge("_node_initialize", "_node_recall")
        builder.add_edge(
            [
                "_node_decide_personal",
                "_node_decide_preference",
                "_node_decide_search",
                "_node_recall",
            ],
            "_node_search",
        )
        builder.add_edge("_node_search", "_node_answer")
        builder.add_edge("_node_answer", "_node_optimize_memory")
        builder.add_edge("_node_optimize_memory", END)
        ShortTermMemory = MemorySaver()
        # 메모리 저장을 답변 이후로 미루므로 스토어를 그래프 밖에서도 쓴다.
        self.store = InMemoryStore()
        self.graph = builder.compile(checkpointer=ShortTermMemory, store=self.store)
        print(f"{GREEN}[agent.py] 그래프 빌드 완료{RESET}")

    def _node_initialize(self, state: State, config: RunnableConfig, store: BaseStore):
        """
        Des:
            초기화 함수
                - 메모리 초기화
                    - 케이스 1) 사용자가 채팅 처음 시작 -> set_config -> DB에 정보없으니까 else로 가서 종료
                    - 케이스 2) 사용자가 채팅을 '새로운 대화'로 시작함 -> 그래프 새로 빌드 -> 롱텀 초기화 -> set_config -> 사용자 정보가 있으니까 데이터 삽입
                    - 케이스 3) 사용자가 채팅을 했었는데 내가 서버 다시킴 -> 그래프 새로 빌드 -> 롱텀 초기화 -> set_config -> 사용자 정보가 있으니까 데이터 삽입
                - 사용자 정보 초기화
                - 사용자 요청메시지 취합
        """
        user_id = config["configurable"]["user_id"]
        namespace = ("memories", user_id)
        user_info = self.user_data.process_request(user_id)
        if user_info:
            print(
                f"{YELLOW}[agent.py] 데이터베이스에 이전 사용자 정보가 있습니다. 그래프내에 데이터를 삽입합니다.{RESET}"
            )
            store.put(
                namespace=namespace, key="personal_info", value={"memory": user_info[1]}
            )
            store.put(
                namespace=namespace,
                key="personal_preference",
                value={"memory": user_info[2]},
            )
        else:
            print(
                f"{YELLOW}[agent.py] 데이터베이스에 이전 사용자 정보가 없습니다.{RESET}"
            )

        # 사용자 요청메시지만 취합해서 정리 (라우팅 등에서 사용)
        self.previous_human_messages = [
            i.content for i in state["messages"] if isinstance(i, HumanMessage)
        ]
        self.previous_human_messages_query = ""
        for idx, message in enumerate(self.previous_human_messages, start=1):
            if idx != len(self.previous_human_messages):
                self.previous_human_messages_query += (
                    f"{idx}번째 요청 메시지 : {message}\n"
                )
            else:
                self.previous_human_messages_query += (
                    f"[현재 요청 메시지] : {message}\n"
                )
        print(
            f"{RED}요청 메시지 취합한거 메시지 : {self.previous_human_messages_query}{RESET}"
        )

    def _node_decide_personal(self, state: State):
        """
        Des:
            사용자 요청에 개인정보 여부가 있는지 판단하는 노드
        """
        prompt = [SystemMessage(content=prompt_config.decide_personal_prompt)] + [
            HumanMessage(content=self.previous_human_messages_query)
        ]
        return {"is_personal": self._parse_yes_no(self.llm.invoke(prompt).content)}

    def _node_decide_preference(self, state: State):
        """
        Des:
            사용자 요청에 답변 선호도 여부가 있는지 판단하는 노드
        """
        prompt = [SystemMessage(content=prompt_config.decide_preference_prompt)] + [
            HumanMessage(content=self.previous_human_messages_query)
        ]
        return {"is_preference": self._parse_yes_no(self.llm.invoke(prompt).content)}

    def _node_decide_search(self, state: State):
        """
        Des:
            검색 필요 여부와 검색어를 한 번에 결정하는 노드
                - 예전에는 판단(YES/NO)과 검색어 생성을 각각 호출해 LLM 왕복이 2회였다.
                  하나로 합쳐 왕복을 1회로 줄인다.
                - 검색 API 키가 없으면 LLM 호출 없이 바로 NO 로 처리한다.
        """
        if not is_search_available():
            return {"is_search": "NO", "search_keyword": ""}

        system_message = prompt_config.decide_search_prompt.format(
            today=datetime.now().strftime("%Y-%m-%d"),
            previous_search_keyword=state.get("search_keyword") or "(없음)",
        )
        prompt = [SystemMessage(content=system_message)] + [
            HumanMessage(content=self.previous_human_messages_query)
        ]
        content = (self.llm.invoke(prompt).content or "").strip()

        # 알파벳만 남겼을 때 NO 면 검색 불필요, 그 외에는 내용을 검색어로 본다.
        if not content or re.sub(r"[^A-Z]", "", content.upper()) == "NO":
            return {"is_search": "NO", "search_keyword": ""}
        return {"is_search": "YES", "search_keyword": content.splitlines()[0].strip()}

    def _node_recall(self, state: State, config: RunnableConfig):
        """
        Des:
            현재 질문과 의미가 비슷한 과거 대화를 찾아오는 노드
                - 라우팅 노드들과 병렬로 실행되어 추가 지연이 사실상 없다.
                - 대화창에 아직 남아있는 내용은 중복이므로 제외한다.
                - 임베딩이나 조회가 실패해도 회상만 비우고 답변은 계속 진행한다.
        """
        user_id = config["configurable"]["user_id"]
        question = state["messages"][-1].content

        vectors = embed_texts([question])
        if not vectors:
            return {"recalled": ""}

        try:
            hits = self.user_data.search_conversations(
                user_id,
                vectors[0],
                limit=RECALL_LIMIT,
                min_similarity=RECALL_MIN_SIMILARITY,
            )
        except Exception as e:
            print(f"{RED}[agent.py] 대화 회상 실패: {type(e).__name__}: {e}{RESET}")
            return {"recalled": ""}

        in_window = {
            m.content for m in state["messages"] if isinstance(m, HumanMessage)
        }
        lines = [f"- 질문: {q}\n  답변: {a}" for q, a, _ in hits if q not in in_window]
        if lines:
            print(f"{YELLOW}[agent.py] 과거 대화 {len(lines)}건을 회상했습니다.{RESET}")
        return {"recalled": "\n".join(lines)}

    def _node_search(self, state: State, config: RunnableConfig, store: BaseStore):
        """
        Des:
            검색이 필요한 경우 웹 검색 결과를 스토어에 적재하는 노드
                - 개인정보/선호도 저장은 답변 지연을 줄이기 위해 그래프 밖으로 뺐다.
                  (update_memory 참고)
        """
        if state.get("is_search") != "YES":
            return

        user_id = config["configurable"]["user_id"]
        namespace = ("memories", user_id)
        main_context, suffix_context = self._web_search(state.get("search_keyword", ""))
        if not main_context:
            # 검색 결과가 없으면 검색 없이 답변하도록 되돌린다.
            print(
                f"{YELLOW}[agent.py] 검색 결과가 없어 일반 답변으로 전환합니다.{RESET}"
            )
            return {"is_search": "NO"}
        store.put(
            namespace=namespace, key="main_context", value={"memory": main_context}
        )
        store.put(
            namespace=namespace, key="suffix_context", value={"memory": suffix_context}
        )

    def _node_answer(self, state: State, config: RunnableConfig, store: BaseStore):
        """
        Des:
            사용자 메시지를 인식하고, 답변을 생성하는 노드
        """
        user_id = config["configurable"]["user_id"]
        namespace = ("memories", user_id)
        personal_memory = self._get_memory(
            namespace=namespace, key="personal_info", store=store
        )
        personal_preference = self._get_memory(
            namespace=namespace, key="personal_preference", store=store
        )

        recalled = state.get("recalled") or ""
        recalled_block = (
            prompt_config.recalled_prompt.format(recalled=recalled) if recalled else ""
        )

        if state.get("is_search") == "YES":
            main_context = self._get_memory(
                namespace=namespace, key="main_context", store=store
            )
            suffix_context = self._get_memory(
                namespace=namespace, key="suffix_context", store=store
            )
            system_message = prompt_config.answer_prompt.format(
                memory=personal_memory,
                preference=personal_preference,
                recalled=recalled_block,
            )
            user_prompt = prompt_config.answer_with_context.format(
                context=main_context, query=state["messages"][-1].content
            )  # TODO 향후 고려필요
            prompt = (
                [SystemMessage(content=self.system_prompt + system_message)]
                + state["messages"][:-1]
                + [HumanMessage(content=user_prompt)]
            )  # TODO 향후 고려필요
            print(f"{BLUE}Answer with Search prompt : {prompt[0].content}{RESET}")
            response = self.llm.invoke(prompt).content
            return {
                "messages": AIMessage(
                    content=self._postprocess(response) + "\n" + suffix_context
                )
            }
        else:
            system_message = prompt_config.answer_prompt.format(
                memory=personal_memory,
                preference=personal_preference,
                recalled=recalled_block,
            )
            prompt = [
                SystemMessage(content=self.system_prompt + system_message)
            ] + state["messages"]
            print(f"{BLUE}Answer prompt : {prompt[0].content}{RESET}")
            response = self.llm.invoke(prompt).content
            return {"messages": AIMessage(content=self._postprocess(response))}

    def _node_optimize_memory(self, state: State):
        """
        Des:
            메모리 최적화 함수
        """
        if len(state["messages"]) > self.LIMIT_LENGTH:
            delete_messages = [
                RemoveMessage(id=m.id)
                for m in state["messages"][: self.LIMIT_LENGTH // 2]
            ]
            return {"messages": delete_messages}
        else:
            return {"messages": state["messages"]}

    def _web_search(self, search_keyword: str):
        """
        Des:
            웹 검색 함수
                - 검색어는 라우팅 노드에서 이미 만들어 넘겨받는다. (LLM 왕복 절약)
                - 검색 결과가 없으면 빈 컨텍스트를 반환하며, 호출측에서 검색 없이 답변한다.
        Args:
            search_keyword: 검색할 키워드
        """
        results = web_search(
            search_keyword, SEARCH_RESULT_COUNT=self.SEARCH_RESULT_COUNT
        )
        print(f"{RED}검색어 : {search_keyword}\n검색결과 : {len(results)}\n{RESET}")
        main_context = ""
        suffix_context = ""
        for idx, result in enumerate(results, start=1):
            title = result["title"]
            link = result["link"]
            main_context += (
                f"제목 : {title}\n링크 : {link}\n내용 : {result['content']}\n\n"
            )
            suffix_context += f"""
📌 참고내용 [{idx}]
제목 : {title}
링크 : {link}
"""
        return main_context, suffix_context

    def _save_conversation(self, state: dict, user_id: str):
        """
        Des:
            직전 턴의 질문/답변을 임베딩과 함께 저장하는 함수
                - 나중에 대화창에서 밀려나도 회상으로 다시 찾아올 수 있게 한다.
                - 실패해도 답변은 이미 전송된 뒤이므로 로그만 남기고 넘어간다.
        """
        messages = state.get("messages") or []
        question = next(
            (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), None
        )
        answer = next(
            (m.content for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if not question or not answer:
            return

        # 인사말 등 짧은 발화는 회상 대상에서 제외한다.
        if len(question.strip()) < RECALL_MIN_QUESTION_CHARS:
            return

        try:
            vectors = embed_texts([f"{question}\n{answer}"])
            if not vectors:
                return
            self.user_data.save_conversation(user_id, question, answer, vectors[0])
        except Exception as e:
            print(f"{RED}[agent.py] 대화 저장 실패: {type(e).__name__}: {e}{RESET}")

    @staticmethod
    def _parse_yes_no(content: str) -> str:
        """
        Des:
            라우팅 노드의 응답을 YES / NO 로 정규화하는 함수
                - 모델에 따라 'YES.', '**YES**', 'Yes, 필요합니다' 처럼 답하므로
                  알파벳만 남긴 뒤 YES 로 시작하는지만 본다.
        Args:
            content: LLM 원본 응답
        Returns:
            str: "YES" 또는 "NO"
        """
        letters = re.sub(r"[^A-Z]", "", content.upper())
        return "YES" if letters.startswith("YES") else "NO"

    def _get_memory(self, namespace, key, store: BaseStore):
        """
        Des:
            현재 저장된 사용자 정보를 가져오는 함수
        """
        existing_memory = store.get(namespace=namespace, key=key)
        return existing_memory.value.get("memory") if existing_memory else ""

    def _call_graph(self, messages):
        """
        Des:
            그래프 호출 함수
        """
        return self.graph.invoke({"messages": messages}, config=self.config)

    def _postprocess(self, result: str):
        """
        Des:
            답변 후처리 함수
        """
        result = result.replace("**", "").replace("*", "").replace("_", "")
        return result
