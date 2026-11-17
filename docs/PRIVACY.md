# Privacy

The short version: this software has no way to know who you are, and keeps nothing.

## What it never does

- It has no accounts, sign-up, licence keys or activation.
- It sends no telemetry, analytics, crash reports or update checks. The only network requests it makes are
  the pages you (or your model) ask for, the `robots.txt` of those sites, and calls to the AI model you chose.
- It writes no config file, history, cache or cookie jar. `robots.txt` files are remembered in memory only
  and forgotten when the program exits.
- It never writes API keys anywhere. Keys are read from environment variables at the moment of the call
  and sent only to the provider they belong to.

## What it stores only if you ask

- **The audit log.** Passing `--audit-log FILE` (or `Fleet(audit_log=...)`) appends a record of each decision
  to that file on your machine: the bot name, purpose, URL, and outcome. It contains no keys, no page
  content, and no information about you. Delete it whenever you like.

## Who can see what

| Who | What they see |
|---|---|
| The authors of this software | Nothing. |
| A website you read | Your IP address, as with any visit, and the bot's User-Agent: its name, a contact address, and its purpose. |
| A cloud AI provider you choose | Your question and the text of the pages, under that provider's own terms. |
| A local AI model | Runs on your machine; nothing is sent elsewhere. |

## About the bot's identity

Bots built with this software always say they are bots and always give a contact address, so a site owner
can reach whoever runs the bot. That address does not have to identify a person: a project page or a shared
mailbox is fine, and the command line uses a generic identity unless you set one.

This software will not disguise a bot as a browser or a person, rotate identities, or route around blocks.
If you want your IP address hidden from the sites you read, use a VPN you trust; that is outside what this
tool does.
