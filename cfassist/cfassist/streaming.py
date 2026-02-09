"""Tool-calling loop orchestration — bridges client, tools, and display."""

import json


def run_conversation(client, tools, display, messages, system_prompt, max_iterations=10):
    """Run a conversation turn with tool-calling loop.

    Uses non-streaming for all LLM calls (reliable tool call parsing with Ollama).
    Final response rendered with Rich Markdown for polished output.

    Returns:
        dict with keys: response (str), tool_calls (int),
        input_tokens (int), output_tokens (int)
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    tool_schemas = tools.get_schemas() if tools else None
    tool_calls_count = 0
    total_input_tokens = 0
    total_output_tokens = 0

    for iteration in range(max_iterations):
        display.show_thinking()

        try:
            response = client.chat(full_messages, tool_schemas)
        except Exception as e:
            display.clear_thinking()
            display.show_error(
                f"LLM request failed: {e}",
                hint=f"Check connection: curl {client.url}/api/tags",
            )
            return {
                "response": "",
                "tool_calls": tool_calls_count,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "error": str(e),
            }

        display.clear_thinking()
        total_input_tokens += response.get("input_tokens", 0)
        total_output_tokens += response.get("output_tokens", 0)

        # Handle tool calls
        if response.get("tool_calls"):
            tool_call = response["tool_calls"][0]
            func = tool_call.get("function", {})
            tool_name = func.get("name", "unknown")
            tool_args = func.get("arguments", {})

            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except json.JSONDecodeError:
                    tool_args = {"raw": tool_args}

            display.show_tool_call(tool_name, tool_args)
            result = tools.execute(tool_name, tool_args)
            display.show_tool_result(tool_name, result)
            tool_calls_count += 1

            assistant_msg = {"role": "assistant", "tool_calls": response["tool_calls"]}
            if response.get("content"):
                assistant_msg["content"] = response["content"]
            full_messages.append(assistant_msg)

            full_messages.append({
                "role": "tool",
                "content": json.dumps(result, default=str),
            })
            continue

        # No tool calls — show the response
        text = response.get("content", "")
        if text:
            display.show_response(text)

        return {
            "response": text,
            "tool_calls": tool_calls_count,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }

    # Max iterations reached
    display.show_warning(f"Reached maximum tool iterations ({max_iterations}).")
    return {
        "response": "",
        "tool_calls": tool_calls_count,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }
