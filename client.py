"""Клиент внутреннего GraphQL API Playerok.

Порт page-api.js расширения Playerok Tools. Работает от cookies сессии
пользователя (как это делает расширение через браузер).
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Optional

import httpx

import config

BASE_URL = "https://playerok.com"
GRAPHQL_PATH = "/graphql"

# Browser-like заголовки + те, что шлёт расширение (page-api.js `headers()`).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_SIZE = 24
BIG_PAGE_SIZE = 50

# Хэши persisted-запросов сайта (первичный способ, как в page-api.js).
HASHES = {
    "userChats": [
        "c1ddbcd7c8b87160ac25e0734f9dc32fc945287b056f4b14abf1473bfb1ad11a",
        "999f86b7c94a4cb525ed5549d8f24d0d24036214f02a213e8fd7cefc742bbd58",
    ],
    "deals": [
        "591b0e6d036c2120c8f95b97dbfdf5635df3747cd901f4895e009935229417ef",
        "c3b623b5fe0758cf91b2335ebf36ff65f8650a6672a792a3ca7a36d270d396fb",
    ],
    "items": [
        "3f20c731f8f769a094ee3fa32e09f8e12250357e9a4f0ebb4e6988e7a0bb9260",
        "bacca5d020eef37b4ff7a2253ad33ecd8b7e144b9ef854c20051f42ebcd04d82",
        "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557",
    ],
}

# Хэши persisted-запросов сайта (нужны только для itemPriorityStatuses).
HASH_ITEM_PRIORITY_STATUSES = "b922220c6f979537e1b99de6af8f5c13727daeff66727f679f07f986ce1c025a"

ALL_ITEM_STATUSES = [
    "PENDING_APPROVAL", "PENDING_MODERATION", "APPROVED", "DECLINED",
    "BLOCKED", "EXPIRED", "SOLD", "DRAFT",
]


class PlayerokError(Exception):
    """Ошибка запроса к Playerok (HTTP/GraphQL/неверная сессия)."""


class AuthError(PlayerokError):
    """Сессия недействительна или cookies устарели."""


def _clean_error(err: Exception) -> str:
    msg = str(err)
    return " ".join(msg.split())[:500]


class PlayerokClient:
    """Асинхронный клиент GraphQL Playerok для одного аккаунта."""

    def __init__(self, cookie_header: str):
        # cookie_header — строка вида "name=value; name2=value2".
        # Playerok авторизует по куке `token` (JWT-сессия), как и браузер.
        self.cookie_header = cookie_header or ""
        proxy = getattr(config, "PLAYEROK_PROXY", "") or ""
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "user-agent": USER_AGENT,
                "accept": "application/json, text/plain, */*",
                "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "cookie": self.cookie_header,
            },
            proxy=proxy or None,
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- низкоуровневые запросы ----------

    def _headers(self, operation_name: str, json_body: bool = False) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "apollo-require-preflight": "true",
            "apollographql-client-name": "web",
            "x-apollo-operation-name": operation_name,
            "x-gql-op": operation_name,
            "x-gql-path": "/",
            "x-timezone-offset": "-180",
        }
        if json_body:
            h["content-type"] = "application/json"
        return h

    async def _fetch_json(self, url: str, options: dict, operation_name: str) -> dict:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = await self._client.request(
                    method=options.get("method", "GET"),
                    url=url,
                    headers=options.get("headers", {}),
                    content=options.get("content"),
                )
                text = resp.text
                try:
                    body = json.loads(text)
                except Exception:
                    body = None

                if resp.status_code in (429,) or resp.status_code >= 500:
                    if attempt < 1:
                        await asyncio.sleep(min(10, 0.65 * (attempt + 1)))
                        continue
                    raise PlayerokError(f"{operation_name}: HTTP {resp.status_code}")

                if resp.status_code in (401, 403):
                    # 401/403 = либо протухшая сессия, либо блокировка DDoS-Guard.
                    raw_text = (text or "").strip()
                    if "ddos-guard" in raw_text.lower() or not raw_text.lstrip().startswith(("{", "[")):
                        raise AuthError(
                            "Playerok не пустил запрос (возможно, IP-адрес сервера заблокирован "
                            "защитой DDoS-Guard). Попробуйте запустить бота с прокси."
                        )
                    detail = ""
                    if body and isinstance(body.get("errors"), list):
                        detail = "; ".join(
                            e.get("message", "") for e in body["errors"] if e.get("message")
                        )
                    raise AuthError(f"Сессия недействительна (HTTP {resp.status_code}). {detail}".strip())

                if resp.status_code >= 400:
                    detail = ""
                    if body and isinstance(body.get("errors"), list):
                        detail = "; ".join(
                            e.get("message", "") for e in body["errors"] if e.get("message")
                        )
                    raise PlayerokError(
                        f"{operation_name}: HTTP {resp.status_code}"
                        + (f" — {detail}" if detail else "")
                    )

                if body is None:
                    raise PlayerokError(f"{operation_name}: сайт вернул не JSON (возможно DDoS-Guard).")

                if isinstance(body.get("errors"), list) and body["errors"]:
                    detail = "; ".join(
                        e.get("message", "") for e in body["errors"] if e.get("message")
                    )
                    raise PlayerokError(f"{operation_name}: {detail or 'ошибка GraphQL'}")

                return body
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < 1:
                    await asyncio.sleep(0.45 * (attempt + 1))
                    continue
                raise PlayerokError(
                    f"{operation_name}: сеть — {_clean_error(exc)}. "
                    f"Возможно, IP-адрес сервера заблокирован защитой Playerok (DDoS-Guard) — "
                    f"запустите бота через прокси."
                )
            except (PlayerokError, AuthError):
                raise
        raise PlayerokError(f"{operation_name}: {_clean_error(last_error or Exception('запрос не выполнен'))}")

    async def _graphql(self, operation_name: str, query: str, variables: dict = None) -> dict:
        return await self._fetch_json(
            GRAPHQL_PATH,
            {
                "method": "POST",
                "headers": self._headers(operation_name, json_body=True),
                "content": json.dumps(
                    {"operationName": operation_name, "query": query, "variables": variables or {}}
                ).encode(),
            },
            operation_name,
        )

    async def _persisted(self, operation_name: str, variables: dict, sha_hash: str) -> dict:
        params = {
            "operationName": operation_name,
            "variables": json.dumps(variables),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha_hash}}),
        }
        url = httpx.URL(GRAPHQL_PATH).copy_merge_params(params)
        return await self._fetch_json(
            str(url),
            {"method": "GET", "headers": self._headers(operation_name, json_body=False)},
            operation_name,
        )

    # ---------- нормализация ----------

    @staticmethod
    def _connection_from(response: dict, root_name: str) -> dict:
        data = (response or {}).get("data") or {}
        conn = data.get(root_name)
        if not conn or not isinstance(conn.get("edges"), list) or not conn.get("pageInfo"):
            raise PlayerokError(f"{root_name}: Playerok вернул данные в неизвестном формате.")
        nodes = [e.get("node") for e in conn["edges"] if e and e.get("node")]
        page_info = conn["pageInfo"]
        return {
            "nodes": nodes,
            "has_next_page": bool(page_info.get("hasNextPage")),
            "end_cursor": page_info.get("endCursor"),
            "total_count": int(conn.get("totalCount") or 0),
        }

    # ---------- операции API ----------

    async def get_viewer(self) -> dict:
        resp = await self._graphql(
            "viewer",
            """
            query viewer {
              viewer {
                id username role unreadChatsCounter isBlocked canPublishItems
                balance { value __typename }
                profile { avatarURL testimonialCounter __typename }
                __typename
              }
            }
            """,
        )
        viewer = (resp.get("data") or {}).get("viewer")
        if not viewer or not viewer.get("id"):
            raise AuthError("Токен недействителен или истёк. Отправьте актуальный токен через /login.")
        return {
            "id": str(viewer["id"]),
            "username": viewer.get("username") or "",
            "role": viewer.get("role") or "",
            "unread_chats_counter": int(viewer.get("unreadChatsCounter") or 0),
            "is_blocked": bool(viewer.get("isBlocked")),
            "can_publish_items": viewer.get("canPublishItems") is not False,
            "balance": float((viewer.get("balance") or {}).get("value") or 0),
            "avatar_url": ((viewer.get("profile") or {}).get("avatarURL") or ""),
            "testimonial_counter": int(((viewer.get("profile") or {}).get("testimonialCounter")) or 0),
        }

    async def _paginated(
        self,
        operation_name: str,
        root_name: str,
        make_variables,
        full_query,
        hashes=None,
        max_pages: int = 40,
        prefer_full: bool = False,
    ) -> dict:
        """Пагинация как в page-api.js: persisted-запросы первыми, при ошибке
        (HTTP/GQL) — откат размера страницы 50→24, полный GraphQL как запасной."""
        hashes = hashes or []
        nodes: list = []
        seen: set = set()
        after = None
        page_size = BIG_PAGE_SIZE
        selected_hash = None
        use_full = prefer_full
        full_broken = False
        total_count = 0
        truncated = True
        page_number = 0

        def _downgradable(err: Exception) -> bool:
            # AuthError не лечится уменьшением страницы — пробрасываем сразу.
            return isinstance(err, PlayerokError) and not isinstance(err, AuthError)

        async def fetch_page():
            nonlocal use_full, full_broken, selected_hash, page_size
            if use_full:
                if full_broken:
                    use_full = False
                    full_broken = False
                else:
                    try:
                        return await full_query(after, page_size)
                    except PlayerokError as e:
                        if _downgradable(e) and page_size > PAGE_SIZE:
                            page_size = PAGE_SIZE
                            return "RETRY"
                        full_broken = True
                        use_full = False
            variables = make_variables(after, page_size)
            if selected_hash:
                try:
                    return await self._persisted(operation_name, variables, selected_hash)
                except PlayerokError as e:
                    if _downgradable(e) and page_size > PAGE_SIZE:
                        page_size = PAGE_SIZE
                        return "RETRY"
                    selected_hash = None
            last_err = None
            for h in hashes:
                try:
                    r = await self._persisted(operation_name, variables, h)
                    selected_hash = h
                    return r
                except PlayerokError as e:
                    last_err = e
                    if _downgradable(e) and page_size > PAGE_SIZE:
                        page_size = PAGE_SIZE
                        return "RETRY"
            if full_query is not None and not full_broken:
                use_full = True
                return "RETRY"
            raise last_err or PlayerokError(f"{root_name}: не удалось получить страницу данных.")

        while page_number < max_pages:
            resp = await fetch_page()
            guard = 0
            while resp == "RETRY" and guard < 3:
                resp = await fetch_page()
                guard += 1
            if resp == "RETRY":
                raise PlayerokError(f"{root_name}: не удалось получить страницу данных.")
            page = self._connection_from(resp, root_name)
            nodes.extend(page["nodes"])
            total_count = page["total_count"] or total_count
            if not page["has_next_page"]:
                truncated = False
                break
            if not page["end_cursor"] or page["end_cursor"] in seen:
                raise PlayerokError(f"{root_name}: повторяющийся курсор.")
            seen.add(page["end_cursor"])
            after = page["end_cursor"]
            page_number += 1
        return {"nodes": nodes, "total_count": total_count, "truncated": truncated}

    @staticmethod
    def _cursor(after) -> str:
        return "null" if after is None else json.dumps(str(after))

    async def get_chats(self, max_pages: int = 40) -> dict:
        viewer = await self.get_viewer()
        uid = json.dumps(viewer["id"])

        def make_variables(after, page_size):
            return {
                "pagination": {"first": page_size, "after": after},
                "filter": {"userId": viewer["id"], "type": None, "status": None},
                "hasSupportAccess": False,
            }

        async def full_query(after, page_size):
            return await self._graphql(
                "userChats",
                f"""
                query userChats {{
                  chats(
                    pagination: {{ first: {page_size}, after: {self._cursor(after)} }}
                    filter: {{ userId: {uid}, type: null, status: null }}
                    hasSupportAccess: false
                  ) {{
                    edges {{
                      node {{
                        id type status unreadMessagesCounter bookmarked isTextingAllowed startedAt finishedAt
                        participants {{ id username role avatarURL isOnline rating __typename }}
                        lastMessage {{ id text createdAt isRead user {{ id username __typename }} __typename }}
                        __typename
                      }}
                      __typename
                    }}
                    pageInfo {{ endCursor hasNextPage __typename }}
                    totalCount
                    __typename
                  }}
                }}
                """,
            )

        result = await self._paginated(
            "userChats", "chats", make_variables, full_query,
            hashes=HASHES["userChats"], max_pages=max_pages, prefer_full=True,
        )
        return {
            "viewer": viewer,
            "chats": [self._norm_chat(n) for n in result["nodes"]],
            "total_count": result["total_count"],
            "truncated": result["truncated"],
        }

    @staticmethod
    def _norm_chat(node: dict) -> dict:
        participants = [
            {
                "id": str(u.get("id")) if u.get("id") else "",
                "username": u.get("username") or "",
                "avatar_url": u.get("avatarURL") or "",
                "is_online": bool(u.get("isOnline")),
                "rating": int(u.get("rating") or 0),
                "role": u.get("role") or "",
            }
            for u in (node.get("participants") or [])
            if isinstance(u, dict)
        ]
        lm = node.get("lastMessage")
        return {
            "id": str(node["id"]),
            "type": node.get("type") or "",
            "status": node.get("status") or "",
            "unread_counter": max(0, int(node.get("unreadMessagesCounter") or 0)),
            "bookmarked": bool(node.get("bookmarked")),
            "is_texting_allowed": node.get("isTextingAllowed") is not False,
            "started_at": node.get("startedAt") or "",
            "finished_at": node.get("finishedAt") or "",
            "participants": participants,
            "last_message": (
                {
                    "id": str(lm["id"]) if lm.get("id") else "",
                    "text": lm.get("text") or "",
                    "created_at": lm.get("createdAt") or "",
                    "is_read": bool(lm.get("isRead")),
                    "user": (
                        {
                            "id": str(lm["user"]["id"]) if lm.get("user", {}).get("id") else "",
                            "username": lm["user"].get("username") or "",
                        }
                        if lm.get("user")
                        else None
                    ),
                }
                if lm
                else None
            ),
        }

    async def get_messages(self, chat_id: str, count: int = 24) -> dict:
        count = max(1, min(count, 24))
        cid = json.dumps(str(chat_id))
        resp = await self._graphql(
            "chatMessages",
            f"""
            query chatMessages {{
              chatMessages(
                pagination: {{ first: {count}, after: null }}
                filter: {{ chatId: {cid} }}
                hasSupportAccess: false
                showForbiddenImage: true
              ) {{
                edges {{
                  node {{
                    id text createdAt deletedAt isRead isSuspicious isAutoResponse event
                    user {{ id username role avatarURL __typename }}
                    __typename
                  }}
                  __typename
                }}
                pageInfo {{ endCursor hasNextPage __typename }}
                totalCount
                __typename
              }}
            }}
            """,
        )
        page = self._connection_from(resp, "chatMessages")
        return {
            "messages": [self._norm_message(n) for n in page["nodes"]],
            "total_count": page["total_count"],
        }

    @staticmethod
    def _norm_message(node: dict) -> dict:
        return {
            "id": str(node["id"]) if node.get("id") else "",
            "text": node.get("text") or "",
            "created_at": node.get("createdAt") or "",
            "deleted_at": node.get("deletedAt") or "",
            "is_read": bool(node.get("isRead")),
            "is_suspicious": bool(node.get("isSuspicious")),
            "is_auto_response": bool(node.get("isAutoResponse")),
            "event": node.get("event") or "",
            "user": (
                {
                    "id": str(node["user"]["id"]) if node.get("user", {}).get("id") else "",
                    "username": node["user"].get("username") or "",
                    "role": node["user"].get("role") or "",
                    "avatar_url": node["user"].get("avatarURL") or "",
                }
                if node.get("user")
                else None
            ),
        }

    async def mark_chat_read(self, chat_id: str) -> dict:
        resp = await self._graphql(
            "markChatAsRead",
            """
            mutation markChatAsRead($input: MarkChatAsReadInput!) {
              markChatAsRead(input: $input) { id unreadMessagesCounter __typename }
            }
            """,
            {"input": {"chatId": str(chat_id)}},
        )
        chat = ((resp.get("data") or {}).get("markChatAsRead")) or {}
        if not chat.get("id"):
            raise PlayerokError("Playerok не подтвердил прочтение чата.")
        return {"id": str(chat["id"]), "unread_counter": int(chat.get("unreadMessagesCounter") or 0)}

    async def send_message(self, chat_id: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise PlayerokError("Нельзя отправить пустое сообщение.")
        if len(text) > 4000:
            raise PlayerokError("Сообщение слишком длинное (лимит 4000).")
        resp = await self._graphql(
            "createChatMessage",
            """
            mutation createChatMessage($input: CreateChatMessageInput!) {
              createChatMessage(input: $input) {
                id text createdAt isRead isAutoResponse
                user { id username __typename }
                __typename
              }
            }
            """,
            {"input": {"chatId": str(chat_id), "imagesIds": [], "text": text}},
        )
        msg = ((resp.get("data") or {}).get("createChatMessage")) or {}
        if not msg.get("id"):
            raise PlayerokError("Playerok не подтвердил отправку сообщения.")
        return self._norm_message(msg)

    async def get_deals(self, max_pages: int = 40, statuses: Optional[list] = None) -> dict:
        viewer = await self.get_viewer()
        uid = json.dumps(viewer["id"])
        status_filter = [str(s) for s in statuses] if statuses else None

        def make_variables(after, page_size):
            return {
                "pagination": {"first": page_size, "after": after},
                "filter": {"userId": viewer["id"], "direction": "OUT", "status": status_filter},
                "showForbiddenImage": True,
            }

        async def full_query(after, page_size):
            status_literal = "null" if not status_filter else "[{}]".format(", ".join(status_filter))
            return await self._graphql(
                "deals",
                f"""
                query deals {{
                  deals(
                    pagination: {{ first: {page_size}, after: {self._cursor(after)} }}
                    filter: {{ userId: {uid}, direction: OUT, status: {status_literal} }}
                  ) {{
                    edges {{
                      node {{
                        id status direction createdAt completedAt hasProblem statusDescription
                        transaction {{ value __typename }}
                        user {{ id username __typename }}
                        chat {{ id __typename }}
                        item {{ id slug name rawPrice price __typename }}
                        __typename
                      }}
                      __typename
                    }}
                    pageInfo {{ endCursor hasNextPage __typename }}
                    totalCount
                    __typename
                  }}
                }}
                """,
            )

        result = await self._paginated(
            "deals", "deals", make_variables, full_query,
            hashes=HASHES["deals"], max_pages=max_pages,
        )
        return {
            "deals": [self._norm_deal(n) for n in result["nodes"]],
            "total_count": result["total_count"],
            "truncated": result["truncated"],
        }

    @staticmethod
    def _norm_deal(node: dict) -> dict:
        item = node.get("item") or {}
        tx = node.get("transaction") or {}
        return {
            "id": str(node["id"]) if node.get("id") else "",
            "status": node.get("status") or "",
            "direction": node.get("direction") or "",
            "created_at": node.get("createdAt") or "",
            "completed_at": node.get("completedAt") or "",
            "has_problem": bool(node.get("hasProblem")),
            "status_description": node.get("statusDescription") or "",
            "price": float(
                item.get("price") if item.get("price") is not None
                else (item.get("rawPrice") if item.get("rawPrice") is not None else tx.get("value") or 0)
            ) or 0,
            "item": {
                "id": str(item["id"]) if item.get("id") else "",
                "slug": item.get("slug") or "",
                "name": item.get("name") or "Без названия",
                "price": float(item.get("price") if item.get("price") is not None else item.get("rawPrice") or 0) or 0,
            },
            "user": (
                {
                    "id": str(node["user"]["id"]) if node.get("user", {}).get("id") else "",
                    "username": node["user"].get("username") or "",
                }
                if node.get("user")
                else None
            ),
            "chat_id": str((node.get("chat") or {}).get("id")) if (node.get("chat") or {}).get("id") else "",
        }

    async def update_deal(self, deal_id: str, status: str) -> dict:
        """Меняет статус сделки (SENT — подтвердить отправку, и т.п.)."""
        resp = await self._graphql(
            "updateDeal",
            """
            mutation updateDeal($input: UpdateItemDealInput!) {
              updateDeal(input: $input) {
                id status direction __typename
              }
            }
            """,
            {"input": {"id": str(deal_id), "status": status}},
        )
        deal = ((resp.get("data") or {}).get("updateDeal")) or {}
        if not deal.get("id"):
            raise PlayerokError("Playerok не подтвердил изменение сделки.")
        return {"id": str(deal["id"]), "status": deal.get("status") or ""}

    async def get_testimonials(self, max_pages: int = 5) -> dict:
        """Отзывы (testimonials) о продавце, статус APPROVED, по убыванию даты."""
        viewer = await self.get_viewer()
        uid = json.dumps(viewer["id"])

        async def full_query(after, page_size):
            return await self._graphql(
                "testimonials",
                f"""
                query testimonials {{
                  testimonials(
                    pagination: {{ first: {page_size}, after: {self._cursor(after)} }}
                    filter: {{ userId: {uid}, status: [APPROVED] }}
                    sort: {{ direction: DESC, field: createdAt }}
                    hasSupportAccess: false
                  ) {{
                    edges {{
                      node {{
                        id status text rating createdAt
                        user {{ id username __typename }}
                        deal {{ id chat {{ id __typename }} __typename }}
                        __typename
                      }}
                      __typename
                    }}
                    pageInfo {{ endCursor hasNextPage __typename }}
                    totalCount
                    __typename
                  }}
                }}
                """,
            )

        result = await self._paginated(
            "testimonials", "testimonials", lambda a, s: {}, full_query,
            hashes=[], max_pages=max_pages,
        )
        return {
            "reviews": [self._norm_review(n) for n in result["nodes"]],
            "total_count": result["total_count"],
        }

    @staticmethod
    def _norm_review(node: dict) -> dict:
        deal = node.get("deal") or {}
        chat = deal.get("chat") or {}
        user = node.get("user") or {}
        return {
            "id": str(node["id"]) if node.get("id") else "",
            "status": node.get("status") or "",
            "text": node.get("text") or "",
            "rating": int(node.get("rating") or 0),
            "created_at": node.get("createdAt") or "",
            "user": {
                "id": str(user["id"]) if user.get("id") else "",
                "username": user.get("username") or "",
            },
            "deal_id": str(deal["id"]) if deal.get("id") else "",
            "chat_id": str(chat["id"]) if chat.get("id") else "",
        }

    async def get_items(self, statuses: Optional[list] = None, max_pages: int = 40) -> dict:
        viewer = await self.get_viewer()
        uid = json.dumps(viewer["id"])
        statuses = [s for s in (statuses or []) if s in ALL_ITEM_STATUSES]
        if not statuses:
            statuses = ALL_ITEM_STATUSES

        def make_variables(after, page_size):
            return {
                "pagination": {"first": page_size, "after": after},
                "filter": {"userId": viewer["id"], "status": statuses},
                "showForbiddenImage": True,
            }

        async def full_query(after, page_size):
            status_list = ", ".join(statuses)
            return await self._graphql(
                "items",
                f"""
                query items {{
                  items(
                    pagination: {{ first: {page_size}, after: {self._cursor(after)} }}
                    filter: {{ userId: {uid}, status: [{status_list}] }}
                  ) {{
                    edges {{
                      node {{
                        id slug name rawPrice price status priorityPosition __typename
                        ... on MyItem {{
                          createdAt priority viewsCounter dealsCounter statusExpirationDate
                          priorityPrice approvalDate
                          attachments(showForbiddenImage: true) {{ url __typename }}
                          __typename
                        }}
                        __typename
                      }}
                      __typename
                    }}
                    pageInfo {{ endCursor hasNextPage __typename }}
                    totalCount
                    __typename
                  }}
                }}
                """,
            )

        result = await self._paginated(
            "items", "items", make_variables, full_query,
            hashes=HASHES["items"], max_pages=max_pages,
        )
        return {
            "items": [self._norm_item(n) for n in result["nodes"]],
            "total_count": result["total_count"],
            "truncated": result["truncated"],
        }

    @staticmethod
    def _norm_item(node: dict) -> dict:
        attachments = node.get("attachments") or []
        first_url = attachments[0].get("url") if attachments and attachments[0] else None
        return {
            "id": str(node["id"]) if node.get("id") else "",
            "slug": node.get("slug") or "",
            "name": node.get("name") or "Без названия",
            "price": float(node.get("price") if node.get("price") is not None else node.get("rawPrice") or 0) or 0,
            "raw_price": float(node.get("rawPrice") or 0) or 0,
            "status": node.get("status") or "",
            "priority": node.get("priority") or "",
            "priority_position": int(node.get("priorityPosition") or 0),
            "views_counter": int(node.get("viewsCounter") or 0),
            "deals_counter": int(node.get("dealsCounter") or 0),
            "status_expiration_date": node.get("statusExpirationDate") or None,
            "priority_price": (
                float(node["priorityPrice"]) if node.get("priorityPrice") is not None else None
            ),
            "approval_date": node.get("approvalDate") or "",
            "created_at": node.get("createdAt") or "",
            "attachment_url": first_url or "",
        }

    async def get_priority_statuses(self, item_id: str, price: float = 0) -> list:
        resp = await self._persisted(
            "itemPriorityStatuses",
            {"itemId": str(item_id), "price": float(price or 0)},
            HASH_ITEM_PRIORITY_STATUSES,
        )
        data = (resp.get("data") or {})
        lst = data.get("itemPriorityStatuses")
        if not isinstance(lst, list):
            raise PlayerokError("itemPriorityStatuses: Playerok вернул неожиданный формат.")
        out = []
        for s in lst:
            if not s or not s.get("id"):
                continue
            out.append(
                {
                    "id": str(s["id"]),
                    "name": s.get("name") if s.get("name") else "",
                    "price": float(s.get("price") or 0),
                    "type": s.get("type") if s.get("type") else "",
                    "period": s.get("period"),
                }
            )
        return out

    _BUMP_FIELDS = """
        id slug name price rawPrice status priority priorityPosition createdAt __typename
        ... on MyItem {
          statusExpirationDate priorityPrice
          statusPayment { id operation direction status statusDescription value __typename }
          __typename
        }
    """

    async def increase_item_priority(self, item_id: str, priority_status_id: str) -> dict:
        resp = await self._graphql(
            "increaseItemPriorityStatus",
            f"""
            mutation increaseItemPriorityStatus($input: PublishItemInput!) {{
              increaseItemPriorityStatus(input: $input) {{ {self._BUMP_FIELDS} }}
            }}
            """,
            {
                "input": {
                    "itemId": str(item_id),
                    "priorityStatuses": [str(priority_status_id)],
                    "transactionProviderData": {"paymentMethodId": None},
                    "transactionProviderId": "LOCAL",
                }
            },
        )
        item = ((resp.get("data") or {}).get("increaseItemPriorityStatus")) or {}
        if not item.get("id"):
            raise PlayerokError("Playerok не подтвердил поднятие лота.")
        return {"item": self._norm_bumped(item)}

    async def publish_item(self, item_id: str, priority_status_id: str) -> dict:
        resp = await self._graphql(
            "publishItem",
            f"""
            mutation publishItem($input: PublishItemInput!) {{
              publishItem(input: $input) {{ {self._BUMP_FIELDS} }}
            }}
            """,
            {
                "input": {
                    "itemId": str(item_id),
                    "priorityStatuses": [str(priority_status_id)],
                    "transactionProviderId": "LOCAL",
                }
            },
        )
        item = ((resp.get("data") or {}).get("publishItem")) or {}
        if not item.get("id"):
            raise PlayerokError("Playerok не подтвердил выставление лота.")
        return {"item": self._norm_bumped(item)}

    @staticmethod
    def _norm_bumped(node: dict) -> Optional[dict]:
        if not node or not node.get("id"):
            return None
        sp = node.get("statusPayment")
        return {
            "id": str(node["id"]),
            "slug": node.get("slug") or "",
            "name": node.get("name") or "",
            "price": float(node.get("price") if node.get("price") is not None else node.get("rawPrice") or 0) or 0,
            "raw_price": float(node.get("rawPrice") or 0) or 0,
            "status": node.get("status") or "",
            "priority": node.get("priority") or "",
            "priority_position": int(node.get("priorityPosition") or 0),
            "status_expiration_date": node.get("statusExpirationDate") or None,
            "priority_price": (
                float(node["priorityPrice"]) if node.get("priorityPrice") is not None else None
            ),
            "created_at": node.get("createdAt") or "",
            "payment": (
                {
                    "id": str(sp["id"]) if sp.get("id") else "",
                    "operation": sp.get("operation") or "",
                    "direction": sp.get("direction") or "",
                    "status": sp.get("status") or "",
                    "status_description": sp.get("statusDescription") or "",
                    "value": float(sp.get("value") or 0) or 0,
                }
                if sp
                else None
            ),
        }


# ---------- парсинг входящих credentials ----------

def is_jwt(s: str) -> bool:
    """Проверяет, похоже ли значение на JWT (три base64url-сегмента через точку)."""
    if not s:
        return False
    parts = s.split(".")
    if len(parts) != 3:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    return all(part and all(ch in allowed for ch in part) for part in parts)


def parse_cookies(raw: str) -> Optional[str]:
    """Превращает ввод пользователя (EditThisCookie JSON / cURL / строка cookie / одиночный токен)
    в строку Cookie-заголовка вида 'name=value; name2=value2'.

    Возвращает None, если ничего похожего на cookies не найдено.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # 1) JSON-экспорт EditThisCookie: [{"name":..., "value":..., "domain":...}, ...]
    if raw.lstrip().startswith("[") or raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            pairs = []
            for c in data:
                if isinstance(c, dict) and c.get("name"):
                    pairs.append(f"{c['name']}={c.get('value', '')}")
            if pairs:
                return "; ".join(pairs)
        if isinstance(data, dict) and data.get("name"):
            return f"{data['name']}={data.get('value', '')}"

    # 2) cURL: curl 'https://...' -H 'cookie: ...' ...
    if raw.lower().startswith("curl"):
        # вытаскиваем все -H '...' и --header
        import re
        headers = re.findall(r"(?:-H|--header)\s+'([^']*)'", raw)
        if not headers:
            headers = re.findall(r'(?:-H|--header)\s+"([^"]*)"', raw)
        for h in headers:
            if h.lower().startswith("cookie:"):
                cookie = h.split(":", 1)[1].strip()
                if cookie:
                    return cookie
        return None

    # 3) Одиночный токен без '=' (JWT-сессия Playerok).
    if "=" not in raw and ";" not in raw and " " not in raw:
        # Playerok хранит JWT-сессию в куке с именем `token`.
        return f"token={raw}"

    # 4) Строка вида "name=value; name2=value2" (возможно с префиксом "Cookie:").
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if "=" in raw:
        return raw

    return None
