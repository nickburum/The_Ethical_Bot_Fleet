# Connecting an AI model

`ebf ask` works with a model on your own computer or with a cloud provider. Either way, every page the
model reads goes through the fleet, so the same rules apply.

Check what is ready at any time:

```bash
ebf models
```

## A model on your own computer

Local models are private: your question and the pages go to a program on your machine and nowhere else.

### Ollama (easiest)

1. Install Ollama from [ollama.com](https://ollama.com) and start it.
2. Download a model. Pick one from the Ollama library that supports tools if you can:

   ```bash
   ollama pull MODEL_NAME
   ```

3. Ask:

   ```bash
   ebf ask "What is this page about?" https://example.com/ --model ollama
   ```

With no model name, the first model Ollama lists is used. To choose one:

```bash
ebf ask "What is this page about?" https://example.com/ --model ollama:MODEL_NAME
```

### LM Studio, llama.cpp, vLLM, Jan

Start the app's local server, load a model, and use its preset name. Each preset expects the app's usual address:

| Preset | Address it expects | How to start the server |
|---|---|---|
| `ollama` | `http://localhost:11434/v1` | runs when Ollama is open |
| `lmstudio` | `http://localhost:1234/v1` | Developer tab, Start Server |
| `llamacpp` | `http://localhost:8080/v1` | `llama-server -m model.gguf` |
| `vllm` | `http://localhost:8000/v1` | `vllm serve MODEL_NAME` |
| `jan` | `http://localhost:1337/v1` | Settings, Local API Server |

```bash
ebf ask "What is this page about?" https://example.com/ --model lmstudio
```

### Any other local or self-hosted server

If it speaks the OpenAI-compatible chat API (most do), point at it:

```bash
ebf ask "What is this page about?" https://example.com/ --model custom --base-url http://localhost:5000/v1
```

A different port or another machine on your network works the same way with any preset:

```bash
ebf ask "What is this page about?" https://example.com/ --model ollama --base-url http://192.168.1.20:11434/v1
```

If the server needs a key, put it in `EBF_API_KEY`. That key is sent to any server you point at with
`--base-url`. A cloud provider's own key (such as `OPENAI_API_KEY`) is only ever sent to that provider's
real address, never to an address you override.

To avoid typing the address each time, set `EBF_BASE_URL`. It applies to the local and `custom` presets
only; it never redirects a cloud preset.

A model on this machine (`localhost`) is always contacted directly, even if your system uses a proxy.

### Models that cannot use tools

Some small local models cannot call tools. That is fine: `ebf` notices, and the model answers from the
pages you listed on the command line. It just cannot ask for more pages on its own.

## A cloud model

Put the provider's key in the environment variable shown, then name the preset. Keys are read from the
environment only; `ebf` never saves them.

| Preset | Environment variable | Example |
|---|---|---|
| `claude` | `ANTHROPIC_API_KEY` | `--model claude` |
| `openai` | `OPENAI_API_KEY` | `--model openai:MODEL_ID` |
| `gemini` | `GEMINI_API_KEY` | `--model gemini:MODEL_ID` |
| `mistral` | `MISTRAL_API_KEY` | `--model mistral:MODEL_ID` |
| `groq` | `GROQ_API_KEY` | `--model groq:MODEL_ID` |
| `deepseek` | `DEEPSEEK_API_KEY` | `--model deepseek:MODEL_ID` |
| `xai` | `XAI_API_KEY` | `--model xai:MODEL_ID` |
| `openrouter` | `OPENROUTER_API_KEY` | `--model openrouter:MODEL_ID` |
| `together` | `TOGETHER_API_KEY` | `--model together:MODEL_ID` |

`MODEL_ID` is the model name from that provider's own documentation. Model names change often, so `ebf`
does not guess one for you.

### Claude

Claude uses Anthropic's official SDK, which is an optional extra:

```bash
pip install "ethical-bot-fleet[claude] @ git+https://github.com/nickburum/The_Ethical_Bot_Fleet"
```

```bash
export ANTHROPIC_API_KEY=your-key
```

```bash
ebf ask "Summarise this page" https://example.com/ --model claude
```

The default is `claude-opus-5-5`. To pick another, for example the faster and cheaper Haiku:

```bash
ebf ask "Summarise this page" https://example.com/ --model claude:claude-haiku-5-5
```

The `claude` preset does not take `--base-url`; to go through a gateway, set `ANTHROPIC_BASE_URL`, which
the SDK reads itself.

If a request is declined by a safety check on the default model, Anthropic's server-side fallback lets
another Claude model answer in the same call.

## Set a default

So you do not have to type `--model` every time:

```bash
export EBF_MODEL=ollama
```

## In Python

```python
from ethical_bot_fleet import Fleet, ask

bot = Fleet().bot("AcmeReaderBot", contact="https://acme.example/bot", purpose="ai-input")
print(ask(bot, "What is this page about?", ["https://example.com/"], model="ollama"))
```

## Troubleshooting

| Message | What to do |
|---|---|
| `No model chosen` | add `--model NAME` or set `EBF_MODEL` |
| `Could not reach the model server` | start the local app, or check the address with `--base-url` |
| `has no models loaded yet` | download or load a model in the local app |
| `needs an API key in the ... environment variable` | export that variable |
| `Name a model for ...` | use `--model NAME:MODEL_ID` |
| `HTTP 404` from the model server | the address usually needs to end in `/v1` |
| `tried to redirect the request elsewhere` | use the server's final address; redirects are refused so keys cannot leak |
| `Not fetched: ...` in the answer | the site does not allow it; that is the fleet working |
