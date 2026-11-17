"""Fetch a list of URLs politely and show what the fleet allowed and refused.

    python examples/crawl.py https://example.com/ https://www.python.org/
"""

import sys

from ethical_bot_fleet import Fleet, Policy

fleet = Fleet(Policy(min_delay=1.0), audit_log="audit.jsonl")
# Change the name and contact to your own before running this for real.
bot = fleet.bot("ExampleCrawlBot", contact="https://github.com/nickburum/The_Ethical_Bot_Fleet", purpose="other")

for result in bot.crawl(sys.argv[1:], workers=8):
    if result.response:
        print("%3d  %s  (%d bytes)" % (result.response.status, result.url, len(result.response.body)))
    elif result.blocked:
        print(" NO  %s  (%s)" % (result.url, result.error.decision.detail))
    else:
        print("ERR  %s  (%s)" % (result.url, result.error))

print("\nEvery decision is recorded in audit.jsonl. Check it with: ebf audit verify audit.jsonl")
