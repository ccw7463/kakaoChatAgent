from . import *


class UserData:
    """
    Des:
        사용자 정보를 Supabase(PostgreSQL)에 저장/조회하는 클래스
            - 커넥션 풀을 클래스 단위로 공유한다. (사용자마다 에이전트가 생성되므로)
            - 다른 프로젝트와 DB 를 공유해도 안전하도록 전용 스키마에 격리한다.
              (public 이 아니면 Supabase 의 PostgREST 가 외부로 노출하지 않는다)
    """

    _pool = None

    def __init__(self):
        schema = os.getenv("DB_SCHEMA", "kakao_agent")
        # 스키마명은 파라미터 바인딩이 불가능하므로 식별자 형식을 직접 검증한다.
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise RuntimeError(
                f"DB_SCHEMA 값이 올바르지 않습니다: {schema!r} "
                "(소문자/숫자/밑줄만 사용 가능)"
            )
        self.schema = schema
        self.table = f"{schema}.users"
        self._initialize_db()

    @classmethod
    def get_pool(cls) -> ConnectionPool:
        """
        Des:
            커넥션 풀을 가져오는 함수 (최초 호출 시 생성)
        Returns:
            ConnectionPool: psycopg 커넥션 풀
        """
        if cls._pool is None:
            dsn = os.getenv("DATABASE_URL")
            if not dsn:
                raise RuntimeError(
                    "DATABASE_URL 이 설정되지 않았습니다. "
                    "Supabase 프로젝트의 Connection string 을 .env 에 입력해주세요."
                )
            cls._pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=5,
                # Supabase 트랜잭션 풀러(6543)는 prepared statement 를 지원하지 않는다.
                kwargs={"prepare_threshold": None},
                open=True,
            )
            print(f"{YELLOW}[db.py] 데이터베이스 커넥션 풀을 생성했습니다.{RESET}")
        return cls._pool

    def process_request(self, user_id: str):
        """
        Des:
            사용자 정보를 찾거나 생성하는 함수
        Args:
            user_id: 사용자 ID
        Returns:
            user_info: 사용자 정보 (id, personal_info, personal_preference) / 신규면 None
        """
        user_info = self._get_or_create_user(user_id)
        return user_info

    def update_user_info(self, user_id: str, field: str, value: str):
        """
        Des:
            사용자 데이터 업데이트 함수
        Args:
            user_id: 사용자 ID
            field: 업데이트할 필드 이름 (personal_info 또는 personal_preference)
            value: 업데이트할 값
        """
        if field not in ["personal_info", "personal_preference"]:
            print(f"{YELLOW}[db.py] 잘못된 필드 이름: {field}{RESET}")
            return

        # field 는 위에서 화이트리스트 검증을 마쳤으므로 문자열 결합이 안전하다.
        with self.get_pool().connection() as conn:
            conn.execute(
                f"UPDATE {self.table} SET {field} = %s WHERE id = %s",
                (value, user_id),
            )
        print(
            f"{YELLOW}[db.py] {field} 정보 업데이트 완료. 사용자 id : {user_id}{RESET}"
        )

    def keepalive(self):
        """
        Des:
            Supabase 무료 플랜은 7일간 쿼리가 없으면 프로젝트를 일시정지시킨다.
            이를 막기 위한 최소 쿼리를 실행한다.
        """
        with self.get_pool().connection() as conn:
            conn.execute("SELECT 1")

    def _initialize_db(self):
        with self.get_pool().connection() as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id TEXT PRIMARY KEY,
                    personal_info TEXT,
                    personal_preference TEXT
                )
                """
            )

    def _get_or_create_user(self, user_id: str):
        """
        Des:
            사용자 정보를 찾거나 생성하는 함수
        Args:
            user_id: 사용자 ID
        Returns:
            user: 사용자 정보 / 신규 사용자면 None
        """
        with self.get_pool().connection() as conn:
            user_info = conn.execute(
                f"SELECT id, personal_info, personal_preference "
                f"FROM {self.table} WHERE id = %s",
                (user_id,),
            ).fetchone()

            if user_info:
                print(
                    f"{YELLOW}[db.py] 기존 사용자 데이터를 찾았습니다: {user_info}{RESET}"
                )
                return user_info

            # 동시에 같은 사용자의 요청이 들어와도 안전하도록 ON CONFLICT 처리
            conn.execute(
                f"INSERT INTO {self.table} (id, personal_info, personal_preference) "
                f"VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (user_id, "", ""),
            )
            print(f"{YELLOW}[db.py] 새 사용자를 추가했습니다: {user_id}{RESET}")
            return None
