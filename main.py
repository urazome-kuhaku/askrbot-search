import httpx
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.all import MessageEventResult

@register("dual_search", "YourName", "GLM意图路由混合搜索", "1.0.0")
class DualSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 严格按照官方规范，在初始化时挂载配置，跟随 UI 开关热重载
        self._reload_config()
    
    def _reload_config(self):
        """加载配置"""
        self.config = self.context.get_config("dual_search")
        if not self.config:
            self.config = {}
        self.bocha_key = self.config.get("bocha_api_key", "").strip()
        self.ms_key = self.config.get("modelscope_api_key", "").strip()
        self.ms_url = self.config.get("modelscope_mcp_url", "").strip()
    
    async def reload(self):
        """热重载配置"""
        self._reload_config()

    async def call_bocha(self, query: str) -> str:
        """底层方法：直连国内 Bocha API"""
        if not self.bocha_key:
            return "[错误] Bocha API Key 未配置"
        headers = {
            "Authorization": f"Bearer {self.bocha_key}",
            "Content-Type": "application/json"
        }
        payload = {"query": query, "freshness": "noLimit", "summary": True}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post("https://api.bochaai.com/v1/web-search", headers=headers, json=payload)
                if resp.status_code == 200:
                    webpages = resp.json().get("data", {}).get("webPages", {}).get("value", [])
                    return "\n".join([f"来源: {w['url']}\n内容: {w['snippet']}" for w in webpages])
                else:
                    return f"[Bocha API 错误] HTTP {resp.status_code}"
        except Exception as e:
            return f"[Bocha 连接失败] {str(e)}"

    async def call_tavily_via_mcp(self, query: str) -> str:
        """底层方法：通过魔塔 MCP 云端容器调用 Tavily"""
        if not self.ms_key or not self.ms_url:
            raise ValueError("MCP API Key 或 URL 未配置")
        headers = {
            "Authorization": f"Bearer {self.ms_key}",
            "Accept": "text/event-stream"
        }
        # 标准的 MCP SSE 异步上下文管理器调用规范
        async with sse_client(url=self.ms_url, headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                # 触发云端 Tavily 搜索
                result = await session.call_tool(
                    "tavily_web_search", 
                    arguments={"query": query}
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

        yield event.plain_result("⏳ 正在请求 GLM 判断搜索意图...")
        
        # 1. GLM 免费大脑进行意图路由
        router_prompt = (
            f"判断以下搜索意图：如果涉及中国大陆政策、八卦、微信/知乎内容或纯国内新闻，输出'BOCHA'；"
            f"如果涉及海外科技、外语文档、全球宏观，输出'TAVILY'。只能输出这两个词之一。\n"
            f"用户搜索：{query}"
        )
        try:
            router_response = await self.context.get_api().llm_chat(router_prompt)
            intent = router_response.strip().upper()
        except Exception as e:
            yield event.plain_result(f"⚠️ GLM 路由失败({str(e)})，使用默认方案...")
            intent = "BOCHA"

        search_text = ""
        
        # 2. 核心路由与降级分发（兜底统一为TAVILY，月度额度刷新）
        if "TAVILY" in intent:
            yield event.plain_result("📡 意图：海外资讯。正在连接 ModelScope MCP 中继...")
            try:
                search_text = await self.call_tavily_via_mcp(query)
                if not search_text:
                    yield event.plain_result("⚠️ Tavily 无返回结果，尝试备选方案...")
                    search_text = await self.call_bocha(query)
            except Exception as e:
                yield event.plain_result(f"⚠️ MCP连接失败({str(e)})，尝试备选方案...")
                search_text = await self.call_bocha(query)
        elif "BOCHA" in intent:
            yield event.plain_result("📡 意图：国内资讯。正在通过 Bocha 极速检索...")
            try:
                search_text = await self.call_bocha(query)
                if not search_text:
                    yield event.plain_result("⚠️ Bocha 无返回结果，自动降级至 TAVILY 兜底...")
                    search_text = await self.call_tavily_via_mcp(query)
            except Exception as e:
                yield event.plain_result(f"⚠️ Bocha 连接失败({str(e)})，自动降级至 TAVILY 兜底...")
                search_text = await self.call_tavily_via_mcp(query)
        else:
            # 默认方案：优先Bocha，失败降级到TAVILY兜底
            yield event.plain_result("📡 默认方案：正在通过 Bocha 检索...")
            try:
                search_text = await self.call_bocha(query)
                if not search_text:
                    yield event.plain_result("⚠️ Bocha 无返回结果，自动降级至 TAVILY 兜底...")
                    search_text = await self.call_tavily_via_mcp(query)
            except Exception as e:
                yield event.plain_result(f"⚠️ Bocha 连接失败({str(e)})，自动降级至 TAVILY 兜底...")
                search_text = await self.call_tavily_via_mcp(query)

        if not search_text:
            yield event.plain_result("❌ 抓取失败，请检查各路 API Key 或网络连通性。")
            return

        # 3. 最终文本注入与总结
        yield event.plain_result("🧠 资料抓取完成，GLM 正在阅读并总结...")
        final_prompt = (
            f"请基于以下最新的网页搜索结果，回答用户的问题。不要产生搜索结果之外的幻觉。\n\n"
            f"【搜索结果】:\n{search_text}\n\n"
            f"【用户问题】: {query}"
        )
        final_answer = await self.context.get_api().llm_chat(final_prompt)
        yield event.plain_result(final_answer)