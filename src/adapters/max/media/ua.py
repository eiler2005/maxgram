"""MAX CDN user-agent selection."""

from typing import Optional
from urllib.parse import parse_qs, urlparse

MAX_CDN_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_CDN_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
MAX_CDN_IOS_CHROME_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/136.0.0.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_CDN_ANDROID_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Mobile) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Mobile Safari/537.36"
)


def download_client_profile_for_url(url: str) -> tuple[dict[str, str], Optional[str], str]:
    src_ag = (
        parse_qs(urlparse(url).query).get("srcAg", [None])[0]
        if url else None
    )
    normalized = str(src_ag or "").upper()
    if "CHROME" in normalized and "ANDROID" in normalized:
        user_agent = MAX_CDN_ANDROID_CHROME_USER_AGENT
        ua_family = "chrome_android"
    elif "CHROME" in normalized and ("IPHONE" in normalized or "IOS" in normalized):
        user_agent = MAX_CDN_IOS_CHROME_USER_AGENT
        ua_family = "chrome_ios"
    elif "CHROME" in normalized:
        user_agent = MAX_CDN_CHROME_USER_AGENT
        ua_family = "chrome_desktop"
    else:
        user_agent = MAX_CDN_USER_AGENT
        ua_family = "safari_mobile"
    return {"User-Agent": user_agent}, src_ag, ua_family


def download_headers_for_url(url: str) -> dict[str, str]:
    headers, _src_ag, _ua_family = download_client_profile_for_url(url)
    return headers
