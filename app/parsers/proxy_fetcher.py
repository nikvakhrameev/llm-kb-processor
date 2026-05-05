"""Fetch free HTTP proxy list from proxifly CDN."""

import aiohttp

PROXY_LIST_URL = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.json"
)


async def fetch_proxies() -> list[str]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                PROXY_LIST_URL,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception:
        return []

    return [item["proxy"] for item in data if item.get("proxy")]
