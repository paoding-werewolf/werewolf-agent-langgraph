import json
from typing import Optional, List, Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_core.tools import tool
from utils.prompt_logger import prompt_logger


class LLMCaller:
    def __init__(self):
        self.api_key = "sk-REMOVED"
        self.base_url = "https://claude35.shop/v1"
        self.model = "deepseek-v4-pro"

        self._create_tools()
        self.llm = ChatOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            temperature=0.7,
        )
        self.llm_with_tools = self.llm.bind_tools(self._tools)

    def _create_tools(self):
        @tool
        def speak(result: str, target: str = "all", extra: Optional[Dict[str, Any]] = None):
            """Public speech in daytime discussion. Use for expressing your analysis, suspicion, defense, or voting intention.

            Args:
                result: Your public speech text
                target: Target player ID you are addressing or voting against. Use "all" for general speech.
                extra: Additional structured data if needed
            """
            pass

        @tool
        def vote(target: str, reason: str):
            """Cast your elimination vote during vote phase. Must specify target and reason.

            Args:
                target: Player ID to vote for elimination
                reason: Your reason for this vote
            """
            pass

        @tool
        def wolf_kill(target: str, reason: str = ""):
            """Wolf night action: choose a player to kill.

            Args:
                target: Player ID to kill
                reason: Strategic reason for this target
            """
            pass

        @tool
        def wolf_gesture(gesture: str, target: Optional[str] = None):
            """Wolf team communication gesture during night phase.

            Args:
                gesture: Gesture type (point, lowkey, shift, change, agree, pass)
                target: Target player ID for the gesture
            """
            pass

        @tool
        def seer_check(target: str):
            """Seer night action: check a player's alignment.

            Args:
                target: Player ID to check
            """
            pass

        @tool
        def witch_heal(target: str):
            """Witch night action: use antidote to save a player.

            Args:
                target: Player ID to heal
            """
            pass

        @tool
        def witch_poison(target: str):
            """Witch night action: use poison to kill a player.

            Args:
                target: Player ID to poison
            """
            pass

        @tool
        def guard_protect(target: str):
            """Guard night action: protect a player from wolf attack.

            Args:
                target: Player ID to protect
            """
            pass

        @tool
        def shoot(target: str, reason: str = ""):
            """Hunter/Wolf King skill: shoot a player when dying.

            Args:
                target: Player ID to shoot (or "pass" to not shoot)
                reason: Reason for this shot
            """
            pass

        @tool
        def signup_sheriff(reason: str = ""):
            """Sign up for sheriff election.

            Args:
                reason: Why you should be sheriff
            """
            pass

        @tool
        def vote_sheriff(target: str, reason: str = ""):
            """Vote for a sheriff candidate.

            Args:
                target: Player ID to vote for as sheriff
                reason: Why you're voting for this candidate
            """
            pass

        @tool
        def pass_turn():
            """Pass your turn without taking any action."""
            pass

        self._tools = [
            speak, vote, wolf_kill, wolf_gesture, seer_check,
            witch_heal, witch_poison, guard_protect, shoot,
            signup_sheriff, vote_sheriff, pass_turn,
        ]

    async def chat(self, system_prompt: str, human_msg: str) -> str:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_msg),
        ]
        response = await self.llm.ainvoke(messages)
        return str(response.content)

    async def chat_with_tools(self, system_prompt: str, human_msg: str) -> AIMessage:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_msg),
        ]
        response = await self.llm_with_tools.ainvoke(messages)
        return response

    def _tool_call_to_action(self, tool_call) -> Optional[Dict[str, Any]]:
        """Convert a tool call to the action dict format expected by game server."""
        name = tool_call["name"]
        args = tool_call.get("args", {})

        action = {
            "result": args.get("result", args.get("reason", name)),
            "target": args.get("target", "all"),
            "extra": args.get("extra", {}),
            "tool": name,
        }

        # Handle pass_turn
        if name == "pass_turn":
            action["result"] = "PASS"
            action["target"] = None

        # Handle wolf_kill / seer_check / witch_* etc
        if name == "wolf_kill":
            action["result"] = args.get("reason", f"Kill player {args.get('target', '')}")
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
        elif name == "signup_sheriff":
            action["result"] = args.get("reason", "I want to be sheriff")
            action["target"] = None
        elif name == "vote_sheriff":
            action["result"] = args.get("reason", f"Vote {args.get('target', '')} for sheriff")

        return action

    async def call_with_log(self, agent_id: str, phase: str, system_prompt: str, user_msg: str) -> str:
        response_text = ""
        try:
            response_text = await self.chat(system_prompt, user_msg)
        except Exception as e:
            response_text = f"ERROR: {str(e)}"

        prompt_logger.log(agent_id, phase, system_prompt, user_msg, response_text)
        return response_text

    def call_with_log_sync(self, agent_id: str, phase: str, system_prompt: str, user_msg: str) -> str:
        response_text = ""
        try:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_msg),
            ]
            response = self.llm.invoke(messages)
            response_text = str(response.content)
        except Exception as e:
            response_text = f"ERROR: {str(e)}"

        prompt_logger.log(agent_id, phase, system_prompt, user_msg, response_text)
        return response_text

    async def decide_with_tools(self, agent_id: str, phase: str,
                                 system_prompt: str, user_msg: str) -> Optional[Dict[str, Any]]:
        response = await self.chat_with_tools(system_prompt, user_msg)
        return self._process_tool_response(agent_id, phase, system_prompt, user_msg, response)

    def decide_with_tools_sync(self, agent_id: str, phase: str,
                                 system_prompt: str, user_msg: str) -> Optional[Dict[str, Any]]:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_msg),
        ]
        response = self.llm_with_tools.invoke(messages)
        return self._process_tool_response(agent_id, phase, system_prompt, user_msg, response)

    def _process_tool_response(self, agent_id: str, phase: str,
                                 system_prompt: str, user_msg: str, response) -> Optional[Dict[str, Any]]:
        content = str(response.content) if response.content else ""

        full_response = content
        if response.tool_calls:
            full_response += "\n[TOOL_CALLS] " + json.dumps(response.tool_calls, ensure_ascii=False)
        prompt_logger.log(agent_id, phase, system_prompt, user_msg, full_response)

        if response.tool_calls:
            return self._tool_call_to_action(response.tool_calls[0])

        import re
        try:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
            else:
                return {"result": content, "target": "all", "extra": {}}
        except Exception:
            return {"result": content, "target": "all", "extra": {}}


llm = LLMCaller()