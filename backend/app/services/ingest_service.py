"""结构化抽取服务：用 LLM 从零散资料中抽取结构化知识条目"""
import json
from typing import List, Optional
from langchain_core.messages import HumanMessage, SystemMessage
from app.models import User
from app.services.llm_service import get_llm_for_user

EXTRACT_PROMPT = """你是一个知识库结构化抽取助手。请从用户提供的原始资料（可能包含文字和图片）中，抽取结构化的知识条目。

每个条目包含以下字段：
1. title：标题（简洁概括，不超过50字）
2. steps：操作步骤（数组，每个元素是一个步骤描述，如有步骤则提取，无则为空数组）
3. system_or_material：涉及的系统或物料（如"招聘系统"、"offer邮件"等，无则为空字符串）
4. caution：注意事项（需要特别注意的点，无则为空字符串）
5. related_flow：关联流程（如"校招流程-面试环节"，无则为空字符串）

要求：
1. 从资料中提取所有可独立成条的知识点，每条一个 JSON 对象
2. 如果资料只包含一个知识点，返回一个元素的数组
3. 如果资料包含多个知识点，返回多个元素的数组
4. 如果资料中包含图片，请仔细阅读图片中的文字和图表信息，并结合文字内容一起抽取
5. 严格输出 JSON 数组格式，不要添加任何其他文字
6. 输出示例格式：[{"title": "...", "steps": ["步骤1", "步骤2"], "system_or_material": "...", "caution": "...", "related_flow": "..."}]

原始资料文字内容：
__RAW_TEXT__
"""


def _parse_llm_response(content: str, raw_text: str) -> List[dict]:
    """解析 LLM 返回的 JSON，失败时返回原始文本作为单条目"""
    content = content.strip()
    # 清理可能的 markdown 代码块标记
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        items = json.loads(content)
    except json.JSONDecodeError:
        return [{"title": raw_text[:50], "steps": [], "system_or_material": "", "caution": "", "related_flow": ""}]

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return [{"title": raw_text[:50], "steps": [], "system_or_material": "", "caution": "", "related_flow": ""}]

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append({
            "title": str(item.get("title", ""))[:200],
            "steps": item.get("steps", []) or [],
            "system_or_material": str(item.get("system_or_material", "") or ""),
            "caution": str(item.get("caution", "") or ""),
            "related_flow": str(item.get("related_flow", "") or ""),
        })

    return result if result else [{"title": raw_text[:50], "steps": [], "system_or_material": "", "caution": "", "related_flow": ""}]


def extract_from_text(user: User, raw_text: str) -> List[dict]:
    """用 LLM 从纯文本中抽取结构化条目（无图片）"""
    llm = get_llm_for_user(user)
    prompt = EXTRACT_PROMPT.replace("__RAW_TEXT__", raw_text)
    messages = [
        SystemMessage(content="你是知识库结构化抽取助手，只输出 JSON 数组，不输出任何其他文字。"),
        HumanMessage(content=prompt),
    ]
    response = llm.invoke(messages)
    return _parse_llm_response(response.content, raw_text)


def extract_from_document(user: User, raw_text: str, images: Optional[List[dict]] = None) -> List[dict]:
    """用 LLM 从文档中抽取结构化条目（支持图片）

    Args:
        user: 用户
        raw_text: 文档的 Markdown 文本内容
        images: 图片列表，每个元素为 {"data": base64字符串, "type": "png"/"jpg" 等}
                为空时退化为纯文本抽取
    """
    if not images:
        return extract_from_text(user, raw_text)

    llm = get_llm_for_user(user)
    prompt = EXTRACT_PROMPT.replace("__RAW_TEXT__", raw_text)

    # 构造多模态消息：文本 + 多张图片
    content_parts = [{"type": "text", "text": prompt}]
    for img in images:
        data_url = f"data:image/{img['type']};base64,{img['data']}"
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })

    messages = [
        SystemMessage(content="你是知识库结构化抽取助手，只输出 JSON 数组，不输出任何其他文字。请仔细阅读提供的图片内容。"),
        HumanMessage(content=content_parts),
    ]
    response = llm.invoke(messages)
    return _parse_llm_response(response.content, raw_text)
