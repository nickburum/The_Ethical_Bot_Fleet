"""Claude backend. Needs the official SDK: pip install "ethical-bot-fleet[claude]"."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict

DEFAULT_MODEL = "claude-opus-5-5"
# Models that can hand a declined request to another model inside the same call.
_FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


INSTALL = 'pip install "ethical-bot-fleet[claude] @ git+https://github.com/nickburum/The_Ethical_Bot_Fleet"'
NO_CREDENTIALS = "Claude needs an API key in the ANTHROPIC_API_KEY environment variable (or run `ant auth login`)."


def preflight() -> None:
    """Raise a clear error now if the SDK is missing, before any page is read."""
    from .llm import LLMError

    try:
        import anthropic  # noqa: F401
    except ImportError:
        raise LLMError("Claude needs the Anthropic SDK. Install it with: " + INSTALL) from None


def run(model: str, system: str, question: str, tool: Dict[str, Any],
        call_tool: Callable[[str, Dict[str, Any]], str], max_steps: int) -> str:
    from .llm import LLMError

    preflight()
    import anthropic

    client = anthropic.Anthropic()  # credentials come from the environment, never from this library
    extra: Dict[str, Any] = {}
    if model in _FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    messages = [{"role": "user", "content": question}]
    tools = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}]

    for _ in range(max_steps):
        try:
            response = client.beta.messages.create(
                model=model, max_tokens=16000, system=system, tools=tools, messages=messages, **extra
            )
        except anthropic.AuthenticationError:
            raise LLMError("Claude rejected the credentials. " + NO_CREDENTIALS)
        except anthropic.NotFoundError:
            raise LLMError("Claude does not know the model %r." % model)
        except anthropic.RateLimitError:
            raise LLMError("Claude is rate limiting this key. Try again shortly.")
        except anthropic.APIStatusError as error:
            message = str(error.message)
            secret = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            if secret:
                message = message.replace(secret, "[key hidden]")
            raise LLMError("Claude returned HTTP %d: %s" % (error.status_code, message[:400]))
        except anthropic.APIConnectionError as error:
            raise LLMError("Could not reach Claude: %s" % error)
        except anthropic.AnthropicError as error:
            raise LLMError("Claude is not set up: %s" % error)
        except TypeError as error:  # the SDK raises this when it finds no credentials at all
            if "authentication" not in str(error).lower():
                raise
            raise LLMError(NO_CREDENTIALS) from None

        text = "\n".join(block.text for block in response.content if block.type == "text").strip()
        if response.stop_reason == "refusal":
            return text or "The model declined to answer this request."
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            if response.stop_reason == "max_tokens":
                text += "\n\n[The answer was cut off at the model's output limit.]"
            if not text:
                raise LLMError("The model returned an empty answer.")
            return text

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": call_tool(block.name, dict(block.input)),
                })
        messages.append({"role": "user", "content": results})
    raise LLMError("The model was still asking for pages after %d steps; stopping." % max_steps)
