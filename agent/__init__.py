"""CFOperator Agent Module

Core agent functionality including OODA loop, chat handling, and LLM integration.
"""

from .agent import (CFOperator, OPENAI_COMPAT_PROVIDERS, EMPTY_RESPONSE_NUDGE,
                    EmptyLLMResponseError, main, normalize_model_id)
from .knowledge_base import ResilientKnowledgeBase
from .llm_fallback import LLMFallbackManager
from .embedding_service import EmbeddingService

__all__ = [
    'CFOperator',
    'OPENAI_COMPAT_PROVIDERS',
    'normalize_model_id',
    'ResilientKnowledgeBase',
    'LLMFallbackManager',
    'EmbeddingService',
    'main',
]
