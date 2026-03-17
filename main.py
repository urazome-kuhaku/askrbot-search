import httpx
# 🚨 核心修复 1：导入专为魔塔 Serverless 云端打造的 streamable HTTP 引擎
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.all import MessageEventResult, logger
from astrbot.api.provider import LLMResponse

@register("askrbot_search", "YourName", "GLM意图路由混合搜索", "1.0.0")
class DualSearchPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        
        self.bocha_key = self.config.get("bocha_api_key", "").strip()
        self.ms_key = self.config.get("modelscope_api_key", "").strip()
        self.ms_url = self.config.get("modelscope_mcp_url", "").strip()
        
        # 🚨 新增：接管 Tavily 高级配置，并做绝对安全的兜底
        self.tavily_depth = self.config.get("tavily_search_depth", "basic").strip() or "basic"
        try:
            self.tavily_max_results = int(self.config.get("tavily_max_results", 5))
        except (ValueError, TypeError):
            self.tavily_max_results = 5

    async def ask_llm(self, event: AstrMessageEvent, prompt: str) -> str:
        """调用当前会话正在使用的大模型"""
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            return llm_resp.completion_text or ""
        except Exception as e:
            raise Exception(f"大模型层级报错: {str(e)}")

    async def call_bocha(self, query: str) -> str:
        """底层方法：直连国内 Bocha API"""
        if not self.bocha_key:
            raise ValueError("Bocha API Key 暂未配置")
        
        headers = {
            "Authorization": f"Bearer {self.bocha_key}",
            "Content-Type": "application/json"
        }
        payload = {"query": query, "freshness": "noLimit", "summary": True}
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post("https://api.bochaai.com/v1/web-search", headers=headers, json=payload)
            if resp.status_code == 200:
                webpages = resp.json().get("data", {}).get("webPages", {}).get("value", [])
                return "\n".join([f"来源: {w['url']}\n内容: {w['snippet']}" for w in webpages])
            else:
                raise Exception(f"HTTP {resp.status_code} - {resp.text}")

    async def call_tavily_via_mcp(self, query: str) -> str:
        """底层方法：通过魔塔 MCP 云端容器调用 Tavily"""
        if not self.ms_key or not self.ms_url:
            raise ValueError("MCP API Key 或 URL 未配置")
            
        headers = {
            "Authorization": f"Bearer {self.ms_key}"
        }
        
        # 🚨 核心修复 2：使用 streamablehttp_client 引擎，URL 保持魔塔原样（不要加/sse了）
        async with streamablehttp_client(url=self.ms_url, headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                
                # 🚨 核心修复 3：遵循官方源码的中划线命名
                result = await session.call_tool(
                        "tavily-search", 
                        arguments={
                            "query": query,
                            "search_depth": self.tavily_depth,
                            "max_results": self.tavily_max_results
                        }
                    )
                
                if result.content and len(result.content) > 0:
                    return result.content[0].text
                return ""

    @filter.command("search")
    async def handle_search(self, event: AstrMessageEvent):
        query = event.get_message_str().replace("/search", "").strip()
        if not query:
            yield event.plain_result("💡 请输入搜索内容，例如: /search 2026年最新AI框架")
            return

        yield event.plain_result("⏳ 正在请求大模型大脑判断搜索意图...")
        
        router_prompt = (
            f"判断以下搜索意图：如果涉及中国大陆政策、八卦、微信/知乎内容或纯国内新闻，输出'BOCHA'；"
            f"如果涉及海外科技、外语文档、全球宏观，输出'TAVILY'。只能输出这两个词之一。\n"
            f"用户搜索：{query}"
        )
        try:
            router_response = await self.ask_llm(event, router_prompt)
            intent = router_response.strip().upper()
        except Exception as e:
            yield event.plain_result(f"⚠️ 路由判断失败({str(e)})，强行触发链路规则...")
            intent = "BOCHA"

        search_text = ""
        
        # 智能双向降级队列
        if "TAVILY" in intent:
            engines = [("TAVILY", self.call_tavily_via_mcp), ("BOCHA", self.call_bocha)]
        else:
            engines = [("BOCHA", self.call_bocha), ("TAVILY", self.call_tavily_via_mcp)]

        for engine_name, engine_func in engines:
            yield event.plain_result(f"📡 尝试启动 [{engine_name}] 检索链路...")
            try:
                search_text = await engine_func(query)
                if search_text:
                    break 
                else:
                    yield event.plain_result(f"⚠️ [{engine_name}] 返回空白数据，准备降级接管...")
            except Exception as e:
                yield event.plain_result(f"⚠️ [{engine_name}] 链路阻断 ({str(e)})，切换备用方案...")
                search_text = ""

        if not search_text:
            yield event.plain_result("❌ 所有检索通道均已瘫痪，请检查配置。")
            return

        yield event.plain_result("🧠 资料抓取完成，GLM 正在阅读并总结...")
        final_prompt = (
            f"请基于以下最新的网页搜索结果，回答用户的问题。严禁产生搜索结果之外的幻觉。\n\n"
            f"【搜索结果】:\n{search_text}\n\n"
            f"【用户问题】: {query}"
        )
        try:
            final_answer = await self.ask_llm(event, final_prompt)
            yield event.plain_result(final_answer)
        except Exception as e:
            yield event.plain_result(f"❌ 终极总结阶段出错: {str(e)}")