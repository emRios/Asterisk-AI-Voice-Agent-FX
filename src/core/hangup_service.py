"""Verifiable channel hangup (§15.A1 of the F2.6 design).

One owner for "hang up this channel and know whether it really went away":

- ``request_hangup`` is the only place that takes the per-channel lock. Callers
  never hold it; they may pass ``revalidate`` (an async predicate evaluated under
  the lock) to cancel the hangup if the call changed state meanwhile.
- The hangup runs as a single-flight Task owned by this service. Callers await it
  through ``asyncio.shield``, so cancelling the caller (e.g. the provider receive
  loop torn down by StasisEnd) does not abort the hangup.
- Confirmation comes from ARI, not from the DELETE status: a Future per channel
  is registered BEFORE the DELETE and resolved by ``notify_channel_gone``, wired
  ONLY to ChannelDestroyed. StasisEnd is not proof (transfer via continue, AMD
  hop or app move leave Stasis with the channel alive); without ChannelDestroyed
  the service asks ARI with a GET.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


class HangupOutcome(str, enum.Enum):
    CONFIRMED = "confirmed"        # ARI reported the channel gone after our DELETE
    ALREADY_GONE = "already_gone"  # DELETE or GET answered 404
    SKIPPED = "skipped"            # revalidate() said the hangup no longer applies
    FAILED = "failed"              # channel still there after one retry


@dataclass
class HangupResult:
    outcome: HangupOutcome
    channel_id: str
    reason: str
    attempts: int = 0
    statuses: List[Any] = field(default_factory=list)


Revalidate = Callable[[], Awaitable[bool]]


class HangupService:
    def __init__(
        self,
        ari_client: Any,
        *,
        request_timeout_secs: float = 3.0,
        confirm_timeout_secs: float = 3.0,
    ) -> None:
        self._ari = ari_client
        self._request_timeout = request_timeout_secs
        self._confirm_timeout = confirm_timeout_secs
        self._locks: Dict[str, "_LockEntry"] = {}
        self._inflight: Dict[str, "asyncio.Task[HangupResult]"] = {}
        self._gone_waiters: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------ events
    def notify_channel_gone(self, channel_id: Optional[str]) -> None:
        """Resolve the confirmation waiter for ``channel_id``, if any.

        Called from ChannelDestroyed only. No-op when no hangup is waiting.
        """
        if not channel_id:
            return
        fut = self._gone_waiters.get(channel_id)
        if fut is not None and not fut.done():
            fut.set_result(True)

    def in_progress(self, channel_id: str) -> bool:
        task = self._inflight.get(channel_id)
        return task is not None and not task.done()

    # ----------------------------------------------------------------- request
    async def request_hangup(
        self,
        channel_id: str,
        reason: str,
        *,
        revalidate: Optional[Revalidate] = None,
    ) -> HangupResult:
        entry = self._locks.get(channel_id)
        if entry is None:
            entry = self._locks[channel_id] = _LockEntry()
        entry.users += 1
        try:
            async with entry.lock:
                task = self._inflight.get(channel_id)
                if task is None:
                    if revalidate is not None and not await revalidate():
                        logger.info("Hangup skipped by revalidation", channel_id=channel_id, reason=reason)
                        return HangupResult(HangupOutcome.SKIPPED, channel_id, reason)
                    task = asyncio.create_task(self._run(channel_id, reason))
                    self._inflight[channel_id] = task
                    task.add_done_callback(lambda _t, cid=channel_id: self._forget(cid, _t))
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(channel_id) is entry:
                del self._locks[channel_id]
        return await asyncio.shield(task)

    def _forget(self, channel_id: str, task: "asyncio.Task[HangupResult]") -> None:
        if self._inflight.get(channel_id) is task:
            del self._inflight[channel_id]

    # --------------------------------------------------------------- execution
    async def _run(self, channel_id: str, reason: str) -> HangupResult:
        loop = asyncio.get_running_loop()
        gone = loop.create_future()
        self._gone_waiters[channel_id] = gone
        result = HangupResult(HangupOutcome.FAILED, channel_id, reason)
        try:
            for attempt in (1, 2):
                result.attempts = attempt
                status = await self._delete(channel_id)
                result.statuses.append(status)
                if status == 404:
                    result.outcome = HangupOutcome.ALREADY_GONE
                    return result
                # Wait for ChannelDestroyed only when the DELETE may have taken
                # effect (2xx or ambiguous timeout); explicit errors go to the GET.
                if (_is_2xx(status) or status == "timeout") and await self._wait_gone(gone):
                    result.outcome = HangupOutcome.CONFIRMED
                    return result
                probe = await self._get(channel_id)
                result.statuses.append(f"GET:{probe}")
                if probe == 404 or gone.done():
                    result.outcome = (
                        HangupOutcome.CONFIRMED if gone.done() else HangupOutcome.ALREADY_GONE
                    )
                    return result
            logger.error(
                "Channel could not be hung up",
                channel_id=channel_id,
                reason=reason,
                statuses=result.statuses,
            )
            return result
        finally:
            if self._gone_waiters.get(channel_id) is gone:
                del self._gone_waiters[channel_id]
            logger.info(
                "Hangup finished",
                channel_id=channel_id,
                reason=reason,
                outcome=result.outcome.value,
                attempts=result.attempts,
                statuses=result.statuses,
            )

    async def _wait_gone(self, gone: asyncio.Future) -> bool:
        if gone.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(gone), self._confirm_timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _delete(self, channel_id: str) -> Any:
        try:
            resp = await asyncio.wait_for(self._ari.hangup_channel(channel_id), self._request_timeout)
        except asyncio.TimeoutError:
            return "timeout"
        except Exception as exc:  # network errors are retried like timeouts
            return f"error:{type(exc).__name__}"
        return _status_of(resp)

    async def _get(self, channel_id: str) -> Any:
        try:
            resp = await asyncio.wait_for(
                self._ari.send_command("GET", f"channels/{channel_id}", tolerate_statuses=[404]),
                self._request_timeout,
            )
        except asyncio.TimeoutError:
            return "timeout"
        except Exception as exc:
            return f"error:{type(exc).__name__}"
        return _status_of(resp)


class _LockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


def _status_of(resp: Any) -> Any:
    """``send_command`` returns {"status": N, ...} on 204/errors and the JSON body on 2xx."""
    if isinstance(resp, dict):
        status = resp.get("status")
        return status if isinstance(status, int) else 200
    logger.warning("Malformed ARI response", response_type=type(resp).__name__)
    return "malformed"


def _is_2xx(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300
