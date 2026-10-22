"""Each ScrapeCreators response is normalised to the one item shape.

The fixtures under ``tests/fixtures/scrapecreators`` follow the field names and nesting of ScrapeCreators' published
OpenAPI examples, with invented names and text: no real account or post is copied.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from support import SC_HOST, FakeWeb, check_shape

from scout.scrapecreators import ScrapeCreatorsClient

MakeClient = Callable[..., ScrapeCreatorsClient]
Load = Callable[[str], Any]


async def test_reddit_search_gives_one_item_per_post_with_an_id(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body=load("scrapecreators/reddit_search"))

    items = await make_client(10).reddit_search("bank run", "week", lens="video_ideas")

    assert [i.ext_id for i in items] == ["abc123", "def456"]  # the post without an id is dropped
    assert check_shape(items[0], "reddit") == {
        "source": "reddit",
        "id": "abc123",
        "key": "reddit:abc123",
        "url": "https://www.reddit.com/r/ExampleSub/comments/abc123/example_post/",
        "title": "Example post about a bank run",
        "text": "Body of the example post.",
        "author": "fake_user_one",
        "community": "ExampleSub",
        "created_at": "2026-09-21T14:13:20+00:00",
        "score": 1500,
        "comments": 320,
        "views": None,
        "upvote_ratio": None,  # a search answer has no ratio
        "link": "https://example.com/story",  # an outbound link, kept apart from the Reddit permalink
        "extra": {"domain": "example.com", "flair": "News"},
    }
    assert (items[0].lens, items[0].watch) == ("video_ideas", "reddit: bank run")
    assert dict(web.paid[0].url.params) == {"query": "bank run", "sort": "relevance", "timeframe": "week"}  # no trim

    selfpost = check_shape(items[1], "reddit")
    assert (
        selfpost["url"] == "https://www.reddit.com/r/ExampleSub/comments/def456/a_self_post/"
    )  # no permalink: its url
    assert selfpost["link"] is None  # a Reddit-hosted url is not an outbound link


async def test_a_subreddit_listing_keeps_the_upvote_ratio(web: FakeWeb, load: Load, make_client: MakeClient) -> None:
    web.on(SC_HOST, "/v1/reddit/subreddit", body=load("scrapecreators/reddit_subreddit"))

    items = await make_client(10).reddit_subreddit("r/ExampleSub", "top", "day")

    data = check_shape(items[0], "reddit")
    assert (data["upvote_ratio"], data["score"], data["comments"]) == (0.62, 900, 410)
    assert items[0].watch == "r/ExampleSub top/day"
    assert web.paid[0].url.params["subreddit"] == "ExampleSub"  # the r/ is stripped


async def test_reddit_comments_skip_deleted_authors_bots_empty_bodies_and_replies_below_the_second_level(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    web.on(SC_HOST, "/v1/reddit/post/comments", body=load("scrapecreators/reddit_comments"))
    url = "https://www.reddit.com/r/ExampleSub/comments/abc123/example_post/"

    found = await make_client(10).reddit_comments_of(url, "abc123")

    assert [c.ext_id for c in found] == ["c1", "c2"]
    assert found[0].to_dict() == {
        "source": "reddit",
        "post_id": "abc123",
        "id": "c1",
        "author": "fake_commenter_a",
        "text": "Is this the same story as the last collapse?",
        "score": 40,
        "created_at": "2026-09-21T14:18:20+00:00",
    }
    assert web.paid[0].url.params["url"] == url  # the comments endpoint wants the post's full URL


async def test_youtube_search_gives_videos_only_with_channel_views_and_date(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    web.on(SC_HOST, "/v1/youtube/search", body=load("scrapecreators/youtube_search"))
    client = make_client(10)

    items = await client.youtube_search("rise and fall", "this_week")

    assert [i.ext_id for i in items] == ["vid00000001"]  # a short and a video without an id are left out
    assert check_shape(items[0], "youtube") == {
        "source": "youtube",
        "id": "vid00000001",
        "key": "youtube:vid00000001",
        "url": "https://www.youtube.com/watch?v=vid00000001",
        "title": "The Rise and Fall of Example Corp",
        "text": "An example description of an example video.",
        "author": "Example Channel",
        "community": "@examplechannel",
        "created_at": "2026-10-01T10:00:00+00:00",
        "score": 120000,  # a video's engagement is its views
        "comments": 850,
        "views": 120000,
        "upvote_ratio": None,
        "link": None,
        "extra": {"likes": 9100, "seconds": 754},
    }
    assert dict(web.paid[0].url.params) == {"query": "rise and fall", "type": "videos", "uploadDate": "this_week"}

    await client.youtube_search("rise and fall", None)
    assert "uploadDate" not in web.paid[1].url.params


async def test_a_youtube_channel_marks_each_video_with_its_views_over_the_channels_median(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    body = load("scrapecreators/youtube_channel_videos")
    web.on(SC_HOST, "/v1/youtube/channel-videos", body=body)
    client = make_client(10)

    items = await client.youtube_channel("@examplechannel", "latest")

    assert len(items) == 6
    assert items[0].extra["channel_median"] == 1000
    assert items[0].extra["outlier"] == 1.0
    assert items[-1].extra["outlier"] == 100.0
    assert dict(web.paid[0].url.params) == {"handle": "examplechannel", "sort": "latest"}

    # Under five videos a median means nothing, so no multiple is given.
    web.on(SC_HOST, "/v1/youtube/channel-videos", body={**body, "videos": body["videos"][:3]})
    few = await client.youtube_channel("UC" + "x" * 22, "popular")
    assert all(i.extra["outlier"] is None for i in few)
    assert dict(web.paid[1].url.params) == {"channelId": "UC" + "x" * 22, "sort": "popular"}


async def test_a_youtube_transcript_gives_the_text_and_timed_segments(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    web.on(SC_HOST, "/v1/youtube/video/transcript", body=load("scrapecreators/youtube_transcript"))

    found = await make_client(10).youtube_transcript("https://www.youtube.com/watch?v=vid00000001", "en")

    assert found["video_id"] == "vid00000001"
    assert found["language"] == "English"
    assert found["text"] == "welcome to the example video today we look at a made-up company"
    assert found["segments"][0] == {
        "text": "welcome to the example video",
        "start_ms": 160,
        "end_ms": 1920,
    }  # "160" -> 160
    assert web.paid[0].url.params["language"] == "en"


async def test_x_search_normalises_tweets_and_adds_the_month_to_the_query(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    web.on(SC_HOST, "/v1/twitter/search/posts", body=load("scrapecreators/x_search"))
    client = make_client(10)

    items = await client.x_search("example query")

    assert [i.ext_id for i in items] == ["1000000000000000001", "1000000000000000002"]  # no id: dropped
    assert web.paid[0].url.params["query"] == "example query October 2026"  # the endpoint searches Google's index
    first = check_shape(items[0], "x")
    assert first["author"] == "fake_handle"
    assert first["title"] == "A short example post about a made-up company."
    assert (first["score"], first["comments"], first["views"]) == (11, 4, 780)  # likes, replies, views ("780")
    assert first["created_at"] == "2026-10-01T18:50:27+00:00"
    assert first["extra"] == {"reposts": 2, "quotes": 1, "bookmarks": 7}

    long_post = check_shape(items[1], "x")
    assert long_post["text"].startswith("The long form text of an example post")  # the full note, not the cut-off text
    assert long_post["url"] == "https://x.com/i/status/1000000000000000002"  # no handle, no url: a status link

    await client.x_search("example query", days=None)  # no window: nothing added
    assert web.paid[1].url.params["query"] == "example query"


async def test_threads_search_normalises_posts_and_bounds_the_dates(
    web: FakeWeb, load: Load, make_client: MakeClient
) -> None:
    web.on(SC_HOST, "/v1/threads/search", body=load("scrapecreators/threads_search"))
    client = make_client(10)

    items = await client.threads_search("example", days=14)

    assert [i.ext_id for i in items] == ["EXAMPLEcode1", "EXAMPLEcode2"]  # no code: dropped
    assert dict(web.paid[0].url.params) == {"query": "example", "start_date": "2026-10-06", "end_date": "2026-10-20"}
    first = check_shape(items[0], "threads")
    assert (first["author"], first["score"], first["comments"]) == ("fake_threads_user", 540, 21)
    assert first["title"] == "An example thread post."
    assert first["extra"] == {"reposts": 4, "quotes": 2}
    assert first["created_at"] == "2026-09-21T14:13:20+00:00"
    assert items[1].url == "https://www.threads.com/@fake_threads_user_two/post/EXAMPLEcode2"  # built from the code

    await client.threads_search("example", days=None)
    assert dict(web.paid[1].url.params) == {"query": "example"}
