"""The tool layer the planner calls. See friday/tools/base.py for the safety rules every call goes through."""

from __future__ import annotations

from typing import Any

from friday.tools.base import ApprovalDenied, Call, Tool, ToolError, ToolRegistry


def build_registry(ctx: Any, llm_factory: Any = None) -> ToolRegistry:
    from friday.tools import accounts, google_tools, memory_tools
    from friday.tools.files import FilesList, FilesRead, TableCompute
    from friday.tools.web import WebFetch, WebSearch

    reg = ToolRegistry(ctx)
    reg.register(*accounts.TOOLS, *google_tools.TOOLS, WebSearch(llm_factory), WebFetch(), FilesList(), FilesRead(),
                 TableCompute(), *memory_tools.TOOLS)  # fmt: skip
    return reg


__all__ = ["ApprovalDenied", "Call", "Tool", "ToolError", "ToolRegistry", "build_registry"]
