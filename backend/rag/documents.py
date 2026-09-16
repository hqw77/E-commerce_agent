"""
读取真实知识文件，并转换成公开 Citation 契约。
"""

from __future__ import annotations  # 延迟类型注解评估，支持Python 3.7+的兼容性

from functools import lru_cache  # LRU缓存装饰器，缓存函数调用结果
from pathlib import Path  # 面向对象的文件路径操作
from typing import Any  # 任意类型，用于宽松的类型注解

import yaml  # YAML格式解析库

from api.schemas import Citation  # 从API模块导入Citation数据契约类


# 知识库根目录：当前文件所在目录的父级目录下的"knowledge"文件夹
KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"


@lru_cache(maxsize=None)  # 无限大小LRU缓存，相同name直接返回缓存结果
def load_knowledge_citation(name: str) -> Citation:
    """
    按 YAML frontmatter 解析知识文件，让 citation 指向真实可替换材料。

    参数:
        name: 知识文件名（相对于knowledge目录）

    返回:
        Citation: 包含解析后的元数据和内容片段的数据契约对象

    异常:
        ValueError: 当路径非法、文件格式不正确或缺少必需的frontmatter时抛出
    """
    # 构建文件的绝对路径，并确保路径解析后的父目录确实是knowledge目录（防止路径遍历攻击）
    path = (KNOWLEDGE_DIR / name).resolve()
    if path.parent != KNOWLEDGE_DIR:
        raise ValueError(f"Invalid knowledge name: {name}")

    # 读取文件全部内容，UTF-8编码
    raw = path.read_text(encoding="utf-8")

    # 验证文件必须以"---\n"开头（YAML frontmatter起始标记）
    if not raw.startswith("---\n"):
        raise ValueError(f"Knowledge file must start with YAML frontmatter: {path}")

    # 去除起始的"---\n"，然后按"\n---\n"分割：
    # frontmatter部分、分隔符、正文部分
    frontmatter, separator, body = raw[4:].partition("\n---\n")
    if not separator:  # 如果没有找到结束分隔符，说明frontmatter不完整
        raise ValueError(f"Knowledge file has incomplete YAML frontmatter: {path}")

    # 使用yaml.safe_load安全解析frontmatter为字典，若为空则使用空字典
    metadata: dict[str, Any] = yaml.safe_load(frontmatter) or {}

    # 构造并返回Citation数据契约对象
    return Citation(
        source=f"knowledge/{name}",  # 来源标识：knowledge/文件名
        title=str(metadata["title"]),  # 标题（必须存在）
        snippet=body.strip(),  # 正文内容，去除首尾空白
        score=float(metadata["score"]),  # 相关性分数（必须存在，转为浮点数）
        retrieval_stage=metadata.get("retrieval_stage"),  # 检索阶段（可选）
        metadata={  # 额外元数据
            "policy_id": str(metadata["policy_id"]),  # 策略ID（必须存在）
            "scene_key": str(metadata["scene_key"]),  # 场景键值（必须存在）
        },
    )
