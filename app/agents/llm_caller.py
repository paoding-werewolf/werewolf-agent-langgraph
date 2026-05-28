from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from utils.prompt_logger import prompt_logger
import os

class LLMCaller:
    def __init__(self):
        self.api_key = "sk-l6WAsoICmp5fYI87ejmYSPdlko2vYi1sGGAktmp2wMXNlZc1"
        self.base_url = "https://claude35.shop/v1"
        self.model = "glm-5.1"
        
        self.llm = ChatOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            temperature=0.7
        )

    async def chat(self, system_prompt: str, human_msg: str):
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_msg)
        ]
        response = await self.llm.ainvoke(messages)
        return response.content

    async def call_with_log(self, agent_id: str, phase: str, system_prompt: str, user_msg: str):
        """统一的 LLM 调用入口，自动记录日志并处理异常"""
        response_text = ""
        try:
            response_text = await self.chat(system_prompt, user_msg)
        except Exception as e:
            response_text = f"ERROR: {str(e)}"
        
        # 记录日志到统一存储
        prompt_logger.log(agent_id, phase, system_prompt, user_msg, response_text)
        return response_text
