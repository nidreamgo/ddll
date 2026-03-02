import feedparser
import os
import time
from dotenv import load_dotenv
from helpers import time_difference
from notion import get_feed_urls_from_notion, is_feed_item_exists_in_notion

load_dotenv()

RUN_FREQUENCY = int(os.getenv("RUN_FREQUENCY", "86400"))


def _parse_struct_time_to_timestamp(st):
    """Convert struct_time to timestamp."""
    if st:
        return time.mktime(st)
    return 0


def get_new_feed_items_from(feed_url):
    """Fetch and filter new items from a single RSS feed."""
    try:
        rss = feedparser.parse(feed_url)
    except Exception as e:
        print(f"Error parsing feed {feed_url}: {e}")
        return []

    current_time_struct = rss.get("updated_parsed") or rss.get("published_parsed")
    current_time = _parse_struct_time_to_timestamp(current_time_struct) if current_time_struct else time.time()

    new_items = []
    for item in rss.entries:
        pub_date = item.get("published_parsed") or item.get("updated_parsed")
        if pub_date:
            blog_published_time = _parse_struct_time_to_timestamp(pub_date)
        else:
            continue

        diff = time_difference(current_time, blog_published_time)
        if diff["diffInSeconds"] < RUN_FREQUENCY:
            title = item.get("title", "")
            link = item.get("link", "")

            if is_feed_item_exists_in_notion(title, link):
                continue

            new_items.append({
                "title": title,
                "link": link,
                "content": item.get("content", [{}])[0].get("value", item.get("summary", "")),
                "published_parsed": pub_date
            })

    return new_items


def get_new_feed_items():
    """Fetch new items from all enabled RSS feeds."""
    all_new_feed_items = []

    feeds = get_feed_urls_from_notion()

    for feed in feeds:
        feed_url = feed.get("feedUrl")
        if feed_url:
            feed_items = get_new_feed_items_from(feed_url)
            all_new_feed_items.extend(feed_items)

    # Sort feed items by published date
    all_new_feed_items.sort(
        key=lambda x: _parse_struct_time_to_timestamp(x.get("published_parsed"))
    )

    return all_new_feed_items
