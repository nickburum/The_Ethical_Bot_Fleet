"""A web-fetch tool for an LLM agent.

Give your model ``fetch_page`` as its only way to reach the web. Whatever URL
it asks for goes through the fleet's checks, and a refusal comes back as a
sentence the model can read and act on.

    python examples/agent_tool.py https://example.com/
"""

import sys

from ethical_bot_fleet import Blocked, FetchError, Fleet

fleet = Fleet(audit_log="audit.jsonl")
# "ai-input" means: the page is read to answer a question, not to train a model.
bot = fleet.bot("ExampleAgentBot", contact="https://github.com/nickburum/The_Ethical_Bot_Fleet", purpose="ai-input")

# The description to register with your LLM framework of choice.
TOOL = {
    "name": "fetch_page",
    "description": "Fetch a public web page. Returns the page text, or the reason it may not be fetched.",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "An http(s) URL"}},
        "required": ["url"],
    },
}


def fetch_page(url: str, limit: int = 20_000) -> str:
    try:
        return bot.get(url).text[:limit]
    except Blocked as no:
        return "Not fetched. %s Do not retry this URL." % no.decision.detail
    except FetchError as error:
        return "The request failed: %s" % error


if __name__ == "__main__":
    for address in sys.argv[1:]:
        print(fetch_page(address)[:500])
