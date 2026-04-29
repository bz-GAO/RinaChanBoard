# rina_persistence.py
"""
对话持久化模块：负责 messages 的序列化 / 反序列化 / 校验
当前支持：JSON 导出（完整模式 / 精简模式）
预留接口：JSON 导入校验（功能 a 的下半部分将启用）
"""
import json
import copy
import uuid
from datetime import datetime

# ==========================================
# 常量定义
# ==========================================
SCHEMA_VERSION = "1.1"
VALID_MODES = ("full", "lite")
VALID_ROLES = ("system", "user", "assistant", "tool")



def _sanitize_filename(name: str, max_len: int = 30) -> str:
    """
    清洗标题用作文件名：
    - 移除 Windows/Linux 文件系统非法字符 \\ / : * ? " < > |
    - 合并空白符为单个下划线
    - 控制长度
    """
    import re
    cleaned = re.sub(r'[\\/:*?"<>|]', '', name)
    cleaned = re.sub(r'\s+', '_', cleaned).strip('_')
    if not cleaned:
        cleaned = "未命名对话"
    return cleaned[:max_len]


# ==========================================
# 内部工具：清洗消息体
# ==========================================
def _is_base64_data_url(url: str) -> bool:
    """判断一个 image_url 是否为 base64 data URL（而非 http 远程链接）"""
    return isinstance(url, str) and url.startswith("data:")


def _strip_base64_images(messages):
    """
    精简模式专用：仅剥离 data:image/... 形式的 base64 内嵌图片
    远程链接（http/https）保持原样，因为它们不占体积也不能再生成
    """
    cleaned = copy.deepcopy(messages)
    for msg in cleaned:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not (isinstance(item, dict) and item.get("type") == "image_url"):
                continue
            url_obj = item.get("image_url", {})
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
            # 仅处理 base64 data URL，远程链接放行
            if _is_base64_data_url(url):
                local_path = item.get("local_path", "未知路径")
                item["image_url"] = {
                    "url": f"[STRIPPED: base64 图片已剥离，本地路径={local_path}]"
                }
    return cleaned


# ==========================================
# 对外接口 1：导出
# ==========================================
def export_messages(messages, total_tokens=0, mode="full", title=None, conversation_id=None,
                    conversation_summary="", summarized_upto_index=0):
    """
    将 messages 序列化为 JSON 字节流，供 st.download_button 直接下载

    :param conversation_summary: 当前滚动摘要（新增）
    :param summarized_upto_index: 摘要已吸收的消息索引（新增）
    """
    if mode not in VALID_MODES:
        raise ValueError(f"非法导出模式 mode={mode!r}，合法取值: {VALID_MODES}")

    if mode == "lite":
        payload_messages = _strip_base64_images(messages)
    else:
        payload_messages = messages

    now_iso = datetime.now().isoformat(timespec="seconds")

    payload = {
        "meta": {
            "version": SCHEMA_VERSION,
            "exported_at": now_iso,
            "mode": mode,
            "total_tokens": total_tokens,
            "message_count": len(payload_messages),
            "conversation_id": conversation_id or uuid.uuid4().hex,
            "title": title or "未命名对话",
            "created_at": now_iso,
            "updated_at": now_iso,
            # 【新增】摘要状态
            "conversation_summary": conversation_summary or "",
            "summarized_upto_index": summarized_upto_index or 0,
        },
        "messages": payload_messages,
    }

    json_str = json.dumps(payload, ensure_ascii=False, indent=2)
    json_bytes = json_str.encode("utf-8")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = _sanitize_filename(title) if title else "未命名对话"
    filename = f"rina_chat_{safe_title}_{mode}_{timestamp}.json"

    return json_bytes, filename
# ==========================================
# 对外接口 2：导入校验（骨架加固版，下一步启用 UI）
# ==========================================
def validate_imported_messages(raw_bytes):
    """
    将上传的 JSON 字节流解析并校验，返回可用的 messages 列表

    :param raw_bytes: st.file_uploader 读到的字节流
    :return: dict — {"ok": bool, "messages": list, "meta": dict, "error": str}
    """
    # 【修正 #4】使用 utf-8-sig 兼容带 BOM 的文件
    try:
        text = raw_bytes.decode("utf-8-sig")
        data = json.loads(text)
    except UnicodeDecodeError as e:
        return {"ok": False, "messages": [], "meta": {}, "error": f"文件编码非 UTF-8: {e}"}
    except json.JSONDecodeError as e:
        return {"ok": False, "messages": [], "meta": {}, "error": f"JSON 解析失败: {e}"}

    # 兼容两种格式：带 meta 包装的 / 纯 messages 列表
    if isinstance(data, dict) and "messages" in data:
        messages = data["messages"]
        meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
    elif isinstance(data, list):
        messages = data
        meta = {}
    else:
        return {"ok": False, "messages": [], "meta": {}, "error": "未识别的 JSON 结构（既非 {meta, messages} 也非 list）"}

    # 【修正 #3a】meta 层面的版本与模式校验（宽松警告式，不阻断）
    meta_version = meta.get("version")
    if meta_version and meta_version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        return {"ok": False, "messages": [], "meta": meta,
                "error": f"Schema 主版本不兼容：文件={meta_version}, 当前={SCHEMA_VERSION}"}

    meta_mode = meta.get("mode")
    if meta_mode and meta_mode not in VALID_MODES:
        return {"ok": False, "messages": [], "meta": meta,
                "error": f"meta.mode 非法：{meta_mode!r}，合法取值: {VALID_MODES}"}

    # 【修正 #3b】messages 结构与内容校验
    if not isinstance(messages, list) or not messages:
        return {"ok": False, "messages": [], "meta": meta, "error": "messages 为空或非列表"}

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return {"ok": False, "messages": [], "meta": meta,
                    "error": f"第 {i} 条消息不是 dict"}

        role = msg.get("role")
        if role not in VALID_ROLES:
            return {"ok": False, "messages": [], "meta": meta,
                    "error": f"第 {i} 条消息 role={role!r} 非法，合法取值: {VALID_ROLES}"}

        # content 校验：允许 str / list / None（tool_call 型 assistant 消息可无 content）
        content = msg.get("content")
        has_tool_calls = bool(msg.get("tool_calls"))
        if content is None and not has_tool_calls and role != "assistant":
            return {"ok": False, "messages": [], "meta": meta,
                    "error": f"第 {i} 条消息 content 缺失且无 tool_calls"}
        if content is not None and not isinstance(content, (str, list)):
            return {"ok": False, "messages": [], "meta": meta,
                    "error": f"第 {i} 条消息 content 类型非法：{type(content).__name__}"}

        # tool 消息必须带 tool_call_id
        if role == "tool" and "tool_call_id" not in msg:
            return {"ok": False, "messages": [], "meta": meta,
                    "error": f"第 {i} 条 tool 消息缺少 tool_call_id"}

    return {"ok": True, "messages": messages, "meta": meta, "error": None}

# ==========================================
# 对外接口 3：归档目录扫描与读取
# ==========================================
def list_archives(archive_dir="archive"):
    """
    扫描归档目录，返回所有 JSON 文件信息（按修改时间倒序）

    :param archive_dir: 归档目录路径
    :return: list[dict] — [{"filename": str, "filepath": str, "mtime": float, "size_kb": int}, ...]
    """
    import os
    if not os.path.isdir(archive_dir):
        return []

    entries = []
    for fname in os.listdir(archive_dir):
        if not fname.lower().endswith(".json"):
            continue
        fpath = os.path.join(archive_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            stat = os.stat(fpath)
            entries.append({
                "filename": fname,
                "filepath": fpath,
                "mtime": stat.st_mtime,
                "size_kb": max(1, stat.st_size // 1024),
            })
        except OSError:
            continue

    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def load_archive(filepath):
    """
    读取指定归档文件并走标准校验流程

    :param filepath: JSON 文件完整路径
    :return: 与 validate_imported_messages 相同的返回结构
    """
    try:
        with open(filepath, "rb") as f:
            raw_bytes = f.read()
    except OSError as e:
        return {"ok": False, "messages": [], "meta": {}, "error": f"文件读取失败: {e}"}

    return validate_imported_messages(raw_bytes)