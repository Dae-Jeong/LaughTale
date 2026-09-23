import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chat_service.core.peer import CloseReason, OfferResult, Peer
from chat_service.schemas.chat import MessageCreated, MessageData, WSError
from chat_service.schemas.responses import ErrorCode


def message(*, event_id=None, text="synthetic") -> MessageCreated:
    identity = event_id or uuid4()
    return MessageCreated(
        event_id=identity,
        message=MessageData(
            message_id=identity,
            conversation_id=uuid4(),
            sender_id=uuid4(),
            client_message_id=uuid4(),
            seq="1",
            text=text,
            created_at=datetime.now(UTC),
        ),
    )


def test_frame_and_byte_capacity_reject_without_exceeding_bounds() -> None:
    async def scenario() -> None:
        peer = Peer()
        for _ in range(64):
            assert (
                peer.offer(WSError(code=ErrorCode.INVALID_INPUT))
                is OfferResult.ACCEPTED
            )
        assert peer.queued_frames == 64
        assert peer.offer(WSError(code=ErrorCode.INVALID_INPUT)) is OfferResult.REJECTED
        assert peer.close_reason is CloseReason.QUEUE_OVERFLOW

        byte_peer = Peer()
        for _ in range(65):
            if byte_peer.offer(message(text="가" * 5_000)) is OfferResult.REJECTED:
                break
        else:
            raise AssertionError(
                "byte capacity did not reject a bounded offer sequence"
            )
        assert byte_peer.queued_bytes <= 262_144
        assert byte_peer.queued_frames < 64
        assert byte_peer.close_reason is CloseReason.QUEUE_OVERFLOW

    asyncio.run(scenario())


def test_duplicate_conflict_and_bounded_dedupe_history() -> None:
    async def scenario() -> None:
        peer = Peer()
        original = message()
        assert peer.offer(original) is OfferResult.ACCEPTED
        assert peer.offer(original) is OfferResult.DUPLICATE
        assert peer.queued_frames == 1
        changed = original.model_copy(
            update={"message": original.message.model_copy(update={"text": "changed"})}
        )
        assert peer.offer(changed) is OfferResult.REJECTED
        assert peer.close_reason is CloseReason.EVENT_CONFLICT

        bounded = Peer()
        for _ in range(300):
            assert bounded.offer(message()) is OfferResult.ACCEPTED
            await bounded.next_frame()
        assert bounded.seen_count == 256

    asyncio.run(scenario())


def test_close_reason_precedence_and_finalization() -> None:
    async def scenario() -> None:
        peer = Peer()
        peer.request_close(CloseReason.DELIVERY_FAILED)
        peer.request_close(CloseReason.SERVER_SHUTDOWN)
        assert peer.close_reason is CloseReason.DELIVERY_FAILED
        peer.request_close(CloseReason.AUTH_REVOKED)
        assert peer.close_reason is CloseReason.AUTH_REVOKED
        peer.request_close(CloseReason.QUEUE_OVERFLOW)
        assert peer.close_reason is CloseReason.AUTH_REVOKED
        assert peer.offer(WSError(code=ErrorCode.INVALID_INPUT)) is OfferResult.REJECTED

        peer.mark_closed()
        assert await peer.wait_closed() is None
        peer.request_close(CloseReason.DELIVERY_FAILED)
        assert peer.close_reason is CloseReason.AUTH_REVOKED
        assert peer.offer(WSError(code=ErrorCode.INVALID_INPUT)) is OfferResult.REJECTED

        finalized = Peer()
        finalized.request_close(CloseReason.DELIVERY_FAILED)
        finalized.mark_closed()
        finalized.request_close(CloseReason.AUTH_REVOKED)
        assert finalized.close_reason is CloseReason.DELIVERY_FAILED

        closed_without_request = Peer()
        closed_without_request.mark_closed()
        assert (
            closed_without_request.offer(WSError(code=ErrorCode.INVALID_INPUT))
            is OfferResult.REJECTED
        )
        assert closed_without_request.close_reason is None

    asyncio.run(scenario())


def test_close_request_and_completion_are_distinct() -> None:
    async def scenario() -> None:
        peer = Peer()
        peer.request_close(CloseReason.SESSION_EXPIRED)
        assert await peer.wait_close_requested() is CloseReason.SESSION_EXPIRED
        waiter = asyncio.create_task(peer.wait_closed())
        await asyncio.sleep(0)
        assert not waiter.done()
        peer.mark_closed()
        await waiter

    asyncio.run(scenario())


def test_cancelled_empty_dequeue_does_not_leak_or_consume() -> None:
    async def scenario() -> None:
        peer = Peer()
        waiter = asyncio.create_task(peer.next_frame())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert peer.queued_frames == 0
        assert peer.queued_bytes == 0
        assert peer.offer(WSError(code=ErrorCode.INVALID_INPUT)) is OfferResult.ACCEPTED
        assert "INVALID_INPUT" in await peer.next_frame()
        assert peer.queued_frames == 0

    asyncio.run(scenario())


def test_dequeue_decrements_utf8_bytes() -> None:
    async def scenario() -> None:
        peer = Peer()
        assert peer.offer(message(text="한글 본문")) is OfferResult.ACCEPTED
        queued_bytes = peer.queued_bytes
        encoded = await peer.next_frame()
        assert queued_bytes == len(encoded.encode())
        assert peer.queued_frames == 0
        assert peer.queued_bytes == 0

    asyncio.run(scenario())
