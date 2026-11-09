"""Stand-ins for the Hermes modules the plugin imports, shaped after the Hermes sources (gateway/
platforms/base.py, event.py, _shared.py, session_context.py, plugins/plugin_storage.py), so the
plugin's own code runs here without Hermes. The real gateway is exercised on a live Hermes."""

from __future__ import annotations

import enum
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRETS: dict[str, str] = {}
SESSION: dict[str, str] = {}
DATA_DIR: list[Path] = []


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class Platform(str):
    pass


class MessageType(enum.Enum):
    TEXT = "text"


class ProcessingOutcome(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class SessionSource:
    platform: Any
    chat_id: str
    chat_name: str | None = None
    chat_type: str = "dm"
    user_id: str | None = None
    user_name: str | None = None


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    message_id: str | None = None
    source: SessionSource | None = None
    metadata: dict = field(default_factory=dict)
    allow_gateway_control: bool = True  # the Hermes default

    def is_command(self) -> bool:  # event.py:176-178
        return self.allow_gateway_control and self.text.strip().startswith("/")


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: Any = None
    retryable: bool = False


class BasePlatformAdapter:
    def __init__(self, config: Any = None, platform: Any = None):
        self.config, self.platform = config, platform
        self.handled: list[MessageEvent] = []
        self.connected = False
        self.fatal: tuple[str, str, bool] | None = None

    def build_source(
        self, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None, **_
    ):
        return SessionSource(self.platform, str(chat_id), chat_name, chat_type, user_id, user_name)

    async def handle_message(self, event: MessageEvent) -> None:
        self.handled.append(event)

    def _mark_connected(self, **_):
        self.connected = True

    def _mark_disconnected(self):
        self.connected = False

    def _set_fatal_error(self, code, message, *, retryable):
        self.fatal = (code, message, retryable)


def _plugin_data_dir(name: str) -> Path:
    d = DATA_DIR[0] / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_is_connected(*names):
    return lambda config: all(SECRETS.get(n, "").strip() for n in names)


_module("gateway")
_module("gateway.config", Platform=Platform)
_module("gateway.platforms")
_module(
    "gateway.platforms._shared",
    get_scoped_secret=lambda name, default=None, **_: SECRETS.get(name, default),
    env_is_connected=_env_is_connected,
)
_module("gateway.platforms.base", BasePlatformAdapter=BasePlatformAdapter, SendResult=SendResult)
_module(
    "gateway.platforms.event",
    MessageEvent=MessageEvent,
    MessageType=MessageType,
    ProcessingOutcome=ProcessingOutcome,
)
_module(
    "gateway.session_context", get_session_env=lambda name, default="": SESSION.get(name, default)
)
_module("plugins")
_module("plugins.plugin_storage", plugin_data_dir=_plugin_data_dir)

# hermes_cli/plugin_provider_requests.py: Hermes makes the plugin's requests with the person's
# sign-in. HERMES_HTTP["handler"](method, url, json, headers) -> (status, headers, body) stands for
# the network with the token attached; None = not signed in. Like Hermes, the stand-in refuses a
# caller whose frame is not inside the plugin (in a worker thread too).
HERMES_HTTP: dict[str, Any] = {"handler": None}
HERMES_CALLS: list[dict] = []


class ProviderNotSignedIn(RuntimeError):
    pass


def _credentialed_provider_request(provider, method, url, *, json=None, headers=None, timeout=30.0):
    caller = sys._getframe(1).f_code.co_filename
    if "agentygg_hermes" not in caller:
        raise PermissionError("credentialed_provider_request must be called from a plugin's code")
    HERMES_CALLS.append(
        {"provider": provider, "method": method, "url": url, "json": json, "headers": headers}
    )
    handler = HERMES_HTTP["handler"]
    if handler is None:
        raise ProviderNotSignedIn(
            "Not signed in to agentygg for plugin 'agentygg' in this Hermes profile. "
            "Run `hermes auth add agentygg:agentygg`."
        )
    status, rheaders, body = handler(method, url, json, dict(headers or {}))
    return types.SimpleNamespace(status=status, headers=rheaders, body=body)


_module("hermes_cli")
_module(
    "hermes_cli.plugin_provider_requests",
    credentialed_provider_request=_credentialed_provider_request,
    ProviderNotSignedIn=ProviderNotSignedIn,
)

APPROVALS: list[tuple] = []
CLARIFIES: list[tuple] = []
_module("tools")
_module(
    "tools.approval",
    resolve_gateway_approval=lambda session_key, choice, reason=None, request_id=None, **_: (
        APPROVALS.append((session_key, choice, reason, request_id)) or 1
    ),
)
_module(
    "tools.clarify_gateway",
    resolve_gateway_clarify=lambda clarify_id, response: (
        CLARIFIES.append((clarify_id, response)) or True
    ),
)


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    SECRETS.clear()
    SESSION.clear()
    HERMES_HTTP["handler"] = None
    HERMES_CALLS.clear()
    APPROVALS.clear()
    CLARIFIES.clear()
    DATA_DIR[:] = [tmp_path]
    yield
    import agentygg_hermes.adapter as adapter

    adapter._active = None
