"""
LLM Metrics Tracking Wrapper

Wraps LLM and embedding calls to track metrics without modifying original code.
"""

import time
import functools
from typing import Any, Dict, Callable


# Import metrics from agent
try:
    from agent import (
        LLM_REQUESTS, LLM_TOKENS, LLM_LATENCY, LLM_ERRORS,
        LLM_FALLBACKS, EMBEDDING_REQUESTS, EMBEDDING_CACHE_HITS
    )
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


def track_llm_request(provider: str, model: str):
    """Decorator to track LLM requests."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not METRICS_AVAILABLE:
                return func(*args, **kwargs)

            start_time = time.time()
            try:
                result = func(*args, **kwargs)

                # Track success
                LLM_REQUESTS.labels(provider=provider, model=model, result='success').inc()

                # Track latency
                latency = time.time() - start_time
                LLM_LATENCY.labels(provider=provider, model=model).observe(latency)

                # Track tokens if available in result
                if isinstance(result, dict):
                    usage = result.get('usage', {})
                    if 'prompt_tokens' in usage:
                        LLM_TOKENS.labels(provider=provider, model=model, type='prompt').inc(usage['prompt_tokens'])
                    if 'completion_tokens' in usage:
                        LLM_TOKENS.labels(provider=provider, model=model, type='completion').inc(usage['completion_tokens'])

                return result

            except Exception as e:
                # Track error
                LLM_REQUESTS.labels(provider=provider, model=model, result='error').inc()

                # Track error type
                error_type = type(e).__name__
                LLM_ERRORS.labels(provider=provider, error_type=error_type).inc()

                raise

        return wrapper
    return decorator


def track_llm_fallback(from_provider: str, to_provider: str):
    """Track LLM fallback events."""
    if METRICS_AVAILABLE:
        LLM_FALLBACKS.labels(from_provider=from_provider, to_provider=to_provider).inc()


def track_embedding_request(result: str):
    """Track embedding generation."""
    if METRICS_AVAILABLE:
        EMBEDDING_REQUESTS.labels(result=result).inc()


def track_embedding_cache(hit: bool):
    """Track embedding cache hits/misses."""
    if METRICS_AVAILABLE:
        result = 'hit' if hit else 'miss'
        EMBEDDING_CACHE_HITS.labels(result=result).inc()
