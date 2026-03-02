import os
import requests
from dotenv import load_dotenv

load_dotenv()

NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_READER_DATABASE_ID = os.getenv("NOTION_READER_DATABASE_ID")
NOTION_FEEDS_DATABASE_ID = os.getenv("NOTION_FEEDS_DATABASE_ID")
CI = os.getenv("CI")

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

log_level = "info" if CI else "debug"


def _get_headers():
    """Get common headers for Notion API requests."""
    return {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }


def get_feed_urls_from_notion():
    """Fetch enabled feed URLs from the Feeds database in Notion."""
    url = f"{NOTION_BASE_URL}/databases/{NOTION_FEEDS_DATABASE_ID}/query"

    payload = {
        "filter": {
            "property": "Enabled",
            "checkbox": {"equals": True}
        }
    }

    try:
        response = requests.post(url, headers=_get_headers(), json=payload)
        response.raise_for_status()
        results = response.json().get("results", [])
    except requests.exceptions.RequestException as err:
        print(f"Error fetching feed URLs: {err}")
        return []

    feeds = []
    for item in results:
        props = item.get("properties", {})
        title_prop = props.get("Title", {}).get("title", [])
        link_prop = props.get("Link", {}).get("url")

        title = title_prop[0].get("plain_text", "") if title_prop else ""
        feeds.append({"title": title, "feedUrl": link_prop})

    return feeds


def is_feed_item_exists_in_notion(title, link):
    """Check if a feed item with the given link or title already exists in Notion."""
    url = f"{NOTION_BASE_URL}/databases/{NOTION_READER_DATABASE_ID}/query"

    filters = []
    if link:
        filters.append({"property": "Link", "url": {"equals": link}})
    if title:
        filters.append({"property": "Title", "rich_text": {"equals": title}})

    if not filters:
        return False

    payload = {"filter": {"or": filters}}

    try:
        response = requests.post(url, headers=_get_headers(), json=payload)
        response.raise_for_status()
        return len(response.json().get("results", [])) > 0
    except requests.exceptions.RequestException as err:
        print(f"Error checking feed item existence: {err}")
        return False


def add_feed_item_to_notion(notion_item):
    """Add a new feed item to the Reader database in Notion."""
    title = notion_item.get("title")
    link = notion_item.get("link")
    content = notion_item.get("content")

    url = f"{NOTION_BASE_URL}/pages"

    payload = {
        "parent": {"database_id": NOTION_READER_DATABASE_ID},
        "properties": {
            "Title": {
                "title": [{"text": {"content": title}}]
            },
            "Link": {
                "url": link
            }
        },
        "children": content
    }

    try:
        response = requests.post(url, headers=_get_headers(), json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as err:
        print(f"Error adding feed item to Notion: {err}")


def delete_old_unread_feed_items_from_notion():
    """Delete feed items older than 30 days that are still unread."""
    import datetime

    fetch_before_date = datetime.datetime.now(datetime.timezone.utc)
    fetch_before_date -= datetime.timedelta(days=30)

    url = f"{NOTION_BASE_URL}/databases/{NOTION_READER_DATABASE_ID}/query"

    payload = {
        "filter": {
            "and": [
                {
                    "property": "Created At",
                    "date": {"on_or_before": fetch_before_date.isoformat()}
                },
                {
                    "property": "Read",
                    "checkbox": {"equals": False}
                }
            ]
        }
    }

    try:
        response = requests.post(url, headers=_get_headers(), json=payload)
        response.raise_for_status()
        results = response.json().get("results", [])
    except requests.exceptions.RequestException as err:
        print(f"Error querying old feed items: {err}")
        return

    for item in results:
        page_id = item.get("id")
        update_url = f"{NOTION_BASE_URL}/pages/{page_id}"

        try:
            requests.patch(update_url, headers=_get_headers(), json={"archived": True})
        except requests.exceptions.RequestException as err:
            print(f"Error archiving page {page_id}: {err}")
