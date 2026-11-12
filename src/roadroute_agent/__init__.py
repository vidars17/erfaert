"""A hand-written agent loop around the roadroute tools.

Kept as a separate package on purpose: ``roadroute`` is a plain routing library with
no LLM dependency, and this package is the one place that knows about the Claude API.
"""
from .config import AgentConfig
from .loop import Agent, AgentResult, ToolCall
from .usage import Usage

__all__ = ["Agent", "AgentConfig", "AgentResult", "ToolCall", "Usage"]
