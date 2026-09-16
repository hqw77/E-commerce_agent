"""
轻量 Prompt Registry —— 选择、排序并记录真实加载的规则片段。

【这个文件是干什么的】
Prompt 片段注册表：把 system prompt 拆成多个可独立启用的「片段」（如路由契约、工具使用、
RAG 回答、售后边界等），按执行阶段和业务信号动态选择，再渲染成一条 system message。
被 router_client、answer_client、langchain_runtime 调用。

【核心设计】
  1. 片段化：不把整条大 prompt 写死，按需拼接，避免上下文浪费
  2. 受控选择：load 字段控制片段在什么信号下启用（always/when_route/when_final_answer/...）
  3. 不泄露：selection_summary 只返回片段元数据，不暴露完整 system prompt 内容
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


PROMPTS_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = PROMPTS_DIR / "prompt_registry.yml"


@dataclass(frozen=True)
class PromptFragment:
    """一个可按执行阶段和业务信号选择的稳定 Prompt 片段。"""
    name: str
    level: str
    version: str
    load: str          # 加载条件：always / when_route / when_final_answer / when_rag 等
    priority: int      # 优先级（只控制排序，不代表置信度）
    status: str = "active"
    description: str = ""


class PromptManager:
    """管理稳定规则片段；RAG、Tool 和 Runtime Context 仍由各自模块提供事实。"""

    def __init__(self, prompts_dir: Path = PROMPTS_DIR) -> None:
        self.prompts_dir = prompts_dir.resolve()
        self._registry = self._load_registry()

    @lru_cache(maxsize=None)
    def load(self, name: str) -> str:
        """只允许读取注册目录中的 Markdown，避免自由文件名跳出 Prompt 边界。"""
        path = (self.prompts_dir / f"{name}.md").resolve()
        if path.parent != self.prompts_dir:  # 防路径穿越
            raise ValueError(f"Invalid prompt name: {name}")
        return path.read_text(encoding="utf-8").strip()

    def select_fragments(self, signals: dict[str, Any]) -> list[PromptFragment]:
        """按受控信号选择片段；优先级只控制顺序，不代表模型置信度。"""
        selected = [
            fragment
            for fragment in self._registry
            if fragment.status == "active" and self._matches(fragment.load, signals)
        ]
        return sorted(selected, key=lambda fragment: (-fragment.priority, fragment.name))

    def render_system_prompt(self, fragments: list[PromptFragment]) -> str:
        """把已选择片段渲染成一条 system message，不混入动态业务事实。"""
        return "\n\n".join(
            f"[{fragment.name} | {fragment.level} | {fragment.version} | priority={fragment.priority}]\n"
            f"{self.load(fragment.name)}"
            for fragment in fragments
        )

    def selection_summary(self, fragments: list[PromptFragment], *, phase: str) -> list[dict[str, Any]]:
        """返回公开元数据，不暴露完整 system prompt。"""
        return [
            {
                "name": fragment.name,
                "level": fragment.level,
                "version": fragment.version,
                "load": fragment.load,
                "priority": fragment.priority,
                "phase": phase,
                "estimated_tokens": max(1, len(self.load(fragment.name)) // 4),
            }
            for fragment in fragments
        ]

    def _load_registry(self) -> list[PromptFragment]:
        """从 prompt_registry.yml 读取片段清单。"""
        payload = yaml.safe_load((self.prompts_dir / "prompt_registry.yml").read_text(encoding="utf-8")) or {}
        return [
            PromptFragment(
                name=str(item["name"]),
                level=str(item.get("level", "capability")),
                version=str(item.get("version", "v1")),
                load=str(item.get("load", "always")),
                priority=int(item.get("priority", 50)),
                status=str(item.get("status", "active")),
                description=str(item.get("description", "")),
            )
            for item in payload.get("prompts", [])
        ]

    @staticmethod
    def _matches(load: str, signals: dict[str, Any]) -> bool:
        """判断片段是否匹配当前信号（load 条件）。"""
        if load == "always":
            return True
        if load == "when_route":
            return signals.get("phase") == "route"
        if load == "when_final_answer":
            return signals.get("phase") == "final_answer"
        if load == "when_business_tool":
            return signals.get("phase") == "final_answer" and bool(signals.get("needs_business_tools"))
        if load == "when_rag":
            return signals.get("phase") == "final_answer" and bool(signals.get("needs_rag"))
        if load == "when_high_risk_after_sale":
            return signals.get("phase") == "final_answer" and bool(signals.get("high_risk_after_sale"))
        return False


prompt_manager = PromptManager()


def load_prompt(name: str) -> str:
    """兼容测试和少量直接读取场景；新执行链路统一使用 PromptManager。"""
    return prompt_manager.load(name.removesuffix(".md"))
