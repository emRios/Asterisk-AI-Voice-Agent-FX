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
# Synchronous veto, evaluated in the same loop step that issues the DELETE (no
# await in between). Returns a reason string to abort, None to proceed.
Veto = Callable[[], Optional[str]]


class _Vetoed(Exception):
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


class _Flight:
    __slots__ = ("task", "veto", "revoked", "delete_task", "idle_keys")

    def __init__(self, veto: Optional[Veto]) -> None:
        self.task: Optional["asyncio.Task[HangupResult]"] = None
        self.veto = veto  # None = unconditional
        self.revoked: Optional[str] = None  # set by revoke() on a conditional flight
        self.delete_task: Optional[asyncio.Task] = None  # the DELETE request in progress
        self.idle_keys: set = set()

    @property
    def conditional(self) -> bool:
        return self.veto is not None

    def promote(self) -> None:
        self.veto = None
        self.revoked = None

    def blocked_by(self) -> Optional[str]:
        if self.revoked:
            return self.revoked
        return self.veto() if self.veto is not None else None


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
        self._inflight: Dict[str, _Flight] = {}
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
        flight = self._inflight.get(channel_id)
        return flight is not None and flight.task is not None and not flight.task.done()

    def when_idle(self, channel_id: str, callback: Callable[[], None], *, key: Any = None) -> None:
        """Run ``callback`` now, or when the in-flight hangup on the channel ends.

        With ``key``, a callback already queued under the same key for this
        flight is not queued again.
        """
        flight = self._live_flight(channel_id)
        if flight is None:
            callback()
            return
        if key is not None:
            if key in flight.idle_keys:
                return
            flight.idle_keys.add(key)
        flight.task.add_done_callback(lambda _t: callback())

    def revoke(self, channel_id: Optional[str], why: str) -> None:
        """Synchronously stop a CONDITIONAL hangup that has not reached Asterisk.

        Cancels the DELETE request while it is still waiting (connection pool,
        DNS…) and blocks any further attempt. Unconditional hangups are not
        affected. Residual: a DELETE whose request already left cannot be undone.
        """
        flight = self._live_flight(channel_id) if channel_id else None
        if flight is None or not flight.conditional:
            return
        flight.revoked = why
        if flight.delete_task is not None and not flight.delete_task.done():
            flight.delete_task.cancel()
        logger.info("Conditional hangup revoked", channel_id=channel_id, why=why)

    def _live_flight(self, channel_id: str) -> Optional[_Flight]:
        flight = self._inflight.get(channel_id)
        if flight is None or flight.task is None or flight.task.done():
            return None
        return flight

    # ----------------------------------------------------------------- request
    async def request_hangup(
        self,
        channel_id: str,
        reason: str,
        *,
        revalidate: Optional[Revalidate] = None,
        veto: Optional[Veto] = None,
    ) -> HangupResult:
        """Hang up ``channel_id``; conditional when ``revalidate``/``veto`` are given.

        ``revalidate`` (async) runs once under the lock before starting. ``veto``
        (sync) runs right before every DELETE, in the same loop step that issues
        it. An unconditional request joining a conditional flight promotes it to
        unconditional (it never inherits another caller's veto).
        """
        entry = self._locks.get(channel_id)
        if entry is None:
            entry = self._locks[channel_id] = _LockEntry()
        entry.users += 1
        try:
            async with entry.lock:
                if revalidate is not None and not await revalidate():
                    logger.info("Hangup skipped by revalidation", channel_id=channel_id, reason=reason)
                    return HangupResult(HangupOutcome.SKIPPED, channel_id, reason)
                flight = self._live_flight(channel_id)
                if flight is None:
                    flight = _Flight(veto)
                    flight.task = asyncio.create_task(self._run(channel_id, reason, flight))
                    self._inflight[channel_id] = flight
                    flight.task.add_done_callback(lambda _t, cid=channel_id, f=flight: self._forget(cid, f))
                elif veto is None and flight.conditional:
                    # This caller wants the channel gone without conditions: it
                    # must not inherit another caller's veto (nor a revocation).
                    logger.info("Hangup promoted to unconditional", channel_id=channel_id, reason=reason)
                    flight.promote()
                task = flight.task
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(channel_id) is entry:
                del self._locks[channel_id]
        return await asyncio.shield(task)

    def _forget(self, channel_id: str, flight: _Flight) -> None:
        if self._inflight.get(channel_id) is flight:
            del self._inflight[channel_id]

    # --------------------------------------------------------------- execution
    async def _run(self, channel_id: str, reason: str, flight: _Flight) -> HangupResult:
        loop = asyncio.get_running_loop()
        gone = loop.create_future()
        self._gone_waiters[channel_id] = gone
        result = HangupResult(HangupOutcome.FAILED, channel_id, reason)
        try:
            for attempt in (1, 2):
                result.attempts = attempt
                while True:
                    try:
                        status = await self._delete(channel_id, flight)
                        break
                    except _Vetoed as v:
                        if not flight.conditional:
                            continue  # promoted while the veto was resolving: retry now
                        result.attempts = attempt - 1
                        result.outcome = HangupOutcome.SKIPPED
                        result.statuses.append(f"vetoed:{v.why}")
                        return result
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

    async def _delete(self, channel_id: str, flight: Optional[_Flight] = None) -> Any:
        # Synchronous check, then the request starts in its own task so revoke()
        # can cancel it while it still waits for a connection (before any byte
        # reaches Asterisk). Residual: a request that already left.
        if flight is not None:
            why = flight.blocked_by()
            if why:
                raise _Vetoed(why)
        request = asyncio.ensure_future(self._ari.hangup_channel(channel_id))
        if flight is not None:
            flight.delete_task = request
        try:
            resp = await asyncio.wait_for(asyncio.shield(request), self._request_timeout)
        except asyncio.CancelledError:
            if flight is not None and request.cancelled():
                # Only revoke() cancels the request itself. If the flight was
                # promoted meanwhile, _run retries instead of skipping.
                raise _Vetoed(flight.revoked or "revoked")
            request.cancel()  # our own task is being cancelled
            raise
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
