"""LLM API client — Ollama and OpenAI-compatible providers."""

import json

import httpx


class LLMClient:
    """Synchronous streaming LLM client for Ollama and OpenAI-compatible APIs."""

    def __init__(self, config):
        llm = config["llm"]
        self.provider = llm["provider"]
        self.url = llm["url"].rstrip("/")
        self.model = llm["model"]
        self.temperature = llm.get("temperature", 0.7)
        self.api_key = llm.get("api_key")
        self.http = httpx.Client(timeout=120.0)

    def close(self):
        self.http.close()

    def chat(self, messages, tools=None):
        """Non-streaming chat. Returns a single response dict.

        Used during tool-calling iterations where we need the full response.
        """
        if self.provider == "ollama":
            return self._ollama_chat(messages, tools)
        else:
            return self._openai_chat(messages, tools)

    def chat_stream(self, messages, tools=None):
        """Yield response chunks from the LLM.

        Each chunk is a dict with optional keys:
            content: str — text token
            tool_calls: list — tool call requests
            done: bool — stream finished
            input_tokens: int — prompt token count (final chunk)
            output_tokens: int — completion token count (final chunk)
        """
        if self.provider == "ollama":
            yield from self._ollama_stream(messages, tools)
        else:
            yield from self._openai_stream(messages, tools)

    def _ollama_chat(self, messages, tools=None):
        """Non-streaming Ollama request. Returns full response dict."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools

        response = self.http.post(f"{self.url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        msg = data.get("message", {})

        return {
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls"),
            "role": msg.get("role", "assistant"),
            "input_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
        }

    def _openai_chat(self, messages, tools=None):
        """Non-streaming OpenAI-compatible request."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools

        response = self.http.post(
            f"{self.url}/v1/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()
        data = response.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})

        tool_calls = None
        if msg.get("tool_calls"):
            tool_calls = [
                {
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": json.loads(tc["function"]["arguments"]),
                    }
                }
                for tc in msg["tool_calls"]
            ]

        return {
            "content": msg.get("content", ""),
            "tool_calls": tool_calls,
            "role": msg.get("role", "assistant"),
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

    def _ollama_stream(self, messages, tools=None):
        """Stream from Ollama's /api/chat endpoint."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools

        with self.http.stream(
            "POST", f"{self.url}/api/chat", json=payload
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                msg = chunk.get("message", {})

                result = {
                    "content": msg.get("content", ""),
                    "tool_calls": msg.get("tool_calls"),
                    "done": chunk.get("done", False),
                }

                # Token counts come on the final chunk
                if chunk.get("done"):
                    result["input_tokens"] = chunk.get("prompt_eval_count", 0)
                    result["output_tokens"] = chunk.get("eval_count", 0)

                yield result

    def _openai_stream(self, messages, tools=None):
        """Stream from OpenAI-compatible /v1/chat/completions endpoint."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools

        # Accumulate tool call fragments across streamed deltas
        tool_call_accum = {}

        with self.http.stream(
            "POST",
            f"{self.url}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    # Yield accumulated tool calls if any
                    if tool_call_accum:
                        yield {
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": json.loads(tc["arguments"]),
                                    }
                                }
                                for tc in tool_call_accum.values()
                            ],
                            "done": True,
                        }
                    else:
                        yield {"content": "", "done": True}
                    return

                chunk = json.loads(data)
                delta = chunk.get("choices", [{}])[0].get("delta", {})

                # Accumulate tool call deltas
                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_call_accum:
                            tool_call_accum[idx] = {
                                "name": "",
                                "arguments": "",
                            }
                        if "function" in tc:
                            if "name" in tc["function"]:
                                tool_call_accum[idx]["name"] = tc["function"]["name"]
                            if "arguments" in tc["function"]:
                                tool_call_accum[idx]["arguments"] += tc["function"]["arguments"]
                    continue

                content = delta.get("content", "")
                if content:
                    yield {"content": content, "done": False}

    def check_connection(self):
        """Test if the LLM endpoint is reachable. Returns (ok, error_message)."""
        try:
            if self.provider == "ollama":
                resp = self.http.get(f"{self.url}/api/tags", timeout=5.0)
                resp.raise_for_status()
                return True, None
            else:
                # OpenAI-compatible: try models endpoint
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                resp = self.http.get(
                    f"{self.url}/v1/models", headers=headers, timeout=5.0
                )
                resp.raise_for_status()
                return True, None
        except httpx.ConnectError:
            return False, f"Connection refused: {self.url}"
        except httpx.TimeoutException:
            return False, f"Timeout connecting to: {self.url}"
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code} from {self.url}"
        except Exception as e:
            return False, str(e)
