import asyncio
import os
import re
from datetime import datetime

from . import *
from utils.util import web_search, is_search_available
from modules.db import UserData

# OpenRouter 는 OpenAI 호환 API 이므로 ChatOpenAI 에 base_url 만 바꿔 끼우면 된다.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-3-flash-preview")


class State(MessagesState):
    is_search: str
    is_personal: str
    is_preference: str


class ChatbotAgent:
    def __init__(self):
        self.LIMIT_LENGTH = 12
        self.SEARCH_RESULT_COUNT = 5
        self.system_prompt = prompt_config.system_message
        self.search_keyword = ""
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
        builder.add_node("_node_write_memory", self._node_write_memory)
        builder.add_node("_node_answer", self._node_answer)
        builder.add_node("_node_optimize_memory", self._node_optimize_memory)
        builder.add_edge(START, "_node_initialize")
        builder.add_edge("_node_initialize", "_node_decide_personal")
        builder.add_edge("_node_initialize", "_node_decide_preference")
        builder.add_edge("_node_initialize", "_node_decide_search")
        builder.add_edge(
            ["_node_decide_personal", "_node_decide_preference", "_node_decide_search"],
            "_node_write_memory",
        )
        builder.add_edge("_node_write_memory", "_node_answer")
        builder.add_edge("_node_answer", "_node_optimize_memory")
        builder.add_edge("_node_optimize_memory", END)
        ShortTermMemory = MemorySaver()
        LongTermMemory = InMemoryStore()
        self.graph = builder.compile(checkpointer=ShortTermMemory, store=LongTermMemory)
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
            사용자 요청에 검색 여부를 결정하는 노드
                - 검색 API 키가 없으면 LLM 호출 없이 바로 NO 로 처리한다.
        """
        if not is_search_available():
            return {"is_search": "NO"}
        prompt = [SystemMessage(content=prompt_config.decide_search_prompt)] + [
            HumanMessage(content=self.previous_human_messages_query)
        ]
        return {"is_search": self._parse_yes_no(self.llm.invoke(prompt).content)}

    def _node_write_memory(
        self, state: State, config: RunnableConfig, store: BaseStore
    ):
        """
        Des:
            사용자 메시지를 인식하고, 개인정보/선호도/검색결과 등을 저장하는 노드
        """
        user_id = config["configurable"]["user_id"]
        namespace = ("memories", user_id)
        if state.get("is_personal") == "YES":
            personal_memory = self._get_memory(
                namespace=namespace, key="personal_info", store=store
            )
            system_message = prompt_config.create_memory_prompt.format(
                memory=personal_memory
            )
            memory_prompt = [SystemMessage(content=system_message)] + [
                HumanMessage(content=self.previous_human_messages_query)
            ]
            result = self.llm.invoke(memory_prompt).content
            store.put(
                namespace=namespace, key="personal_info", value={"memory": result}
            )
            self.user_data.update_user_info(user_id, "personal_info", result)
        if state.get("is_preference") == "YES":
            preference_memory = self._get_memory(
                namespace=namespace, key="personal_preference", store=store
            )
            system_message = prompt_config.create_preference_prompt.format(
                preference=preference_memory
            )
            preference_prompt = [SystemMessage(content=system_message)] + [
                HumanMessage(content=self.previous_human_messages_query)
            ]
            result = self.llm.invoke(preference_prompt).content
            store.put(
                namespace=namespace, key="personal_preference", value={"memory": result}
            )
            self.user_data.update_user_info(user_id, "personal_preference", result)

        if state.get("is_search") == "YES":
            main_context, suffix_context = self._web_search()
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
                namespace=namespace,
                key="suffix_context",
                value={"memory": suffix_context},
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

        if state.get("is_search") == "YES":
            main_context = self._get_memory(
                namespace=namespace, key="main_context", store=store
            )
            suffix_context = self._get_memory(
                namespace=namespace, key="suffix_context", store=store
            )
            system_message = prompt_config.answer_prompt.format(
                memory=personal_memory, preference=personal_preference
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
                memory=personal_memory, preference=personal_preference
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

    def _web_search(self):
        """
        Des:
            웹 검색 함수
                - Tavily API 가 검색과 본문 추출을 함께 처리한다.
                - 검색 결과가 없으면 빈 컨텍스트를 반환하며, 호출측에서 검색 없이 답변한다.
        """
        prompt = prompt_config.generate_search_keyword.format(
            query=self.previous_human_messages_query,
            previous_search_keyword=self.search_keyword,
            today=datetime.now().strftime("%Y-%m-%d"),
        )
        self.search_keyword = self.llm.invoke(prompt).content
        results = web_search(
            self.search_keyword, SEARCH_RESULT_COUNT=self.SEARCH_RESULT_COUNT
        )
        print(
            f"{RED}검색어 : {self.search_keyword}\n검색결과 : {len(results)}\n{RESET}"
        )
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
