# Contributing

Thanks for helping the bots behave.

## Setup

```bash
pip install -e .
```

```bash
python -m unittest discover -s tests
```

The tests use an in-memory fake web, so they run offline in well under a second.

## Ground rules

- **No runtime dependencies.** The standard library only.
- **Every behaviour change needs a test.**
- **Python 3.9 compatible.**
- **The fleet only gets stricter by default.** Features that help a bot hide, impersonate, post as a crowd,
  or get around a block will be declined. See the [Bot Code of Conduct](docs/BOT_CODE_OF_CONDUCT.md).

## Good first contributions

- An asyncio API alongside the threaded one
- Transports for `httpx` and `requests`
- Sitemap discovery from robots.txt
- Tracking the IETF AI-preferences work as it settles
- More robots.txt edge cases from real sites, as tests

## Reporting a site-owner problem

If a bot built with this library misbehaved on your site, open an issue with the User-Agent string you saw.
