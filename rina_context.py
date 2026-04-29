# rina_context.py
"""
上下文构造与滚动摘要模块
职责：
- 把“完整历史 messages”压缩成“真正发给模型的 api_messages”
- 维护一份滚动摘要 conversation_summary，吸收旧对话
- 不改动 st.session_state.messages 原始数据，只读不写（摘要更新除外）
"""
import copy

# ==========================================
# 可调参数（集中在这里，方便以后挪到侧边栏）
# ==========================================
RECENT_ROUNDS = 3              # 保留最近多少"轮"（1 轮 ≈ 1 user + 后续 assistant/tool）
SUMMARY_TRIGGER = 6           # 可摘要区 ≥ 此值触发摘要更新
SUMMARY_MODEL = "google/gemini-3.1-flash-lite-preview"  # 摘要用的便宜模型，可换
SUMMARY_MAX_TOKENS = 800       # 摘要输出上限


# ==========================================
# 工具 1：把消息内容压平成纯文本（用于摘要 & token 估算）
# ==========================================
def _flatten_content(content):
    """把 str / list[dict] 统一成 str，图片/文件保留占位符。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(item.get("text", ""))
            elif t == "image_url":
                parts.append("[图片]")
            else:
                parts.append(f"[{t}]")
        return "\n".join(parts)
    return str(content)


# ==========================================
# 工具 2：找到"最近 N 轮"的起始索引
# ==========================================
def _find_recent_start_index(messages, recent_rounds):
    """
    messages 不含 system。从后往前数 recent_rounds 个 user 消息，
    返回第一个 user 的索引；不足则返回 0。
    """
    user_positions = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_positions) <= recent_rounds:
        return 0
    return user_positions[-recent_rounds]


# ==========================================
# 工具 3：保证 tool 消息的 tool_call_id 配对不被切断
# ==========================================
def _safe_cut_point(messages, cut_index):
    """
    如果 cut_index 把一个 assistant(tool_calls) 和它的 tool 响应切开了，
    就把 cut_index 往前挪到那个 assistant 之前，避免 API 报错。
    """
    if cut_index <= 0 or cut_index >= len(messages):
        return cut_index

    # 如果切点正好是 tool 消息，往前找到它对应的 assistant
    while cut_index < len(messages) and messages[cut_index].get("role") == "tool":
        cut_index -= 1
        if cut_index < 0:
            return 0

    # 如果切点之后紧跟 tool，但切点自己不是带 tool_calls 的 assistant，说明切坏了
    # 简化处理：往前找到最近一个 user 消息作为安全切点
    while cut_index > 0 and messages[cut_index].get("role") != "user":
        cut_index -= 1
    return cut_index


# ==========================================
# 核心 1：构造真正发给模型的 api_messages
# ==========================================
def build_context_for_api(full_messages, conversation_summary, summarized_upto_index,
                          recent_rounds=RECENT_ROUNDS):
    """
    :param full_messages: st.session_state.messages（含 system）
    :param conversation_summary: 当前滚动摘要字符串
    :param summarized_upto_index: 已被摘要吸收的"非 system 消息"截止索引（exclusive）
    :param recent_rounds: 保留最近几轮
    :return: list[dict] —— 可直接塞给 client.chat.completions.create(messages=...)
    """
    if not full_messages:
        return []

    # 拆出 system
    system_msg = full_messages[0] if full_messages[0].get("role") == "system" else None
    body = full_messages[1:] if system_msg else full_messages[:]

    if not body:
        return [system_msg] if system_msg else []

    # 计算"最近 N 轮"的起始点
    recent_start = _find_recent_start_index(body, recent_rounds)
    # 安全切：不能把 tool_call 配对切散
    recent_start = _safe_cut_point(body, recent_start)

    # 保证不会回退到已经被摘要吸收的区域之前（摘要外再带一份反而浪费）
    # 但也要保证 recent 区至少有东西，所以取 max
    effective_start = max(recent_start, summarized_upto_index)
    # 如果 effective_start 把 recent 区整个跳过了，回退到 recent_start
    if effective_start >= len(body):
        effective_start = recent_start

    recent_slice = body[effective_start:]

    # 拼装最终 api_messages
    api_messages = []
    if system_msg:
        api_messages.append(system_msg)

    if conversation_summary and conversation_summary.strip():
        api_messages.append({
            "role": "system",
            "content": (
                "以下是本轮对话早期内容的摘要，供你参考上下文连续性（不是用户最新发言）：\n\n"
                f"{conversation_summary.strip()}\n\n"
                "以上为历史摘要，接下来是最近的真实对话。"
            )
        })

    api_messages.extend(recent_slice)
    return api_messages


# ==========================================
# 核心 2：判断是否需要更新摘要
# ==========================================
def should_update_summary(full_messages, summarized_upto_index,
                          recent_rounds=RECENT_ROUNDS,
                          summary_trigger=SUMMARY_TRIGGER):
    """
    当"已摘要区 ~ 最近 N 轮起点"之间累计的消息数 ≥ summary_trigger 时，触发。
    """
    if not full_messages:
        return False
    body = full_messages[1:] if full_messages[0].get("role") == "system" else full_messages
    recent_start = _find_recent_start_index(body, recent_rounds)
    recent_start = _safe_cut_point(body, recent_start)

    pending_count = max(0, recent_start - summarized_upto_index)
    return pending_count >= summary_trigger


# ==========================================
# 核心 3：生成/更新滚动摘要
# ==========================================
def update_summary(client, full_messages, conversation_summary, summarized_upto_index,
                   recent_rounds=RECENT_ROUNDS,
                   summary_model=SUMMARY_MODEL):
    """
    增量式更新摘要：把 [summarized_upto_index : recent_start] 区间的对话压缩进 summary。

    :return: (new_summary: str, new_summarized_upto_index: int, used_tokens: int)
             如果无事发生，返回原值。
    """
    if not full_messages:
        return conversation_summary, summarized_upto_index, 0

    body = full_messages[1:] if full_messages[0].get("role") == "system" else full_messages
    recent_start = _find_recent_start_index(body, recent_rounds)
    recent_start = _safe_cut_point(body, recent_start)

    if recent_start <= summarized_upto_index:
        return conversation_summary, summarized_upto_index, 0

    # 要压缩的新区间
    new_slice = body[summarized_upto_index:recent_start]
    if not new_slice:
        return conversation_summary, summarized_upto_index, 0

    # 构造"待摘要文本"
    transcript_lines = []
    for m in new_slice:
        role = m.get("role", "unknown")
        if role == "tool":
            transcript_lines.append(f"[工具返回:{m.get('name','')}] {_flatten_content(m.get('content'))[:500]}")
        else:
            text = _flatten_content(m.get("content"))
            if m.get("tool_calls"):
                tool_names = [tc.get("function", {}).get("name", "?") for tc in m["tool_calls"]]
                text += f"  (调用工具: {', '.join(tool_names)})"
            transcript_lines.append(f"[{role}] {text}")
    transcript = "\n".join(transcript_lines)

    # 调摘要模型
    summarize_prompt = [
        {
            "role": "system",
            "content": (
                "你是对话摘要助手。请把下面的对话片段压缩成简洁的要点摘要，"
                "保留：用户的关键诉求、已确认的事实/设定、重要结论、未解决的问题、"
                "涉及的文件名或工具调用结果要点。不要寒暄、不要颜文字、不要复述原话。"
                "如果已存在旧摘要，请把新内容合并进去，输出一份更新后的完整摘要。"
            )
        },
        {
            "role": "user",
            "content": (
                f"【旧摘要（可能为空）】\n{conversation_summary or '（无）'}\n\n"
                f"【新增对话片段】\n{transcript}\n\n"
                "请输出更新后的完整摘要（中文，分点，不超过 400 字）："
            )
        }
    ]

    try:
        resp = client.chat.completions.create(
            model=summary_model,
            messages=summarize_prompt,
            max_tokens=SUMMARY_MAX_TOKENS,
            stream=False,
        )
        new_summary = resp.choices[0].message.content.strip()
        used_tokens = resp.usage.total_tokens if resp.usage else 0
        return new_summary, recent_start, used_tokens
    except Exception as e:
        # 摘要失败不影响主流程，保持原状
        print(f"[摘要更新失败] {type(e).__name__}: {e}")
        return conversation_summary, summarized_upto_index, 0