# rina_search.py
import os
from tavily import TavilyClient


def web_search_raw(query: str, max_results: int = 5) -> dict:
    """
    使用 Tavily 替换不稳定的 DuckDuckGo，彻底解决 403 被拦截问题
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "query": query, "results": [], "error": "query 为空"}

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"ok": False, "query": query, "results": [], "error": "未配置 TAVILY_API_KEY"}

    try:
        client = TavilyClient(api_key=api_key)
        # search_depth="basic" 足以满足大模型的日常检索需求，响应极快
        response = client.search(query=query, max_results=max_results, search_depth="basic")

        results = []
        for item in response.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "href": item.get("url", ""),
                "body": item.get("content", "")
            })

        return {"ok": True, "query": query, "results": results, "error": None}

    except Exception as e:
        return {"ok": False, "query": query, "results": [], "error": f"{type(e).__name__}: {e}"}


def format_search_results(search_data: dict) -> str:
    """
    格式化逻辑保持不变，确保外层的 test3.py 无缝调用
    """
    if not search_data.get("ok"):
        return f"搜索工具异常：{search_data.get('error')}"

    results = search_data.get("results", [])
    if not results:
        return "搜索完成，但没有找到相关结果。"

    formatted_results = []
    for i, res in enumerate(results, 1):
        title = res.get("title") or "无标题"
        href = res.get("href") or "无链接"
        body = res.get("body") or "无摘要"
        formatted_results.append(
            f"**[{i}] {title}**\n"
            f"链接: {href}\n"
            f"摘要: {body}"
        )
    return "\n\n".join(formatted_results)


def perform_web_search(query: str, max_results: int = 5) -> str:
    search_data = web_search_raw(query, max_results=max_results)
    return format_search_results(search_data)


search_tool_schema = {
    "type": "function",
    "function": {
        "name": "perform_web_search",
        "description": "当用户询问最新新闻、实时信息、或者你知识库之外的客观事实时，调用此工具进行联网搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "提取出的核心搜索关键词，需要尽量精准"
                }
            },
            "required": ["query"]
        }
    }
}