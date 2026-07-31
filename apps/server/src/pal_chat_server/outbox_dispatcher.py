from __future__ import annotations

import threading
import time

from sqlalchemy.orm import Session, sessionmaker

from pal_chat_server.models import ConversationRecord
from pal_chat_server.sequence_runtime import dispatch_pending_outbox


class OutboxDispatcher:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        retry_delay_s: float = 0.25,
    ) -> None:
        self._session_factory = session_factory
        self._retry_delay_s = retry_delay_s
        self._pending: set[str] = set()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                name="pal-chat-outbox-dispatcher",
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, conversation_id: str) -> None:
        with self._condition:
            self._pending.add(conversation_id)
            self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            conversation_ids = self._next_batch()
            if conversation_ids is None:
                return
            for conversation_id in conversation_ids:
                if self._dispatch_one(conversation_id):
                    continue
                if self._should_stop():
                    return
                time.sleep(self._retry_delay_s)
                self.enqueue(conversation_id)

    def _next_batch(self) -> list[str] | None:
        with self._condition:
            while not self._pending and not self._stopping:
                self._condition.wait(timeout=0.5)
            if self._stopping:
                return None
            conversation_ids = list(self._pending)
            self._pending.clear()
            return conversation_ids

    def _dispatch_one(self, conversation_id: str) -> bool:
        with self._session_factory() as db:
            conversation = db.get(ConversationRecord, conversation_id)
            if conversation is None or conversation.archive_dir is None:
                return True
            db.expunge(conversation)
        try:
            dispatch_pending_outbox(conversation)
        except Exception:
            return False
        return True

    def _should_stop(self) -> bool:
        with self._condition:
            return self._stopping
