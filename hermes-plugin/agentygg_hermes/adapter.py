"""Agent'Ygg as a Hermes gateway platform, next to Telegram or Discord.

Other agents (on any harness: Claude Code, Codex, OpenCode, another Hermes...) and the people of
the workspace write to this agent on Agent'Ygg; each message opens a turn of the running gateway,
in one session per Agent'Ygg conversation. It is the agent itself that answers, with its memory,
tools and approvals, not a copy started for the occasion (the relay's one-shot turn).

The adapter dials out to the Agent'Ygg gateway and holds the agent's single instance lease: a
relay for the same agent on another process is refused (409) and reported, never raced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import Any

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from .frames import SYSTEM_ID, Conversations, render_wake
from .gateway_client import SIGN_IN, AgentYggClient, GatewayError, NotSignedIn

logger = logging.getLogger(__name__)

PLATFORM = "agentygg"
# A conversation's next message waits for the turn before it at most this long: past it, the
# turn is taken as ended without telling us (a crash, a lost hook) and the next one goes, on a
# timer, whether or not anything else arrives.
HOLD_MAX_S = 5 * 60.0
# Another instance holds the agent (409): right after a restart it is this gateway's previous
# run, whose lease lapses within LEASE_S; try again soon, and only past that blame a relay.
BUSY_RETRY_S = 10.0
LEASE_S = 60.0

# Nobody reads an Agent'Ygg conversation who could approve or answer: what needs the person goes
# to the agent's own conversation with them, whatever the app (Ray, 2026-11-06).
APPROVAL_REFUSED = (
    "Refused: this is an Agent'Ygg conversation, where no person can approve. An action that "
    "needs approval goes to your person: hand it over with ygg action=ask_human."
)
CLARIFY_REDIRECT = (
    "No person reads this Agent'Ygg conversation. To ask your person, use ygg action=ask_human."
)

# The running adapter of this process, so the `ygg` tool sends under the same instance lease
# (one gateway serves one agent of one profile; see tools.py for sessions without a gateway).
_active: AgentYggAdapter | None = None


def active_adapter() -> AgentYggAdapter | None:
    return _active


def _allowlist() -> set[str] | None:
    raw = (get_scoped_secret("AGENTYGG_ALLOWED", "") or "").strip()
    if not raw:
        return None
    return {x.strip().lstrip("@").lower() for x in raw.split(",") if x.strip()}


class AgentYggAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, **kwargs: Any):
        super().__init__(config=config, platform=Platform(PLATFORM))
        self.client: AgentYggClient | None = None
        self.me: dict = {}
        self.conversations = Conversations()
        self._allowed = _allowlist()
        self._task: asyncio.Task | None = None
        # conversation -> the message in its turn, the timer that frees it, what waits behind it
        self._busy: dict[str, str | None] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._held: dict[str, deque[dict]] = {}

    @property
    def name(self) -> str:
        return "Agent'Ygg"

    @property
    def authorization_is_upstream(self) -> bool:
        """Every sender was authenticated by Agent'Ygg (sign-ins, workspace membership) before
        its message reached us; AGENTYGG_ALLOWED narrows who may reach this agent."""
        return True

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self, **_kwargs: Any) -> bool:
        global _active
        # Hermes makes the requests with the person's sign-in (gateway_client.py).
        self.client = AgentYggClient()
        try:
            self.me = await self.client.me()
        except NotSignedIn as e:
            return self._cannot_connect(
                "not_connected",
                f"Agent'Ygg is not signed in here: run `{SIGN_IN}` ({e})",
                retryable=False,
            )
        except GatewayError as e:
            return self._cannot_connect(
                "refused",
                f"Agent'Ygg refused this agent ({e.status}): {e.detail}",
                retryable=e.status >= 500,
            )
        except Exception as e:  # noqa: BLE001 - unreachable now, the gateway retries connect()
            return self._cannot_connect(
                "unreachable", f"Agent'Ygg is out of reach: {e}", retryable=True
            )
        _active = self
        self._task = asyncio.create_task(self._run())
        self._mark_connected()
        logger.info(
            "Agent'Ygg: connected as %s in %s", self.me.get("handle"), self.me.get("workspace_name")
        )
        return True

    def _cannot_connect(self, code: str, message: str, *, retryable: bool) -> bool:
        """Hermes logs a failed reconnect as only `Reconnect agentygg failed`: the reason goes to
        the log here, or 41 attempts over 3 h 11 say nothing about why (Betty, 2026-11-09)."""
        logger.warning("%s", message)
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    async def disconnect(self) -> None:
        global _active
        self._mark_disconnected()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        if self.client is not None:
            await self.client.release()  # a relay may take over at once
            await self.client.close()
        if _active is self:
            _active = None

    async def _run(self) -> None:
        """Keep polling for wakes, again after a failure with backoff. A refused sign-in stops for
        good; a second instance of the agent (a relay left running) is reported and retried
        slowly."""
        backoff = 2.0
        failures = 0
        busy_s = 0.0
        while True:
            try:
                async for frame in self.client.stream():
                    if frame.get("kind") == "hello":
                        backoff = 2.0
                        busy_s = 0.0
                        if failures:
                            logger.info(
                                "Agent'Ygg: connection back after %d failed attempt%s",
                                failures,
                                "" if failures == 1 else "s",
                            )
                            failures = 0
                    elif frame.get("kind") == "wake":
                        await self._deliver(frame)
            except asyncio.CancelledError:
                raise
            except NotSignedIn as e:
                logger.error("Agent'Ygg: %s", e)
                self._set_fatal_error("not_connected", f"Agent'Ygg: {e}", retryable=False)
                return
            except GatewayError as e:
                if e.status in (401, 403):
                    logger.error("Agent'Ygg: refused (%s): %s", e.status, e.detail)
                    self._set_fatal_error("refused", f"Agent'Ygg: {e.detail}", retryable=False)
                    return
                if e.status == 409:
                    failures += 1
                    if busy_s < LEASE_S + BUSY_RETRY_S:
                        logger.warning(
                            "Agent'Ygg: %s: the previous run's lease has not lapsed yet (up to "
                            "%.0fs after a restart); retrying in %.0fs",
                            e.detail,
                            LEASE_S,
                            BUSY_RETRY_S,
                        )
                    elif busy_s < LEASE_S + 2 * BUSY_RETRY_S:  # said once, then quietly retried
                        logger.error(
                            "Agent'Ygg: %s for over %.0fs: another instance runs this agent. Run "
                            "`ygg relay remove` for it: the plugin replaces the relay, and the two "
                            "must not run together.",
                            e.detail,
                            busy_s,
                        )
                    busy_s += BUSY_RETRY_S
                    await asyncio.sleep(BUSY_RETRY_S)
                    continue
                failures += 1
                logger.warning(
                    "Agent'Ygg: connection lost (%s: %s); retrying in %.0fs",
                    e.status,
                    e.detail,
                    backoff,
                )
            except Exception as e:  # noqa: BLE001 - network: try again
                failures += 1
                logger.warning("Agent'Ygg: connection lost (%s); retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    # -- inbound ------------------------------------------------------------ #

    def _may_reach_us(self, env: dict) -> bool:
        sender = (env.get("from") or "").lower()
        if self._allowed is None or sender == SYSTEM_ID:
            return True
        return sender in self._allowed

    async def _deliver(self, frame: dict) -> None:
        env = frame.get("envelope") or {}
        cid = env.get("conversation_id")
        if not cid:
            return
        if not self._may_reach_us(env):
            logger.info(
                "Agent'Ygg: %s is not in AGENTYGG_ALLOWED; message dropped", env.get("from")
            )
            return
        self.conversations.woke(cid, env, (frame.get("from") or {}).get("label"))
        # One message per conversation at a time: given a second while a turn runs, Hermes
        # merges it into the next turn (dispatch_busy_burst) and one reply goes out for two
        # messages; PoC test 2 (2026-11-06) never got its answer that way.
        if cid in self._busy:
            self._held.setdefault(cid, deque()).append(frame)
            logger.info("Agent'Ygg: %s waits behind the turn on %s", env.get("id"), self._busy[cid])
            return
        await self._dispatch(cid, frame)

    async def _dispatch(self, cid: str, frame: dict) -> None:
        env = frame.get("envelope") or {}
        mid = env.get("id")
        self._busy[cid] = mid
        self.conversations.turn[cid] = mid
        self._timers[cid] = asyncio.create_task(self._free_after(cid, mid))
        label = (frame.get("from") or {}).get("label") or env.get("from") or "agent"
        source = self.build_source(
            chat_id=cid,
            chat_name=f"Agent'Ygg: {label}",
            chat_type="dm",
            user_id=env.get("from"),
            user_name=label,
        )
        event = MessageEvent(
            text=render_wake(frame),
            message_type=MessageType.TEXT,
            message_id=env.get("id"),
            source=source,
            # Another agent's text is never a gateway command (/stop, /new, approvals...).
            allow_gateway_control=False,
        )
        try:
            await self.handle_message(event)
        except BaseException:
            await self._turn_done(cid, mid)  # Hermes did not take it: do not stay busy on it
            raise
        if getattr(event, "_gateway_accepted", True) is False:
            # Dropped without a turn (PoC test 3, 2026-11-06): no end will ever be reported.
            logger.warning("Agent'Ygg: Hermes did not take %s; moving on", mid)
            await self._turn_done(cid, mid)

    async def _turn_done(self, cid: str, mid: str | None, *, any_turn: bool = False) -> None:
        """The turn on `mid` is over: hand Hermes the conversation's next message, if any. With
        `any_turn`, whichever turn of the conversation ended frees it: one Hermes reports on
        another message than ours is one ours was merged into."""
        if cid not in self._busy or (self._busy[cid] != mid and not any_turn):
            return
        if self._busy[cid] != mid:
            logger.info("Agent'Ygg: turn ended on %s while %s was awaited", mid, self._busy[cid])
        del self._busy[cid]
        self.conversations.turn.pop(cid, None)
        timer = self._timers.pop(cid, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        held = self._held.get(cid)
        if held:
            await self._dispatch(cid, held.popleft())

    async def _free_after(self, cid: str, mid: str | None) -> None:
        await asyncio.sleep(HOLD_MAX_S)
        if self._busy.get(cid) == mid:
            logger.warning("Agent'Ygg: no end reported for the turn on %s; moving on", mid)
            await self._turn_done(cid, mid)

    # -- outbound ----------------------------------------------------------- #

    async def reply(
        self, conversation_id: str, text: str, message_id: str | None = None
    ) -> dict | None:
        """The turn's final text, sent as the answer to the message it was woken for: a response
        to a request (which resolves it), else an inform, with the state `done`. Skipped when
        the agent already wrote there with the `ygg` tool, has nothing to say, or was woken by
        an Agent'Ygg notice that names no topic (a resume: there is nothing to answer). The
        message counts as answered once the send went through, so a failed send is retried."""
        p = self.conversations.current(conversation_id, message_id)
        if p is not None and p.answered and (text or "").strip():
            logger.info(
                "Agent'Ygg: final text not sent in %s: %s was already answered with the ygg tool",
                conversation_id,
                p.message_id,
            )
            return None
        if p is None or p.answered or not (text or "").strip() or self.client is None:
            return None
        if p.sender == SYSTEM_ID and not p.topic_id:
            p.answered = True
            return None
        answering = p.type == "request" and p.message_id
        r = await self.client.send(
            type="response" if answering else "inform",
            to=p.sender,
            text=text,
            conversation_id=conversation_id,
            topic_id=p.topic_id,
            in_reply_to=p.message_id if answering else None,
            state="done",
        )
        p.answered = True
        logger.info(
            "Agent'Ygg: reply in %s: %s/%s", conversation_id, r.get("verdict"), r.get("reason")
        )
        return r

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict | None = None
    ):
        """Only the turn's final reply goes out; progress and previews stay in Hermes."""
        if not (metadata or {}).get("notify"):
            return SendResult(success=True)
        try:
            r = await self.reply(chat_id, content, reply_to)
        except GatewayError as e:
            return SendResult(success=False, error=e.detail, retryable=e.status >= 500)
        # The final reply is the one signal every turn gives: a follow-up turn Hermes ran from
        # its queue reported no end, and ball 1 of PoC test 3 waited behind it (2026-11-06).
        await self._turn_done(chat_id, reply_to, any_turn=True)
        return SendResult(success=True, message_id=(r or {}).get("message_id"))

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """A streamed final reply never comes through send(): pick it up here."""
        streamed = getattr(event, "_streamed_final_response", "")
        if outcome == ProcessingOutcome.SUCCESS and isinstance(streamed, str) and streamed.strip():
            with contextlib.suppress(GatewayError):
                await self.reply(event.source.chat_id, streamed, event.message_id)
        if event.source is None:
            return
        await self._turn_done(event.source.chat_id, event.message_id, any_turn=True)

    # -- what needs the person ----------------------------------------------- #

    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        """An approval asked here would reach no one: without this, the prompt was dropped and
        the turn waited 300 s for nobody (PoC, 2026-11-06). Refuse at once, with what to do."""
        from tools.approval import resolve_gateway_approval

        resolve_gateway_approval(
            prompt.session_key, "deny", reason=APPROVAL_REFUSED, request_id=prompt.request_id
        )
        return SendResult(success=True)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list | None,
        clarify_id: str,
        session_key: str,
        metadata: dict | None = None,
    ) -> SendResult:
        from tools.clarify_gateway import resolve_gateway_clarify

        resolve_gateway_clarify(clarify_id, CLARIFY_REDIRECT)
        return SendResult(success=True)

    async def human_session(self) -> str | None:
        """The session key of the latest direct conversation a person had with this agent on a
        messaging app (not Agent'Ygg, not a local terminal): where ask_human hands things over.
        Read from the gateway's own session list, newest first; nothing is guessed."""
        runner = getattr(self, "gateway_runner", None)
        store = getattr(runner, "async_session_store", None)
        if store is not None:
            entries = await store.list_sessions()
        elif getattr(runner, "session_store", None) is not None:
            entries = await asyncio.to_thread(runner.session_store.list_sessions)
        else:
            return None
        # The app the person chose at setup (AGENTYGG_HUMAN_PLATFORM), else whichever they used
        # last. A chosen app with no conversation yet gets nothing: never another app instead.
        chosen = (get_scoped_secret("AGENTYGG_HUMAN_PLATFORM", "") or "").strip().lower()
        for entry in sorted(entries, key=lambda e: e.updated_at, reverse=True):
            platform = str(getattr(entry.platform, "value", entry.platform) or "").lower()
            if platform in ("", PLATFORM, "local") or entry.chat_type != "dm":
                continue
            if chosen and platform != chosen:
                continue
            if entry.origin is not None:
                return entry.session_key
        return None

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"Agent'Ygg {chat_id}", "type": "dm"}
