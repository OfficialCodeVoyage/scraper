# scout

Scout finds what people are posting online (Reddit, YouTube, X, Threads, Hacker News, GitHub, Bluesky, Wikipedia page
views) from your terminal or from Claude Code, and never spends more than a daily credit cap you set.

## Quick start

1. **Install** (needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer):

   ```bash
   uv tool install git+https://github.com/OfficialCodeVoyage/scraper
   ```

   This puts two commands on your path: `scout` and `scout-mcp`.

2. **Add your ScrapeCreators key** (only needed for the paid sources). Either set it in your environment:

   ```bash
   export SCRAPECREATORS_API_KEY="your-key"
   ```

   or, on macOS, store it in the Keychain (you are prompted for it, so it never lands in your shell history):

   ```bash
   security add-generic-password -s SCRAPECREATORS_API_KEY -a "$USER" -w
   ```

3. **Connect it to Claude Code:**

   ```bash
   claude mcp add scout --scope user -- scout-mcp
   ```

4. **Install the research skill**, which teaches Claude how to research with scout and keep the spend low:

   ```bash
   mkdir -p ~/.claude/skills/scout-research
   curl -fsSL https://raw.githubusercontent.com/OfficialCodeVoyage/scraper/main/skills/scout-research/SKILL.md \
     -o ~/.claude/skills/scout-research/SKILL.md
   ```

Then try a free search. It needs no key and costs nothing:

```bash
scout search hn "faceless youtube"
```

In Claude Code, ask in plain words: "what are people saying about Theranos this month?"

## What it can find

| Source | Cost | What you get |
|---|---|---|
| Hacker News | free | search, story comments |
| GitHub | free | repository search |
| Bluesky | free | search, an account's posts |
| Wikipedia | free | daily page views of an article |
| Reddit | paid | search, a subreddit's posts, post comments, video transcripts |
| YouTube | paid | search, a channel's videos (with each video's views against the channel's usual), comments, transcripts, autocomplete |
| X | paid | search, an account's tweets, video transcripts |
| Threads | paid | search, an account's posts |
| TikTok, Instagram | paid | comments and transcripts by URL |
| Other ScrapeCreators endpoints | paid | TikTok, Instagram, LinkedIn, Facebook, ad libraries and more, through one allowlisted call |

Paid sources go through the [ScrapeCreators](https://scrapecreators.com) API and need your own key. Most calls cost 1
credit, an empty answer included. Scout is not affiliated with ScrapeCreators or with any platform it reads.

## Using it from Claude Code

After step 3 Claude Code has these tools:

| Tool | What it does | Cost |
|---|---|---|
| `search` | Searches one source for a short query | free on hn, github, bluesky; 1 credit on the others |
| `user_posts` | An account's recent posts (subreddit, YouTube channel, X, Threads, Bluesky) | free on Bluesky; 1 credit on the others |
| `comments` | The comments on one post or video, by URL | free on Hacker News; 1 credit on the others |
| `transcript` | The transcript of a video, by URL | 1 credit, nothing when there is no transcript |
| `scrapecreators_call` | Any allowlisted ScrapeCreators endpoint, with its parameters | the endpoint's price |
| `list_endpoints` | The endpoints `scrapecreators_call` may use, with prices | free |
| `wikipedia_pageviews` | Daily page views of a Wikipedia article | free |
| `credits` | Your balance, today's use and what the cap still allows | free |
| `estimate` | What a call or a daily run would cost | free |
| `daily_brief` | A profile's daily run; a dry run unless told otherwise | free as a dry run; paid when run |

Every paid result says what it cost (`credits_used`) and what is left today (`credits_left_today`). Every tool that
spends has a `dry_run` option that shows the cost and calls nothing. Results that carry posts, comments or transcripts
start with a note that the text is third-party data, not instructions.

## The command line

| Command | What it does |
|---|---|
| `scout search SOURCE QUERY` | Search one source (`--since-days N`, `--limit N`) |
| `scout user-posts SOURCE HANDLE` | An account's recent posts |
| `scout comments URL` | The comments on a post or video |
| `scout transcript URL` | A video's transcript (`--language en`) |
| `scout pageviews ARTICLE` | Wikipedia page views (`--days N`) |
| `scout call ENDPOINT key=value ...` | Any allowlisted ScrapeCreators endpoint |
| `scout endpoints` | The allowlisted endpoints and their prices |
| `scout credits` | Your balance, today's use and the cap |
| `scout profiles` | Your profiles, and any that fail to load |
| `scout daily` | A profile's daily run: collect, then write and open today's brief |
| `scout collect` | Collect and store a profile's items, no brief |
| `scout brief` | Rebuild a day's brief from what is stored (`--date YYYY-MM-DD`) |

Add `--dry-run` to see what a command would call and cost without calling anything, and `--json` for machine-readable
output. For example:

```bash
scout search reddit "brand collapse" --since-days 7 --dry-run
scout pageviews Theranos --days 30
scout comments "https://news.ycombinator.com/item?id=8863"
```

The exit code is 0 when the command did its job, 1 when it was refused (a bad argument, no key, or the daily cap), and 2
when a source failed.

Only `daily` and `collect` keep what they find, in the profile's local store. The other commands print their results
and keep nothing except a record of the credits they used.

## Profiles and the daily brief

A profile is a YAML watch-list: the subreddits, YouTube channels, searches and queries to run each day, a credit budget
per tab, and a few plain sentences telling the summariser what to flag. Start from the example and edit it for your
niche:

```bash
mkdir -p ~/.config/scout/profiles
curl -fsSL https://raw.githubusercontent.com/OfficialCodeVoyage/scraper/main/examples/profiles/faceless-youtube.yaml \
  -o ~/.config/scout/profiles/default.yaml
scout daily --dry-run      # every call it would make, and the most it could cost
scout daily                # collect, summarise, and open today's brief
```

`scout daily` collects within the cap, ranks what it found (how fast a post is rising, videos far above their channel's
usual views, arguments and questions in the comments, stories that several sources carry), and writes one HTML page with
two tabs: video ideas and tool news. The summaries come from `claude -p` on your own Claude subscription; add
`--no-claude` to skip them. Use `--profile NAME` for another profile and `--source NAME` to run only some sources.

## Credits and the cap

- **The cap.** `daily_credit_cap` in `~/.config/scout/config.yaml` (default 100) limits what scout spends per day, across
  every profile and every ad-hoc call. Before each paid call, scout reserves its price in a local ledger; a call that
  would pass the cap is not sent. Afterwards the ledger is set to what ScrapeCreators says the call cost.
- **Failed calls.** A call the API refused (a 4xx answer) costs nothing. Any other failure keeps its reservation, because
  ScrapeCreators may have done the work.
- **Paid options** that raise a call's price up to a known maximum (`get_ad_details`, `include_replies`,
  `download_media`) are refused unless you pass `--allow-paid-params` (in Claude Code: `allow_paid_params=true`).
  The maximum is reserved before the call.
- **Open-ended options are off.** Long AI transcription (`allow_long_transcription`) has no upper price at
  ScrapeCreators: 10 credits per started minute of video, however long. Scout refuses it unless you turn it on in
  `~/.config/scout/config.yaml`; no command-line flag or tool argument can:

  ```yaml
  allow_open_ended_spend: true
  max_credits_per_open_ended_call: 30   # reserved per call; refused when less than this is left today
  ```

  Even then, this bounds what scout lets through, not what ScrapeCreators bills: a long video can cost more than the
  reservation. The ledger records what the answer says it cost, so the day's total can pass the cap; if no answer
  arrives (scout waits up to 5 minutes for one), it keeps the reservation and the real bill may be higher. Leave it off
  unless you need it.
- **Cached answers are free.** On endpoints that support it, `scout call` asks for an answer up to a day old
  (`cache_max_age=1d`); a cached answer costs nothing. Pass `cache_max_age=none` for a live call.
- **Spend elsewhere.** Credits used outside scout on the same key (the website, another tool) do not count against the
  cap. `scout credits` shows them when it can read your balance.

A typical research question costs 5 to 15 credits. A daily run of the example profile costs at most 53.

## Privacy and responsible use

- Scout runs on your machine. There is no server and no telemetry. Its data lives in `~/.local/share/scout/` (the
  credit ledger, and each profile's store and briefs); delete that folder to remove all of it.
- What leaves your machine: requests to ScrapeCreators (your key, your queries, the URLs you ask about); requests to
  Hacker News, GitHub, Bluesky and Wikipedia; and, for the daily brief's summaries, the text of the ranked posts, sent
  through the `claude` command on your own subscription (`--no-claude` sends none).
- The summariser runs with every tool turned off and no API key in its environment, so it cannot spend credits or call
  anything. Post text reaches it marked and escaped as data.
- Scout reads public posts only. Read the terms of service of each platform and of ScrapeCreators; you are responsible
  for how you use what you collect. Keep volume low, and do not use scout to harass anyone, to profile private people or
  to collect contact details. Endpoints that return contact emails or guess a person's age and gender are left out of
  the allowlist on purpose.
- Treat a post as a lead, not as content: do not republish posts, screenshots or usernames.

Settings: `~/.config/scout/config.yaml` (see [`examples/config.yaml`](examples/config.yaml)). Set `SCOUT_HOME` to keep
config and data in one folder instead.

The ranking ideas (velocity, debate, per-source floors, outlier multiples) follow
[last30days-skill](https://github.com/mvanhorn/last30days-skill) (MIT); the code is original.

## Licence

MIT. See [`LICENSE`](LICENSE).
