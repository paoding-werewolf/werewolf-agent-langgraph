import json
import logging
import os
import re
import threading
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional, Dict, Any

import yaml
from openai import AsyncOpenAI, OpenAI

from utils.prompt_logger import prompt_logger

logger = logging.getLogger(__name__)


_LLM_CONFIG_PATH = Path(os.getenv("WEREWOLF_AGENT_HOME", "~/.werewolf-agent")).expanduser() / "config.yaml"
_rt_cache: Dict[str, Any] = {"mtime": None, "config": {}}
_rt_lock = threading.Lock()


def _runtime_llm_config() -> Dict[str, Any]:
    """按 mtime 热加载 config.yaml 顶层 llm 段。

    结构：
      llm:
        api_key / base_url / model        # 全局默认（覆盖环境变量）
        overrides:                         # 按模型名路由端点（可选）
          "<model_name>": {api_key, base_url}
    修改文件即生效，无需重启容器。
    """
    try:
        mtime = _LLM_CONFIG_PATH.stat().st_mtime
    except OSError:
        return _rt_cache["config"]
    with _rt_lock:
        if _rt_cache["mtime"] != mtime:
            try:
                with open(_LLM_CONFIG_PATH) as f:
                    raw = yaml.safe_load(f) or {}
                _rt_cache["config"] = raw.get("llm") or {}
            except Exception:
                pass
            _rt_cache["mtime"] = mtime
    return _rt_cache["config"]


def _fn(name: str, description: str, properties: Dict[str, Any], required: list) -> Dict[str, Any]:
    """Build an OpenAI function-tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# OpenAI tool schemas (ported 1:1 from the previous langchain @tool definitions).
TOOLS = [
    _fn("speak",
        "Public speech in daytime discussion. Use for expressing your analysis, suspicion, "
        "defense, or voting intention.",
        {
            "result": {"type": "string", "description": "Your public speech text"},
            "target": {"type": "string", "description": "Target player ID you are addressing or voting against. Use 'all' for general speech."},
            "extra": {"type": "object", "description": "Additional structured data if needed"},
        },
        ["result"]),
    _fn("vote",
        "Cast your elimination vote during vote phase. Must specify target and reason.",
        {
            "target": {"type": "string", "description": "Player ID to vote for elimination"},
            "reason": {"type": "string", "description": "Your reason for this vote"},
        },
        ["target", "reason"]),
    _fn("wolf_kill",
        "Wolf night action: choose a player to kill.",
        {
            "target": {"type": "string", "description": "Player ID to kill"},
            "reason": {"type": "string", "description": "Strategic reason for this target"},
        },
        ["target"]),
    _fn("wolf_chat",
        "Wolf team private chat during night phase. Send a message to your wolf teammates to discuss strategy.",
        {
            "message": {"type": "string", "description": "Your message to wolf teammates (e.g. discuss kill target, coordinate strategy)"},
        },
        ["message"]),
    _fn("seer_check",
        "Seer night action: check a player's alignment.",
        {"target": {"type": "string", "description": "Player ID to check"}},
        ["target"]),
    _fn("witch_heal",
        "Witch night action: use antidote to save a player.",
        {"target": {"type": "string", "description": "Player ID to heal"}},
        ["target"]),
    _fn("witch_poison",
        "Witch night action: use poison to kill a player.",
        {"target": {"type": "string", "description": "Player ID to poison"}},
        ["target"]),
    _fn("guard_protect",
        "Guard night action: protect a player from wolf attack.",
        {"target": {"type": "string", "description": "Player ID to protect"}},
        ["target"]),
    _fn("shoot",
        "Hunter/Wolf King skill: shoot a player when dying.",
        {
            "target": {"type": "string", "description": "Player ID to shoot (or 'pass' to not shoot)"},
            "reason": {"type": "string", "description": "Reason for this shot"},
        },
        ["target"]),
    _fn("decide_signup",
        "Decide whether to run for sheriff. You MUST call this tool with your decision.",
        {"decision": {"type": "string", "enum": ["参选", "不参选"], "description": "参选 = run for sheriff, 不参选 = decline"}},
        ["decision"]),
    _fn("vote_sheriff",
        "Vote for a sheriff candidate.",
        {
            "target": {"type": "string", "description": "Player ID to vote for as sheriff"},
            "reason": {"type": "string", "description": "Why you're voting for this candidate"},
        },
        ["target"]),
    _fn("choose_speech_order",
        "Choose daytime speaking direction as sheriff.",
        {
            "direction": {"type": "string", "enum": ["left", "right"], "description": "left = 警左, right = 警右"},
            "reason": {"type": "string", "description": "Brief reason for choosing this direction"},
        },
        ["direction"]),
    _fn("pass_turn",
        "Pass your turn without taking any action.",
        {},
        []),
]


class AgentErrorType(str, Enum):
    """Agent 行动错误类型，与服务端协议保持一致。"""

    API_ERROR = "api_error"
    TIMEOUT = "timeout"
    LLM_UNEXPECTED_OUTPUT = "llm_unexpected_output"
    INVALID_ACTION = "invalid_action"
    TRANSPORT_ERROR = "transport_error"
    UNKNOWN = "unknown"


def _error_action(error_type: AgentErrorType, message: str) -> Dict[str, Any]:
    return {
        "result": f"ERROR: {message}",
        "target": "all",
        "extra": {"error_type": error_type.value, "error": message},
        "error_type": error_type.value,
        "error": message,
    }


class _SyncFailoverClient:
    """同步 LLM 客户端代理：按主备链路执行 create，主端点异常自动切换备用。"""

    def __init__(self, caller: "LLMCaller"):
        self._caller = caller

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        last: Optional[Exception] = None
        for i, (api_key, base_url, model) in enumerate(self._caller._endpoint_chain()):
            try:
                client = self._caller._sdk_client(api_key, base_url, sync=True)
                kwargs["model"] = model
                resp = client.chat.completions.create(**kwargs)
                if i:
                    logger.warning("LLM 主端点失败, 已切换备用端点 %s", base_url)
                return resp
            except Exception as e:
                last = e
                logger.warning("LLM 端点失败 (%s): %s; 尝试下一端点", base_url, e)
        raise last


class _AsyncFailoverClient:
    """异步 LLM 客户端代理：主备链路语义同 _SyncFailoverClient。"""

    def __init__(self, caller: "LLMCaller"):
        self._caller = caller

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        last: Optional[Exception] = None
        for i, (api_key, base_url, model) in enumerate(self._caller._endpoint_chain()):
            try:
                client = self._caller._sdk_client(api_key, base_url, sync=False)
                kwargs["model"] = model
                resp = await client.chat.completions.create(**kwargs)
                if i:
                    logger.warning("LLM 主端点失败, 已切换备用端点 %s", base_url)
                return resp
            except Exception as e:
                last = e
                logger.warning("LLM 端点失败 (%s): %s; 尝试下一端点", base_url, e)
        raise last


class LLMCaller:
    def __init__(self):
        # 端点与模型在每次调用时动态解析 (config.yaml llm 段 → 环境变量兜底)，
        # 支持不改容器热切换；主端点异常自动按 llm.backups 顺序切换备用。
        self.model: Optional[str] = None
        self.temperature = 0.7
        self._clients: Dict[tuple, Any] = {}

    def _endpoint_chain(self) -> list[tuple[str, str, str]]:
        """按优先级返回 (api_key, base_url, model)：主端点 + llm.backups 备用链。"""
        rt = _runtime_llm_config()
        model = self.model or rt.get("model") or os.getenv("OPENAI_MODEL")
        overrides = rt.get("overrides") or {}
        ov = overrides.get(model) or {} if model else {}
        api_key = ov.get("api_key") or rt.get("api_key") or os.getenv("OPENAI_API_KEY")
        base_url = ov.get("base_url") or rt.get("base_url") or os.getenv("OPENAI_BASE_URL")
        chain = [(api_key, base_url, model)]
        for backup in rt.get("backups") or []:
            b_url = backup.get("base_url")
            if not b_url:
                continue
            chain.append((
                backup.get("api_key") or api_key,
                b_url,
                backup.get("model") or model,
            ))
        return [(k, u, m) for k, u, m in chain if k and u and m]

    def _sdk_client(self, api_key: str, base_url: str, sync: bool):
        key = (sync, base_url, api_key)
        if key not in self._clients:
            cls = OpenAI if sync else AsyncOpenAI
            self._clients[key] = cls(
                api_key=api_key, base_url=base_url, timeout=180.0 if sync else 120.0
            )
        return self._clients[key]

    def _require_api_key(self) -> str:
        chain = self._endpoint_chain()
        if not chain:
            raise RuntimeError("OPENAI_API_KEY is required for LLM calls")
        return chain[0][0]

    def _require_model_config(self) -> tuple[str, str]:
        chain = self._endpoint_chain()
        if not chain:
            raise RuntimeError("OPENAI_BASE_URL is required for LLM calls")
        return chain[0][1], chain[0][2]

    @property
    def async_client(self) -> "_AsyncFailoverClient":
        return _AsyncFailoverClient(self)

    @property
    def client(self) -> "_SyncFailoverClient":
        return _SyncFailoverClient(self)

    async def _chat_with_tools(self, system_prompt: str, user_msg: str):
        _, model = self._require_model_config()
        resp = await self.async_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            tools=TOOLS,
            tool_choice="auto",
            temperature=self.temperature,
        )
        return resp.choices[0].message

    def _tool_call_to_action(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a tool call to the action dict format expected by game server."""
        action = {
            "result": args.get("result", args.get("reason", name)),
            "target": args.get("target", "all"),
            "extra": args.get("extra", {}),
            "tool": name,
        }

        if name == "pass_turn":
            action["result"] = "PASS"
            action["target"] = None

        if name == "wolf_kill":
            action["result"] = args.get("reason", f"Kill player {args.get('target', '')}")
        elif name == "wolf_chat":
            action["result"] = args.get("message", "...")
            action["target"] = None
        elif name == "seer_check":
            action["result"] = f"Check player {args.get('target', '')}"
        elif name == "witch_heal":
            action["result"] = f"Heal player {args.get('target', '')}"
        elif name == "witch_poison":
            action["result"] = f"Poison player {args.get('target', '')}"
        elif name == "guard_protect":
            action["result"] = f"Protect player {args.get('target', '')}"
        elif name == "shoot":
            action["result"] = args.get("reason", f"Shoot player {args.get('target', '')}")
        elif name == "decide_signup":
            action["result"] = args.get("decision", "不参选")
            action["target"] = None
        elif name == "vote_sheriff":
            action["result"] = args.get("target", "")
        elif name == "choose_speech_order":
            action["result"] = args.get("direction", "right")
            action["target"] = None

        return action

    # 发言类阶段: speak 等表达型工具均合法
    _VERBAL_PHASE_KEYWORDS = ("discussion", "last_words", "wolf_chat", "sheriff", "speech")
    # 技能阶段: 工具名必须以对应前缀开头（witch_action→witch_*，guard_action→guard_*……）
    _SKILL_PHASE_PREFIXES = (
        ("witch", "witch"),
        ("guard", "guard"),
        ("seer", "seer"),
        ("wolf_kill", "wolf_kill"),
        ("shoot", "shoot"),
    )

    @classmethod
    def _tool_matches_phase(cls, phase: str, tool: str) -> bool:
        p = (phase or "").lower()
        t = (tool or "").lower()
        if t == "pass_turn":
            return True
        if any(k in p for k in cls._VERBAL_PHASE_KEYWORDS):
            return True
        for phase_key, tool_prefix in cls._SKILL_PHASE_PREFIXES:
            if phase_key in p:
                return t.startswith(tool_prefix)
        return True

    async def decide_with_tools(self, agent_id: str, phase: str,
                                system_prompt: str, user_msg: str,
                                session_id: str = "", external_agent_id: str = "") -> Optional[Dict[str, Any]]:
        try:
            message = await self._chat_with_tools(system_prompt, user_msg)
        except Exception as e:
            err = f"ERROR: {str(e)}"
            prompt_logger.log(agent_id, phase, system_prompt, user_msg, err, session_id, external_agent_id)
            return _error_action(AgentErrorType.API_ERROR, str(e))
        return self._process_tool_response(agent_id, phase, system_prompt, user_msg, message, session_id, external_agent_id)

    # Keep backward-compatible alias
    decide_with_tools_sync = decide_with_tools

    def _process_tool_response(self, agent_id: str, phase: str,
                               system_prompt: str, user_msg: str, message,
                               session_id: str = "", external_agent_id: str = "") -> Optional[Dict[str, Any]]:
        content = message.content or ""
        tool_calls = message.tool_calls or []

        full_response = content
        if tool_calls:
            serialized = [
                {"name": tc.function.name, "args": tc.function.arguments}
                for tc in tool_calls
            ]
            full_response += "\n[TOOL_CALLS] " + json.dumps(serialized, ensure_ascii=False)
        prompt_logger.log(agent_id, phase, system_prompt, user_msg, full_response, session_id, external_agent_id)

        if tool_calls:
            tc = tool_calls[0]
            if not self._tool_matches_phase(phase, tc.function.name):
                # 模型在本阶段调用了不匹配的工具（如女巫行动阶段 speak）。
                # 不能放行：下游会从 result 文本里抽取座位号当目标，
                # 曾导致女巫把文本里自报的"7号"当成毒杀目标毒死自己。
                # 降级为 PASS，走引擎正规的"放弃技能/跳过"路径。
                return {
                    "result": "PASS",
                    "target": None,
                    "extra": {"degraded_tool": tc.function.name},
                    "thought": content,
                    "error_type": AgentErrorType.LLM_UNEXPECTED_OUTPUT.value,
                    "error": f"tool {tc.function.name} is not valid in phase {phase}; degraded to PASS",
                }
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                return {
                    **_error_action(
                        AgentErrorType.LLM_UNEXPECTED_OUTPUT,
                        f"Invalid tool arguments for {tc.function.name}",
                    ),
                    "thought": content,
                }
            action = self._tool_call_to_action(tc.function.name, args)
            action["thought"] = content
            return action

        try:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                parsed.setdefault("thought", content)
                if parsed.get("error") and not parsed.get("error_type"):
                    parsed["error_type"] = AgentErrorType.UNKNOWN.value
                return parsed
            return {
                "result": content,
                "target": "all",
                "extra": {},
                "thought": content,
                "error_type": AgentErrorType.LLM_UNEXPECTED_OUTPUT.value,
                "error": "LLM returned plain text without a tool call or JSON action",
            }
        except Exception:
            return {
                "result": content,
                "target": "all",
                "extra": {},
                "thought": content,
                "error_type": AgentErrorType.LLM_UNEXPECTED_OUTPUT.value,
                "error": "LLM action response could not be parsed",
            }

    async def _stream_chat_content(
        self,
        system_prompt: str,
        user_msg: str,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> str:
        _, model = self._require_model_config()
        stream = await self.async_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=self.temperature,
            stream=True,
        )
        parts = []
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            parts.append(delta)
            await on_delta(delta)
        return "".join(parts)

    async def _chat_content(self, system_prompt: str, user_msg: str) -> str:
        _, model = self._require_model_config()
        resp = await self.async_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=self.temperature,
        )
        return resp.choices[0].message.content or ""

    async def call_with_log(self, agent_id: str, phase: str,
                            system_prompt: str, user_msg: str,
                            session_id: str = "", external_agent_id: str = "",
                            on_delta: Optional[Callable[[str], Awaitable[None]]] = None) -> str:
        """Async LLM call that logs the prompt and returns the content."""
        try:
            if on_delta:
                saw_delta = False

                async def track_delta(delta: str) -> None:
                    nonlocal saw_delta
                    saw_delta = True
                    await on_delta(delta)

                try:
                    content = await self._stream_chat_content(system_prompt, user_msg, track_delta)
                except Exception:
                    if saw_delta:
                        raise
                    content = await self._chat_content(system_prompt, user_msg)
            else:
                content = await self._chat_content(system_prompt, user_msg)
        except Exception as e:
            content = f"ERROR: {str(e)}"

        prompt_logger.log(agent_id, phase, system_prompt, user_msg, content, session_id, external_agent_id)
        return content

    # Keep backward-compatible alias
    call_with_log_sync = call_with_log


class _LazyLLMCaller:
    """延迟初始化真实 LLM 客户端，避免无密钥时导入模块失败。"""

    _instance: Optional[LLMCaller] = None

    def _get_instance(self) -> LLMCaller:
        if self._instance is None:
            self._instance = LLMCaller()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get_instance(), name)


llm = _LazyLLMCaller()
