"""Connect a model to the fleet: presets for cloud and local models, and ``ask``.

Keys are read from environment variables at the moment of the call. They are
never written to disk, logged, or sent anywhere except the provider you chose.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .fleet import Blocked, Bot, FetchError, FleetError
from .text import readable

PAGE_LIMIT = 30_000  # characters of each page handed to the model

SYSTEM = (
    "You answer questions using public web pages. Pages the user supplied are included in the "
    "message. Use the fetch_page tool to read any other page you need. If a page cannot be "
    "fetched the tool says why: respect that, do not retry it, and say so in your answer. "
    "Base your answer on what the pages say and name the URLs you used. Never describe a page "
    "whose text is not in this conversation; if you have not read it, say so."
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
    # EBF_BASE_URL only moves local and custom servers; a cloud preset moves only with an explicit --base-url.
    env_base = "" if (preset.base_url and not preset.local) else os.environ.get("EBF_BASE_URL", "")
    base = (base_url or env_base or preset.base_url).rstrip("/")
    if base and "://" not in base:
        base = "http://" + base
    if preset.kind == "openai" and not base:
        raise LLMError("The custom preset needs a server address: --base-url http://host:port/v1 (or EBF_BASE_URL).")
    return preset, model or preset.default_model, base


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A model server that redirects would carry the key and the question to a host nobody chose.
    def redirect_request(self, *args, **kwargs):
        return None


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _opener(url: str) -> urllib.request.OpenerDirector:
    handlers: List[Any] = [_NoRedirect]
    if _is_loopback(urlsplit(url).hostname or ""):
        handlers.append(urllib.request.ProxyHandler({}))  # a model on this machine is never reached through a proxy
    return urllib.request.build_opener(*handlers)


def _show(url: str) -> str:
    """The address without any user:password part, for messages."""
    parts = urlsplit(url)
    return url.replace(parts.netloc, parts.netloc.rpartition("@")[2], 1) if "@" in parts.netloc else url


def _http(url: str, payload: Optional[Dict[str, Any]], key: str, timeout: float) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": "ethical-bot-fleet"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with _opener(url).open(request, timeout=timeout) as handle:
            body = handle.read()
    except urllib.error.HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", "replace")[:400]
        if key:
            detail = detail.replace(key, "[key hidden]")
        if 300 <= error.code < 400:
            raise LLMError("The model server at %s tried to redirect the request elsewhere; refusing to follow. "
                           "Use its final address." % _show(url), error.code)
        hint = " Check that the address ends in /v1." if error.code == 404 and "/v1" not in url else ""
        raise LLMError("The model server returned HTTP %d: %s%s" % (error.code, detail, hint), error.code)
    except (OSError, ValueError) as error:
        raise LLMError("Could not reach the model server at %s (%s)" % (_show(url), error))
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise LLMError("The model server at %s answered, but not with JSON. Is this the right address?" % _show(url))


def list_models(base_url: str, key: str = "", timeout: float = 5.0) -> List[str]:
    data = _http(base_url + "/models", None, key, timeout)
    items = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise LLMError("The server at %s does not look like a model server." % _show(base_url))
    return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]


_TEXT_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.S)
_URL = re.compile(r"https?://[^\s<>\"'`\])}]+")


def _text_calls(content: str) -> List[Dict[str, Any]]:
    """Tool calls that a local model wrote out as text instead of sending them properly."""
    found = []
    for block in _TEXT_CALL.findall(content):
        try:
            call = json.loads(block)
        except ValueError:
            # Small models often get the brackets wrong; the intent is still plain to see.
            address = _URL.search(block)
            if "fetch_page" in block and address:
                found.append({"name": "fetch_page", "arguments": {"url": address.group(0).rstrip('.,;:!?\\')}})
            continue
        if isinstance(call, dict) and call.get("name"):
            arguments = call.get("arguments", call.get("parameters", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            found.append({"name": str(call["name"]), "arguments": arguments if isinstance(arguments, dict) else {}})
    return found


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
        if not isinstance(message, dict):
            raise LLMError("The model server sent a reply this tool does not understand.")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            calls = []
        if not calls:
            content = message.get("content")
            if isinstance(content, list):  # some servers send the answer as a list of text parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            answer = content.strip() if isinstance(content, str) else ""
            written = _text_calls(answer) if use_tools else []
            if written:  # many local models ask for a tool this way; run it and hand the result back
                messages.append({"role": "assistant", "content": answer})
                results = ["Result of %s(%s):\n%s" % (
                    call["name"], call["arguments"].get("url", ""), call_tool(call["name"], call["arguments"]),
                ) for call in written]
                messages.append({"role": "user", "content": "\n\n".join(results) + "\n\nNow answer the question."})
                continue
            if "<tool_call>" in answer:
                raise LLMError("The model tried to use the page-reading tool in a form that could not be understood. "
                               "Put the page address in your question or on the command line instead.")
            if not answer:
                raise LLMError("The model returned an empty answer.")
            return answer
        messages.append(message)
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                function = {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            if isinstance(arguments, dict):
                result = call_tool(str(function.get("name", "")), arguments)
            else:
                result = "The tool arguments were not valid JSON."
            call_id = call.get("id", "") if isinstance(call, dict) else ""
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
    raise LLMError("The model was still asking for pages after %d steps; stopping." % max_steps)


def fetch_text(bot: Bot, url: str, limit: int = PAGE_LIMIT) -> str:
    """What the model sees for one page: its text, or a plain reason it was not fetched."""
    try:
        response = bot.get(url)
    except Blocked as no:
        return "Not fetched: %s. Do not retry this URL." % no.decision.detail.rstrip(".")
    except FetchError as error:
        return "The request failed: %s" % error
    if not response.ok:
        return "The server answered HTTP %d for %s." % (response.status, url)
    text = readable(response)
    cut = " The download itself was also cut off at %d bytes." % len(response.body) if response.truncated else ""
    if len(text) > limit:
        text = text[:limit] + "\n\n[Page truncated: showing the first %d of %d characters.%s]" % (limit, len(text), cut)
    elif cut:
        text += "\n\n[Page truncated.%s]" % cut
    return text


def ask(bot: Bot, question: str, urls: Iterable[str] = (), model: Optional[str] = None,
        base_url: Optional[str] = None, max_steps: int = 8,
        on_event: Optional[Callable[[str], None]] = None) -> str:
    """Ask a model a question about the web. Every page it reads goes through the fleet."""
    preset, model_name, base = resolve(model, base_url)
    if preset.kind == "claude" and base_url:
        raise LLMError("The claude preset does not take --base-url. To use a gateway, set ANTHROPIC_BASE_URL instead.")
    notify = on_event or (lambda message: None)
    # A provider's key goes only to that provider's own address. Anywhere else gets EBF_API_KEY, if set.
    if preset.key_env and (not preset.base_url or base == preset.base_url.rstrip("/")):
        key = os.environ.get(preset.key_env, "")
    else:
        key = os.environ.get("EBF_API_KEY", "")
    moved = bool(preset.base_url) and base != preset.base_url.rstrip("/")

    if preset.kind == "openai":
        if not model_name and (preset.local or not preset.base_url):
            available = list_models(base, key)
            if not available:
                raise LLMError("The server at %s is running but has no models loaded yet." % base)
            model_name = available[0]
        if not model_name:
            raise LLMError("Name a model for %s, for example: --model %s:MODEL_NAME" % (preset.label, model or "custom"))
        if preset.key_env and not key and not preset.local and preset.base_url and not moved:
            raise LLMError("%s needs an API key in the %s environment variable." % (preset.label, preset.key_env))

    def call_tool(name: str, arguments: Dict[str, Any]) -> str:
        url = arguments.get("url")
        if name != "fetch_page" or not isinstance(url, str):
            return "Unknown tool call. The only tool is fetch_page(url)."
        return read(url)

    reads: List[str] = []

    def read(url: str) -> str:
        reads.append(url)
        notify("reading " + url)
        text = fetch_text(bot, url)
        if text.startswith(("Not fetched:", "The request failed:", "The server answered")):
            notify("  " + text.replace(" Do not retry this URL.", ""))
        return text

    if preset.kind == "claude":
        from . import claude

        claude.preflight()  # fail before reading any page if Claude cannot run

    # Pages named in the question are read up front too, so the answer never depends on the model
    # choosing to use the tool.
    wanted = list(dict.fromkeys(list(urls) + [found.rstrip(".,;:!?") for found in _URL.findall(question)]))
    parts = [question]
    for url in wanted:
        parts.append("Page %s:\n%s" % (url, read(url)))
    prompt = "\n\n".join(parts)
    notify("asking %s (%s)%s" % (preset.label, model_name, " at " + base if moved else ""))

    try:
        if preset.kind == "claude":
            answer = claude.run(model_name, SYSTEM, prompt, TOOL, call_tool, max_steps)
        else:
            answer = _openai_run(base, key, model_name, SYSTEM, prompt, call_tool, max_steps)
    except (AttributeError, KeyError, IndexError, TypeError):
        raise LLMError("The model server sent a reply this tool does not understand.") from None
    if not reads:
        notify("note: the model answered without reading any page, so treat the answer as its own knowledge")
    return answer


def status() -> List[Tuple[str, Preset, bool, str]]:
    """For ``ebf models``: (name, preset, ready, note) for every preset."""
    rows = []
    for name, preset in PRESETS.items():
        if preset.local:
            try:
                found = list_models(preset.base_url, timeout=0.5)
                rows.append((name, preset, True, "running, %d model(s): %s" % (len(found), ", ".join(found[:3]) or "none loaded")))
            except Exception:  # anything odd on that port just means "no model server here"
                rows.append((name, preset, False, "not running at %s" % preset.base_url))
        elif name == "custom":
            base = os.environ.get("EBF_BASE_URL", "")
            rows.append((name, preset, bool(base), "EBF_BASE_URL=%s" % base if base else "set EBF_BASE_URL or pass --base-url"))
        else:
            ready = bool(os.environ.get(preset.key_env))
            note = "%s is %s" % (preset.key_env, "set" if ready else "not set")
            if preset.kind == "claude":
                try:
                    import anthropic  # noqa: F401
                except ImportError:
                    ready, note = False, note + "; Anthropic SDK not installed (see docs/MODELS.md)"
            rows.append((name, preset, ready, note))
    return rows
