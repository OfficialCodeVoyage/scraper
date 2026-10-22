"""scout: find what people post on Reddit, YouTube, X, Threads, Hacker News, GitHub, Bluesky and Wikipedia.

ScrapeCreators answers for Reddit, YouTube, X and Threads under a daily credit cap; the other sources are free
and keyless. A profile drives the daily brief, whose summaries come from ``claude -p`` on the user's Claude
subscription, never the API. ``scout.api`` is the surface both the CLI and the MCP server use.
"""

__version__ = "0.1.0"
