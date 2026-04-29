# rina_tools.py
import os
import time
import base64
from rina_search import web_search_raw, format_search_results, search_tool_schema

def get_tools():
    """获取所有可用的工具配置"""
    return [
        {
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": "当用户明确要求画图时调用",
                "parameters": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string", "description": "英文提示词"}},
                    "required": ["prompt"]
                }
            }
        },
        search_tool_schema
    ]

def execute_tool_call(client, function_name, function_args):
    """路由并执行具体的工具调用"""
    if function_name == "generate_image":
        prompt = function_args.get("prompt")
        if not prompt:
            return "图片生成失败：缺少 prompt 参数"
        try:
            image_response = client.images.generate(
                model="openai/gpt-image-2",
                prompt=prompt,
                n=1,
                size="1024x1024",
                response_format="b64_json"
            )
            b64_data = image_response.data[0].b64_json
            if not os.path.exists("img"):
                os.makedirs("img")
            img_data = base64.b64decode(b64_data)
            filename = f"img/gen_{int(time.time())}.png"
            with open(filename, "wb") as f:
                f.write(img_data)
            # 提示词和返回格式保持原样不动
            return f"【系统指令】：图像已生成，路径为 {filename}。请务必在回复中原样输出标签 [LOCAL_IMAGE:{filename}] 以触发前端渲染，并用文字描述你画了什么。"
        except Exception as e:
            return f"图片生成失败：{type(e).__name__}: {e}"

    elif function_name == "perform_web_search":
        query = function_args.get("query")
        if not query:
            return f"搜索失败：缺少 query 参数，收到参数={function_args}"
        search_data = web_search_raw(query)
        return format_search_results(search_data)

    return f"未知设备调用：{function_name}"