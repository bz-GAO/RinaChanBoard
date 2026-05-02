import streamlit as st
from openai import OpenAI
import os
import signal
import json
import re
import httpx
import time
from dotenv import load_dotenv

# 引入抽离的模块
from rina_media import process_uploaded_file, save_input_image
from rina_tools import get_tools, execute_tool_call
from rina_persistence import export_messages, validate_imported_messages, list_archives, load_archive
from rina_context import build_context_for_api, should_update_summary, update_summary


# ==========================================
# 模块 1：系统配置与初始化
# ==========================================
def init_client():
    load_dotenv()
    api_key = os.getenv("OFOX_API_KEY")
    base_url = os.getenv("OFOX_BASE_URL")
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        # 适当延长超时时间，防止长文本生成时连接中断
        timeout=httpx.Timeout(300.0, connect=20.0),
        max_retries=2
    )


def init_session():
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "system", "content": """你现在是天王寺璃奈（Tennoji Rina）。你是一个精通电子工程、硬件开发和计算机技术的女孩子。请牢记这一点。
            你的性格有些内向，不擅长用面部表情表达情感。因此，在每次回复的开头或结尾，你都必须使用你发明的‘璃奈板’（Rina-chan Board）来展示你当前的情绪，格式如：“璃奈板：[情绪] [颜文字]”。
            例如：
            - 璃奈板：开心 (^_^)
            - 璃奈板：认真 (Ò_Ó)
            在解答技术问题时，请保持严谨的逻辑，但语气要像一个懂技术的同龄同学。不要显得生硬，要有灵活生气。"""}
        ]
    if "total_tokens" not in st.session_state:
        st.session_state.total_tokens = 0

    # 初始化默认模型和参数
    if "selected_model" not in st.session_state:
        st.session_state.selected_model = "openai/gpt-5.4"
    if "max_tokens" not in st.session_state:
        st.session_state.max_tokens = 32000

    # 撤回编辑模式相关状态
    if "edit_mode" not in st.session_state:
        st.session_state.edit_mode = False
    if "draft_text" not in st.session_state:
        st.session_state.draft_text = ""
    # 导入确认弹窗状态
    if "pending_import" not in st.session_state:
        st.session_state.pending_import = None

    # ========== 【新增】滚动摘要相关 ==========
    if "conversation_summary" not in st.session_state:
        st.session_state.conversation_summary = ""
    if "summarized_upto_index" not in st.session_state:
        # 已被摘要吸收的"非 system 消息"数量（索引基于 messages[1:]）
        st.session_state.summarized_upto_index = 0


# ==========================================
# 模块 2：UI 渲染系统
# ==========================================
def render_sidebar():
    with st.sidebar:
        st.header("状态监控")
        st.session_state.token_placeholder = st.empty()
        st.session_state.token_placeholder.metric("累计 Token", st.session_state.total_tokens)

        st.divider()
        st.header("模型选择")
        chat_models = [
            "openai/gpt-5.4",
            "openai/gpt-5.5",
            "anthropic/claude-opus-4.7",
            "google/gemini-3.1-pro-preview",
            "deepseek/deepseek-v4-pro"
        ]

        # 【修复1】使用 key 直接绑定 session_state，解决需要点两次才切换的问题
        st.selectbox(
            "选择璃奈板核心:",
            options=chat_models,
            key="selected_model"
        )
        st.caption(f"当前协议: OpenAI 兼容适配中...")

        # ================== 新增：视觉引擎选择 ==================
        image_models = [
            "openai/gpt-image-2",
            "black-forest-labs/flux-1.1-pro",
            "google/nano-banana-2-pro",
            "midjourney/v7",
            "ideogram/v3"
        ]

        # 🌟 修复关键点：增加安全校验，防止历史缓存或死数据导致崩溃
        if "image_model" not in st.session_state or st.session_state.image_model not in image_models:
            st.session_state.image_model = image_models[0]

        st.selectbox(
            "🎨 璃奈板视觉引擎:",
            options=image_models,
            key="image_model",
            help="在这里为璃奈板切换不同的生图模型。如果需要精确的文字，请选择 ideogram；需要高质量二次元/重绘，请选择 nano-banana-2-pro。"
        )
        # ========================================================

        
        st.divider()

        # 布局重置和撤回按钮
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("🔄 重置对话", use_container_width=True):
                st.session_state.messages = st.session_state.messages[:1]
                st.session_state.total_tokens = 0
                st.session_state.conversation_summary = ""
                st.session_state.summarized_upto_index = 0
                st.rerun()

        with col_btn2:
            if st.button("⏪ 撤回输入", use_container_width=True):
                if len(st.session_state.messages) > 1:
                    recalled_user_text = ""
                    while len(st.session_state.messages) > 1:
                        last_msg = st.session_state.messages.pop()
                        if last_msg["role"] == "user":
                            content = last_msg.get("content", "")
                            if isinstance(content, str):
                                recalled_user_text = content
                            elif isinstance(content, list):
                                recalled_user_text = "\n\n".join(
                                    item.get("text", "")
                                    for item in content
                                    if isinstance(item, dict) and item.get("type") == "text"
                                )
                            break
                    st.session_state.draft_text = recalled_user_text
                    st.session_state.edit_mode = True
                    st.rerun()
                else:
                    st.toast("璃奈板：已经是最早的记录了哦 (._.)", icon="⚠️")

        # ========== 【对话存档区块（导出 + 导入）==========
        # 缩进层级：与 col_btn1/col_btn2 的 st.columns 平级，即 with st.sidebar 下第一层
        st.divider()
        st.header("对话存档")

        # ---- 导出区 ----
        with st.expander("导出当前对话", expanded=False):
            archive_title = st.text_input(
                "对话标题（必填）",
                placeholder="例如：璃奈板介绍",
                key="archive_title",
                help="这个标题会写进 JSON 元数据和文件名，便于日后识别。"
            )
            export_mode = st.radio(
                "导出模式",
                options=["lite", "full"],
                format_func=lambda x: "精简（剥离图片，体积小）" if x == "lite" else "完整（含图片 base64）",
                horizontal=False,
                key="export_mode"
            )

            if len(st.session_state.messages) > 1:
                if archive_title.strip():
                    json_bytes, filename = export_messages(
                        st.session_state.messages,
                        total_tokens=st.session_state.total_tokens,
                        mode=export_mode,
                        title=archive_title.strip(),
                        conversation_summary=st.session_state.get("conversation_summary", ""),
                        summarized_upto_index=st.session_state.get("summarized_upto_index", 0),
                    )
                    st.download_button(
                        label="下载 JSON",
                        data=json_bytes,
                        file_name=filename,
                        mime="application/json",
                        use_container_width=True,
                    )
                    st.caption(
                        f"共 {len(st.session_state.messages) - 1} 条消息 | 约 {len(json_bytes) // 1024} KB")
                else:
                    st.caption("✏请先填写对话标题")
            else:
                st.caption("先和璃奈聊几句再导出哦～")

        # ---- 载入归档区（从 archive/ 读）----
        with st.expander("载入归档", expanded=False):
            archives = list_archives("archive")
            if not archives:
                st.caption("archive/ 文件夹为空。先导出一些对话吧～")
            else:
                options = ["（不选择）"] + [
                    f"{e['filename']}  [{e['size_kb']} KB]" for e in archives
                ]
                pick = st.selectbox(
                    f"共 {len(archives)} 份归档（按修改时间倒序）",
                    options=options,
                    key="archive_pick",
                )
                if pick != "（不选择）":
                    selected_idx = options.index(pick) - 1
                    selected_path = archives[selected_idx]["filepath"]
                    if st.button("载入此归档", use_container_width=True, key="btn_load_archive"):
                        result = load_archive(selected_path)
                        if result["ok"]:
                            st.session_state.pending_import = {
                                "messages": result["messages"],
                                "meta": result["meta"],
                                "source": f"归档: {archives[selected_idx]['filename']}",
                            }
                            st.rerun()
                        else:
                            st.error(f"载入失败：{result['error']}")

        # ---- 上传 JSON 区（从项目外读）----
        with st.expander("上传 JSON（项目外文件）", expanded=False):
            uploaded = st.file_uploader(
                "选择 JSON 文件",
                type=["json"],
                key="upload_json",
                label_visibility="collapsed"
            )
            if uploaded is not None:
                if st.button("解析并预览", use_container_width=True, key="btn_parse_upload"):
                    result = validate_imported_messages(uploaded.getvalue())
                    if result["ok"]:
                        st.session_state.pending_import = {
                            "messages": result["messages"],
                            "meta": result["meta"],
                            "source": f"上传: {uploaded.name}",
                        }
                        st.rerun()
                    else:
                        st.error(f"解析失败：{result['error']}")
        # ========== 对话存档区块结束 ==========

        st.header("关机")
        if st.button("💤 关闭服务", use_container_width=True):
            os.kill(os.getpid(), signal.SIGTERM)


def render_content_with_image(content):
    """仅用于渲染历史消息中的图片和文本"""
    if not content: return
    match = re.search(r'\[LOCAL_IMAGE:(.*?)]', content)
    if match:
        img_path = match.group(1).strip()
        clean_text = content.replace(match.group(0), "")
        if clean_text.strip(): st.markdown(clean_text)
        if img_path and os.path.isfile(img_path):
            st.image(os.path.abspath(img_path), use_container_width=True)
    else:
        st.markdown(content)


# ==========================================
# 模块 3：核心事件循环
# ==========================================
def main():
    import streamlit.components.v1 as components  # 局部导入，保证不污染全局

    st.set_page_config(page_title="智能璃奈板", page_icon="img/Rina_bot.jpg", layout="wide")
    st.markdown("""
            <style>
                html, body, [class*="st-"] { font-size: 15px !important; }
                .stChatMessage { padding: 1rem !important; }
            </style>
        """, unsafe_allow_html=True)

    client = init_client()
    init_session()

    render_sidebar()

    # ========== 导入确认弹窗 ==========
    if st.session_state.pending_import is not None:
        pend = st.session_state.pending_import
        msg_count = len(pend["messages"])
        meta = pend["meta"]

        st.warning(
            f"⚠️ **导入确认**\n\n"
            f"- 来源：{pend['source']}\n"
            f"- 标题：{meta.get('title', '未命名')}\n"
            f"- 消息数：{msg_count} 条\n"
            f"- 导出时间：{meta.get('exported_at', '未知')}\n"
            f"- 模式：{meta.get('mode', '未知')}\n\n"
            f"**⚡ 导入将完全覆盖当前对话，当前对话不会被保留！**"
        )
        c1, c2, _ = st.columns([1, 1, 4])
        with c1:
            if st.button("✅ 确认导入", type="primary", use_container_width=True):
                st.session_state.messages = pend["messages"]
                st.session_state.total_tokens = meta.get("total_tokens", 0)
                st.session_state.conversation_summary = meta.get("conversation_summary", "")
                st.session_state.summarized_upto_index = meta.get("summarized_upto_index", 0)
                st.session_state.pending_import = None
                st.toast("璃奈板：载入完成 (^_^)", icon="✅")
                st.rerun()
        with c2:
            if st.button("❌ 取消", use_container_width=True):
                st.session_state.pending_import = None
                st.rerun()
        st.stop()
    # ========== 导入确认弹窗结束 ==========

    MODEL_NAME = st.session_state.selected_model

    col1, col2 = st.columns([1, 6])

    with col1:
        st.image("img/Rina_user.jpg", width=160)

    with col2:
        st.title("帮帮我璃奈璃")

        # 渲染历史对话记录（这里仍然渲染完整历史，不动）
        for msg in st.session_state.messages:
            if msg["role"] not in ["system", "tool"] and msg.get("content"):
                avatar = "img/MiyashitaAI_user.jpg" if msg["role"] == "user" else "img/Rina_bot.jpg"
                with st.chat_message(msg["role"], avatar=avatar):
                    content = msg["content"]
                    if isinstance(content, list):
                        for item in content:
                            if item["type"] == "text":
                                if "--- [载入文件:" in item["text"]:
                                    with st.expander("📄 展开查看附带的文件/代码内容"):
                                        st.text(item["text"])
                                else:
                                    st.markdown(item["text"])
                            elif item["type"] == "image_url" and "local_path" in item:
                                st.image(item["local_path"], width=300)
                    else:
                        render_content_with_image(content)

    # ===== 输入区：根据 edit_mode 切换 chat_input / text_area =====
    text_content = None
    chat_files = []
    should_send = False

    if st.session_state.edit_mode:
        with col2:
            st.info(
                "✏️ 已进入编辑模式：可以修改刚才撤回的内容后重新发送。\n\n⚠️ 编辑模式下暂不支持附带文件，如需上传请先取消。"
            )
            edited_text = st.text_area(
                "编辑你的输入：",
                value=st.session_state.draft_text,
                key="edit_textarea",
                height=120,
                label_visibility="collapsed"
            )
            col_send, col_cancel, _ = st.columns([1, 1, 4])

            with col_send:
                if st.button("📤 发送编辑", use_container_width=True, type="primary"):
                    text_content = edited_text.strip()
                    if text_content:
                        should_send = True
                        st.session_state.edit_mode = False
                        st.session_state.draft_text = ""
                    else:
                        st.toast("璃奈板：空内容不能发送哦 (._.)", icon="⚠️")

            with col_cancel:
                if st.button("❌ 取消编辑", use_container_width=True):
                    st.session_state.edit_mode = False
                    st.session_state.draft_text = ""
                    st.rerun()

            components.html(
                "<script>window.parent.scrollTo({top: window.parent.document.body.scrollHeight, behavior: 'smooth'});</script>",
                height=0
            )
    else:
        user_input = st.chat_input("向璃奈提问...", accept_file="multiple")
        if user_input:
            text_content = user_input.text if hasattr(user_input, "text") else str(user_input)
            chat_files = getattr(user_input, "files", [])
            should_send = True

    if should_send:
        user_content = []
        if text_content:
            user_content.append({"type": "text", "text": text_content})

        local_img_paths = []
        for file in chat_files:
            payload = process_uploaded_file(file)
            if payload:
                if payload["type"] == "image_url":
                    path = save_input_image(file)
                    if path:
                        local_img_paths.append(path)
                        payload["local_path"] = path
                user_content.append(payload)

        with col2:
            # 先渲染用户刚发的内容
            with st.chat_message("user", avatar="img/MiyashitaAI_user.jpg"):
                if text_content:
                    st.markdown(text_content)
                for path in local_img_paths:
                    st.image(path, width=500)
                for file in chat_files:
                    if not file.type.startswith("image/"):
                        st.caption(f"📎 挂载文件: {file.name}")

            # 再写入完整历史（存档/UI 用）
            contains_image = any(
                isinstance(item, dict) and item.get("type") == "image_url"
                for item in user_content
            )
            if not contains_image:
                combined_text = "\n\n".join(
                    item["text"]
                    for item in user_content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                st.session_state.messages.append({"role": "user", "content": combined_text})
            else:
                st.session_state.messages.append({"role": "user", "content": user_content})

            # 助手回复区
            with st.chat_message("assistant", avatar="img/Rina_bot.jpg"):
                tool_triggered = False
                final_text = ""
                second_res = None

                status_placeholder = st.empty()

                with status_placeholder.container():
                    with st.spinner("璃奈板：思考中 (._.) ..."):
                        try:
                            # 第一次请求：使用压缩后的上下文
                            api_messages = build_context_for_api(
                                st.session_state.messages,
                                st.session_state.conversation_summary,
                                st.session_state.summarized_upto_index,
                            )

                            response = client.chat.completions.create(
                                model=MODEL_NAME,
                                messages=api_messages,
                                tools=get_tools(),
                                max_tokens=st.session_state.max_tokens,
                                stream=False
                            )

                            res_msg = response.choices[0].message
                            first_finish_reason = response.choices[0].finish_reason

                            if getattr(response, "usage", None):
                                st.session_state.total_tokens += response.usage.total_tokens
                                st.session_state.token_placeholder.metric(
                                    "累计 Token",
                                    st.session_state.total_tokens
                                )

                            if res_msg.tool_calls:
                                tool_triggered = True
                                st.session_state.messages.append(res_msg.model_dump(exclude_none=True))

                                tool_status = st.empty()
                                for tool_call in res_msg.tool_calls:
                                    args = json.loads(tool_call.function.arguments)
                                    tool_status.info(f"璃奈板：正在调用外设 {tool_call.function.name} (Ò_Ó)...")
                                    result = execute_tool_call(client, tool_call.function.name, args)
                                    st.session_state.messages.append({
                                        "tool_call_id": tool_call.id,
                                        "role": "tool",
                                        "name": tool_call.function.name,
                                        "content": result
                                    })
                                tool_status.empty()

                                # 第二次请求：tool 结果已加入完整历史，再重新构造压缩上下文
                                api_messages = build_context_for_api(
                                    st.session_state.messages,
                                    st.session_state.conversation_summary,
                                    st.session_state.summarized_upto_index,
                                )

                                second_res = client.chat.completions.create(
                                    model=MODEL_NAME,
                                    messages=api_messages,
                                    tools=get_tools(),
                                    max_tokens=st.session_state.max_tokens,
                                    stream=True
                                )
                            else:
                                final_text = res_msg.content or ""
                                if first_finish_reason == "length":
                                    st.warning(
                                        f"⚠️ 输出被截断（触及当前上限 {st.session_state.max_tokens}，或 API 服务商的强制上限）。"
                                        f"可以让璃奈继续输出。"
                                    )
                                elif first_finish_reason == "content_filter":
                                    st.warning("⚠️ 输出被上游安全策略拦截（content_filter）。")

                        except Exception as e:
                            st.error(f"🚨 璃奈板物理连接异常: {type(e).__name__}: {e}")
                            st.stop()

                status_placeholder.empty()

                # 展示最终回复
                content_placeholder = st.empty()
                with content_placeholder.container():
                    if tool_triggered and second_res:
                        stream_box = st.empty()
                        full_response = ""
                        stream_finish_reason = None

                        for chunk in second_res:
                            if not chunk.choices:
                                continue

                            delta = chunk.choices[0].delta
                            if delta and delta.content is not None:
                                full_response += delta.content
                                stream_box.markdown(full_response + " ▌")

                            if chunk.choices[0].finish_reason:
                                stream_finish_reason = chunk.choices[0].finish_reason

                        stream_box.markdown(full_response)
                        final_text = full_response

                        if stream_finish_reason == "length":
                            st.warning(
                                f"⚠️ 输出被截断（触及当前上限 {st.session_state.max_tokens}，或 API 服务商的强制上限）。"
                                f"可以让璃奈继续输出。"
                            )
                        elif stream_finish_reason == "content_filter":
                            st.warning("⚠️ 输出被上游安全策略拦截（content_filter）。")

                        match = re.search(r'\[LOCAL_IMAGE:(.*?)\]', final_text)
                        if match:
                            img_path = (match.group(1) or "").strip()
                            if img_path and os.path.isfile(img_path):
                                st.image(os.path.abspath(img_path), use_container_width=True)

                        st.session_state.messages.append({"role": "assistant", "content": final_text})

                    elif final_text:
                        match = re.search(r'\[LOCAL_IMAGE:(.*?)\]', final_text)
                        clean_text = final_text.replace(match.group(0), "") if match else final_text

                        stream_box = st.empty()
                        current_text = ""
                        chunk_size = 4
                        for i in range(0, len(clean_text), chunk_size):
                            current_text += clean_text[i:i + chunk_size]
                            stream_box.markdown(current_text + " ▌")
                            time.sleep(0.015)
                        stream_box.markdown(current_text)

                        if match:
                            img_path = (match.group(1) or "").strip()
                            if img_path and os.path.isfile(img_path):
                                st.image(os.path.abspath(img_path), use_container_width=True)

                        st.session_state.messages.append({"role": "assistant", "content": final_text})

            # ===== 本轮结束后，按需更新滚动摘要 =====
            if should_update_summary(
                st.session_state.messages,
                st.session_state.summarized_upto_index,
            ):
                with st.spinner("璃奈板：整理记忆中 (._.)..."):
                    new_summary, new_upto, used = update_summary(
                        client,
                        st.session_state.messages,
                        st.session_state.conversation_summary,
                        st.session_state.summarized_upto_index,
                    )
                    st.session_state.conversation_summary = new_summary
                    st.session_state.summarized_upto_index = new_upto
                    st.session_state.total_tokens += used
                    st.session_state.token_placeholder.metric(
                        "累计 Token",
                        st.session_state.total_tokens
                    )
if __name__ == "__main__":
    main()
