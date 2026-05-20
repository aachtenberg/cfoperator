"""CFOperator Agent Module

Core agent functionality including OODA loop, chat handling, and LLM integration.
"""

from .agent import CFOperator, OPENAI_COMPAT_PROVIDERS, main
from .knowledge_base import ResilientKnowledgeBase
from .llm_fallback import LLMFallbackManager
from .embedding_service import EmbeddingService

__all__ = [
    'CFOperator',
    'OPENAI_COMPAT_PROVIDERS',
    'ResilientKnowledgeBase',
    'LLMFallbackManager',
    'EmbeddingService',
    'main',
]
