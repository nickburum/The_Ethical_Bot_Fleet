# Privacy

The short version: this software has no way to know who you are, and keeps nothing unless you ask it to.

## What it never does

- It has no accounts, sign-up, licence keys or activation.
- It sends no telemetry, analytics, crash reports or update checks, and never contacts its authors.
- It writes no config file, history, cache or cookie jar. `robots.txt` files are remembered in memory only
  and forgotten when the program exits.
- It never writes API keys anywhere, and hides a key if a server echoes it back in an error message.

## Every network request it makes

| Request | When |
|---|---|
| The pages you or your model ask for, and the `robots.txt` of those sites | `read`, `ask`, `check`, `links`, `fetch` |
| The model server you chose: `/chat/completions`, and `/models` to find the loaded model | `ask` |
| A quick look at five addresses on your own machine (`localhost` ports 11434, 1234, 8080, 8000, 1337) to see which local model apps are running | `ebf models` only |

Nothing else. Model requests never follow redirects, so your key and question cannot be passed on to a
host you did not choose. A model on `localhost` is contacted directly, never through a proxy.

## Where keys go

- A provider's key (for example `OPENAI_API_KEY`) is sent only to that provider's own address.
- If you point a preset somewhere else with `--base-url`, the provider key is not sent; only `EBF_API_KEY`
  is, if you set it.
- `EBF_BASE_URL` applies to local and `custom` presets only and cannot redirect a cloud preset.

## What it stores only if you ask

**The audit log.** Passing `--audit-log FILE` (or `Fleet(audit_log=...)`) appends one record per decision to
that file on your machine, readable only by you. Each record holds: a sequence number, a timestamp, the
event, the bot name and purpose, the **full URL including any query string**, the reason and rule, the HTTP
status and size, and the chain hashes. That makes it a timestamped history of what the bot visited, so
treat it accordingly. It holds no keys, no page content, no questions, and nothing about which model was used.

## Who can see what

| Who | What they see |
|---|---|
| The authors of this software | Nothing. |
| A website you read | Your IP address, as with any visit, and the bot's User-Agent: its name and version, a contact address, its purpose, and this library's version. No cookies, no referrer. |
| A cloud AI provider you choose | Your question, the text of the pages, the short instructions this tool gives the model, and the "not fetched" notes (which include the URLs tried), under that provider's own terms. |
| A local AI model | Runs on your machine; nothing is sent elsewhere. |

### The Claude preset

Claude uses Anthropic's official SDK, which behaves as the SDK does, not as this tool does: it adds its own
headers (SDK version, operating system, CPU type, Python version), and it may read, and refresh, a login
profile under `~/.config/anthropic/` if you created one with `ant auth login`. If you use only
`ANTHROPIC_API_KEY`, no file is involved.

## About the bot's identity

Bots built with this software always say they are bots and always give a contact address, so a site owner
can reach whoever runs the bot. That address does not have to identify a person: a project page or a shared
mailbox is fine, and the command line uses a generic identity unless you set one.

This software will not disguise a bot as a browser or a person, rotate identities, or route around blocks.
If you want your IP address hidden from the sites you read, use a VPN you trust; that is outside what this
tool does. Note that if your system routes web traffic through an HTTP proxy, page requests go through it.
