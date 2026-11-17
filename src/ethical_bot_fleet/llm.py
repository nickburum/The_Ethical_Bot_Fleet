"""Connect a model to the fleet: presets for cloud and local models, and ``ask``.

Keys are read from environment variables at the moment of the call. They are
never written to disk, logged, or sent anywhere except the provider you chose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .fleet import Blocked, Bot, FetchError, FleetError
from .text import readable

PAGE_LIMIT = 30_000  # characters of each page handed to the model

SYSTEM = (
    "You answer questions using public web pages. Pages the user supplied are included in the "
    "message. Use the fetch_page tool to read any other page you need. If a page cannot be "
    "fetched the tool says why: respect that, do not retry it, and say so in your answer. "
    "Base your answer on what the pages say and name the URLs you used."
)

TOOL = {
    "name": "fetch_page",
    "description": (
        "Fetch one public web page and return its text. Call this when you need a page that is "
        "not already in the conversation. Returns the page text, or the reason it may not be fetched."
    ),
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "A full http or https URL"}},
        "required": ["url"],
    },
}


class LLMError(FleetError):
    """The model could not be reached or did not answer."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Preset:
    label: str
    kind: str  # "claude" or "openai" (the OpenAI-compatible chat API most servers speak)
    base_url: str = ""
    key_env: str = ""
    default_model: str = ""
    local: bool = False


PRESETS: Dict[str, Preset] = {
    # Cloud
    "claude": Preset("Anthropic Claude", "claude", key_env="ANTHROPIC_API_KEY", default_model="claude-opus-5-5"),
    "openai": Preset("OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "gemini": Preset("Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "mistral": Preset("Mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "groq": Preset("Groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "deepseek": Preset("DeepSeek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "xai": Preset("xAI Grok", "openai", "https://api.x.ai/v1", "XAI_API_KEY"),
    "openrouter": Preset("OpenRouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "together": Preset("Together AI", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    # Local: nothing leaves your machine, no key needed
    "ollama": Preset("Ollama", "openai", "http://localhost:11434/v1", local=True),
    "lmstudio": Preset("LM Studio", "openai", "http://localhost:1234/v1", local=True),
    "llamacpp": Preset("llama.cpp server", "openai", "http://localhost:8080/v1", local=True),
    "vllm": Preset("vLLM", "openai", "http://localhost:8000/v1", local=True),
    "jan": Preset("Jan", "openai", "http://localhost:1337/v1", local=True),
    # Anything else that speaks the OpenAI-compatible API
    "custom": Preset("Any OpenAI-compatible server", "openai", key_env="EBF_API_KEY"),
}


def resolve(spec: Optional[str], base_url: Optional[str] = None) -> Tuple[Preset, str, str]:
    """Turn ``"ollama"`` or ``"openai:some-model"`` into (preset, model, base_url)."""
    spec = spec or os.environ.get("EBF_MODEL", "")
    if not spec:
        raise LLMError(
            "No model chosen. Use --model NAME or set EBF_MODEL. Choices: %s. "
            "Run `ebf models` to see which are ready." % ", ".join(PRESETS)
        )
    name, _, model = spec.partition(":")
    preset = PRESETS.get(name.lower())
    if preset is None:
        raise LLMError("Unknown model preset %r. Choices: %s." % (name, ", ".join(PRESETS)))
    base = (base_url or os.environ.get("EBF_BASE_URL") or preset.base_url).rstrip("/")
    if preset.kind == "openai" and not base:
        raise LLMError("The custom preset needs a server address: --base-url http://host:port/v1 (or EBF_BASE_URL).")
    return preset, model or preset.default_model, base


def _http(url: str, payload: Optional[Dict[str, Any]], key: str, timeout: float) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": "ethical-bot-fleet"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as handle:
            return json.loads(handle.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", "replace")[:400]
        raise LLMError("The model server returned HTTP %d: %s" % (error.code, detail), error.code)
    except (OSError, ValueError) as error:
        raise LLMError("Could not reach the model server at %s (%s)" % (url, error))


def list_models(base_url: str, key: str = "", timeout: float = 5.0) -> List[str]:
    data = _http(base_url + "/models", None, key, timeout)
    return [item["id"] for item in data.get("data", []) if isinstance(item, dict) and item.get("id")]


def _openai_run(base: str, key: str, model: str, system: str, question: str,
                call_tool: Callable[[str, Dict[str, Any]], str], max_steps: int) -> str:
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    tools = [{"type": "function", "function": TOOL}]
    use_tools = True
    for step in range(max_steps):
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if use_tools:
            payload["tools"] = tools
        try:
            data = _http(base + "/chat/completions", payload, key, 600.0)
        except LLMError as error:
            if use_tools and step == 0 and error.status in (400, 404, 422, 501):
                use_tools = False  # some local models cannot call tools; carry on with the supplied pages
                try:
                    data = _http(base + "/chat/completions", {"model": model, "messages": messages}, key, 600.0)
                except LLMError:
                    raise error
            else:
                raise
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("The model server sent a reply this tool does not understand.")
        calls = message.get("tool_calls") or []
        if not calls:
            return (message.get("content") or "").strip()
        messages.append(message)
        for call in calls:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                result = call_tool(function.get("name", ""), arguments if isinstance(arguments, dict) else {})
            except ValueError:
                result = "The tool arguments were not valid JSON."
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    raise LLMError("The model was still asking for pages after %d steps; stopping." % max_steps)


def fetch_text(bot: Bot, url: str, limit: int = PAGE_LIMIT) -> str:
    """What the model sees for one page: its text, or a plain reason it was not fetched."""
    try:
        response = bot.get(url)
    except Blocked as no:
        return "Not fetched: %s Do not retry this URL." % no.decision.detail
    except FetchError as error:
        return "The request failed: %s" % error
    if not response.ok:
        return "The server answered HTTP %d for %s." % (response.status, url)
    text = readable(response)
    if len(text) > limit:
        text = text[:limit] + "\n\n[Page truncated: showing the first %d of %d characters.]" % (limit, len(text))
    return text


def ask(bot: Bot, question: str, urls: Iterable[str] = (), model: Optional[str] = None,
        base_url: Optional[str] = None, max_steps: int = 8,
        on_event: Optional[Callable[[str], None]] = None) -> str:
    """Ask a model a question about the web. Every page it reads goes through the fleet."""
    preset, model_name, base = resolve(model, base_url)
    notify = on_event or (lambda message: None)
    key = os.environ.get(preset.key_env, "") if preset.key_env else ""

    if preset.kind == "openai":
        if not model_name and (preset.local or not preset.base_url):
            available = list_models(base, key)
            if not available:
                raise LLMError("The server at %s is running but has no models loaded yet." % base)
            model_name = available[0]
        if not model_name:
            raise LLMError("Name a model for %s, for example: --model %s:MODEL_NAME" % (preset.label, model or "custom"))
        if preset.key_env and not key and not preset.local and preset.base_url:
            raise LLMError("%s needs an API key in the %s environment variable." % (preset.label, preset.key_env))

    def call_tool(name: str, arguments: Dict[str, Any]) -> str:
        url = arguments.get("url")
        if name != "fetch_page" or not isinstance(url, str):
            return "Unknown tool call. The only tool is fetch_page(url)."
        notify("reading " + url)
        return fetch_text(bot, url)

    parts = [question]
    for url in urls:
        notify("reading " + url)
        parts.append("Page %s:\n%s" % (url, fetch_text(bot, url)))
    prompt = "\n\n".join(parts)
    notify("asking %s (%s)" % (preset.label, model_name))

    if preset.kind == "claude":
        from . import claude

        return claude.run(model_name, SYSTEM, prompt, TOOL, call_tool, max_steps)
    return _openai_run(base, key, model_name, SYSTEM, prompt, call_tool, max_steps)


def status() -> List[Tuple[str, Preset, bool, str]]:
    """For ``ebf models``: (name, preset, ready, note) for every preset."""
    rows = []
    for name, preset in PRESETS.items():
        if preset.local:
            try:
                found = list_models(preset.base_url, timeout=0.5)
                rows.append((name, preset, True, "running, %d model(s): %s" % (len(found), ", ".join(found[:3]) or "none loaded")))
            except LLMError:
                rows.append((name, preset, False, "not running at %s" % preset.base_url))
        elif name == "custom":
            base = os.environ.get("EBF_BASE_URL", "")
            rows.append((name, preset, bool(base), "EBF_BASE_URL=%s" % base if base else "set EBF_BASE_URL or pass --base-url"))
        else:
            ready = bool(os.environ.get(preset.key_env))
            rows.append((name, preset, ready, "%s is %s" % (preset.key_env, "set" if ready else "not set")))
    return rows
