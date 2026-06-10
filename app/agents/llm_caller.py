import json
import os
import re
from typing import Optional, Dict, Any

from openai import AsyncOpenAI, OpenAI

from utils.prompt_logger import prompt_logger


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


class LLMCaller:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL")
        self.model = os.getenv("OPENAI_MODEL")
        self.temperature = 0.7
        self._async_client: Optional[AsyncOpenAI] = None
        self._client: Optional[OpenAI] = None

    def _require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for LLM calls")
        return self.api_key

    def _require_model_config(self) -> tuple[str, str]:
        if not self.base_url:
            raise RuntimeError("OPENAI_BASE_URL is required for LLM calls")
        if not self.model:
            raise RuntimeError("OPENAI_MODEL is required for LLM calls")
        return self.base_url, self.model

    @property
    def async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            base_url, _ = self._require_model_config()
            self._async_client = AsyncOpenAI(api_key=self._require_api_key(), base_url=base_url)
        return self._async_client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            base_url, _ = self._require_model_config()
            self._client = OpenAI(api_key=self._require_api_key(), base_url=base_url)
        return self._client

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

    async def decide_with_tools(self, agent_id: str, phase: str,
                                system_prompt: str, user_msg: str,
                                session_id: str = "", external_agent_id: str = "") -> Optional[Dict[str, Any]]:
        try:
            message = await self._chat_with_tools(system_prompt, user_msg)
        except Exception as e:
            err = f"ERROR: {str(e)}"
            prompt_logger.log(agent_id, phase, system_prompt, user_msg, err, session_id, external_agent_id)
            return {"result": err, "target": "all", "extra": {}}
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
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            action = self._tool_call_to_action(tc.function.name, args)
            action["thought"] = content
            return action

        try:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                parsed.setdefault("thought", content)
                return parsed
            return {"result": content, "target": "all", "extra": {}, "thought": content}
        except Exception:
            return {"result": content, "target": "all", "extra": {}, "thought": content}

    async def call_with_log(self, agent_id: str, phase: str,
                            system_prompt: str, user_msg: str,
                            session_id: str = "", external_agent_id: str = "") -> str:
        """Async LLM call that logs the prompt and returns the content."""
        try:
            _, model = self._require_model_config()
            resp = await self.async_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=self.temperature,
            )
            content = resp.choices[0].message.content or ""
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
