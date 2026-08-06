from __future__ import annotations

import html as html_mod
import http.server
import json
import re
import socketserver
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs, quote

import requests

from anime_watch.models import SearchResult, Episode, StreamSource, MediaResult
from anime_watch.core import SESSION, SCRAPE_TIMEOUT
from .base import BaseProvider
from .hlsproxy import HlsProxy


BASE = "https://streamingunity.vip"
CDN = "https://cdn.streamingunity.vip"
IFRAME_BASE = f"{BASE}/en/iframe"
SEARCH_URL = f"{BASE}/en/search"
TITLE_URL = f"{BASE}/en/titles"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def _get_page_props(url: str, headers: dict | None = None) -> dict:
    resp = SESSION.get(url, headers=headers or HEADERS, timeout=SCRAPE_TIMEOUT)
    resp.raise_for_status()
    m = re.search(r'data-page="([^"]+)"', resp.text)
    if not m:
        return {}
    return json.loads(html_mod.unescape(m.group(1))).get("props", {})


def _get_image_url(images: list[dict]) -> str:
    for img in images:
        if img.get("type") == "poster":
            return f"{CDN}/images/{img['filename']}"
    for img in images:
        if img.get("type") == "cover":
            return f"{CDN}/images/{img['filename']}"
    return ""


def _build_hls_url(embed_url: str) -> tuple[str, str] | None:
    resp = SESSION.get(
        embed_url,
        headers={"Referer": BASE, **HEADERS},
        timeout=SCRAPE_TIMEOUT,
    )
    if resp.status_code != 200:
        return None

    html = resp.text
    token = re.search(r"'token':\s*'([^']+)'", html)
    expires = re.search(r"'expires':\s*'([^']+)'", html)
    sm = re.search(r'window\.streams\s*=\s*(\[.+?\])\s*;', html)

    if not token or not expires or not sm:
        return None

    token = token.group(1)
    expires = expires.group(1)
    streams = json.loads(sm.group(1).replace("'", '"'))
    if not streams:
        return None

    u = urlparse(streams[0]["url"])
    qs_list = [("token", token), ("expires", expires)]
    lang = parse_qs(urlparse(embed_url).query).get("lang", ["en"])[0]
    qs_list.append(("lang", lang))
    if re.search(r'window\.canPlayFHD\s*=\s*true', html):
        qs_list.append(("h", "1"))
    master_url = urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(qs_list), u.fragment))
    return master_url, embed_url


class _VixHlsProxy(HlsProxy):
    """Backwards-compatible alias for the shared HLS proxy."""


def _make_proxied_stream(master_url: str, referer: str) -> Optional[StreamSource]:
    try:
        proxy = _VixHlsProxy(master_url, referer, headers=HEADERS,
                              key_url="https://vixcloud.co/storage/enc.key")
    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError, OSError):
        return None
    return StreamSource(
        url=proxy.master_url,
        site_name="StreamingUnity",
        quality="best",
        is_direct=True,
        headers=None,
        subtitles=None,
        proxy_server=proxy,
    )


class StreamingUnityProvider(BaseProvider):
    name = "StreamingUnity"
    slug = "streamingunity"
    url = "https://streamingunity.vip"
    category = "movies"

    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]

    def search(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        try:
            props = _get_page_props(f"{SEARCH_URL}?q={quote(query)}")
            titles = props.get("titles", [])
            query_words = [w.lower() for w in query.split() if w]
            for item in titles:
                name = item.get("name", "")
                name_lower = name.lower()
                if query_words and not all(w in name_lower for w in query_words):
                    continue
                title_id = item["id"]
                slug = item.get("slug", "")
                media_type = item.get("type", "movie")
                score = item.get("score", "")
                year = ""
                date_str = item.get("last_air_date") or item.get("release_date") or ""
                if date_str:
                    year = date_str[:4]

                display = f"{name} ({year})" if year else name
                if media_type == "tv":
                    seasons = item.get("seasons_count", 0)
                    if seasons:
                        display = f"{name} ({year})" if year else name

                poster = _get_image_url(item.get("images", []))
                results.append(SearchResult(
                    title=display,
                    url=f"{TITLE_URL}/{title_id}-{slug}",
                    site_name=self.name,
                    image=poster,
                    data={
                        "title_id": title_id,
                        "slug": slug,
                        "media_type": media_type,
                        "name": name,
                        "year": year,
                        "score": score,
                    },
                ))
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            pass
        return results

    def get_episodes(self, result: SearchResult) -> list[Episode]:
        data = result.data
        title_id = data.get("title_id")
        slug = data.get("slug", "")
        media_type = data.get("media_type", "movie")
        name = data.get("name", result.title)

        if not title_id:
            return []

        if media_type == "movie":
            return [Episode(
                title=f"{name} (Movie)",
                url=result.url,
                number="1",
                site_name=self.name,
                anime_name=name,
                data={
                    "title_id": title_id,
                    "slug": slug,
                    "media_type": "movie",
                    "name": name,
                    "year": data.get("year", ""),
                },
            )]

        episodes: list[Episode] = []
        try:
            props = _get_page_props(f"{TITLE_URL}/{title_id}-{slug}")
            title = props.get("title", {})
            seasons = title.get("seasons", [])

            for season_info in seasons:
                season_num = season_info.get("number", 1)
                season_id = season_info.get("id")

                season_props = _get_page_props(f"{TITLE_URL}/{title_id}-{slug}/season-{season_num}")
                loaded = season_props.get("loadedSeason", {})
                season_eps = loaded.get("episodes", [])

                for ep in season_eps:
                    ep_num = ep.get("number", 1)
                    ep_name = ep.get("name", f"Episode {ep_num}")
                    scws_id = ep.get("scws_id")
                    episode_id = ep.get("id")

                    episodes.append(Episode(
                        title=f"S{season_num} E{ep_num} - {ep_name}",
                        url=result.url,
                        number=f"{season_num}.{ep_num}",
                        site_name=self.name,
                        anime_name=name,
                        data={
                            "title_id": title_id,
                            "slug": slug,
                            "media_type": "tv",
                            "name": name,
                            "year": data.get("year", ""),
                            "season": season_num,
                            "episode": ep_num,
                            "episode_id": episode_id,
                            "scws_id": scws_id,
                        },
                    ))
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            pass

        return episodes

    def extract_stream(self, episode: Episode, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        data = episode.data
        title_id = data.get("title_id")
        slug = data.get("slug", "")
        media_type = data.get("media_type", "movie")
        episode_id = data.get("episode_id")

        if not title_id:
            return None

        try:
            if media_type == "tv" and episode_id:
                iframe_url = f"{IFRAME_BASE}/{title_id}?episode_id={episode_id}&next_episode=1"
            else:
                iframe_url = f"{IFRAME_BASE}/{title_id}"

            resp = SESSION.get(
                iframe_url,
                headers={"Referer": f"{TITLE_URL}/{title_id}-{slug}", **HEADERS},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None

            m = re.search(r'src="([^"]*vixcloud\.co[^"]*)"', resp.text)
            if not m:
                return None

            embed_url = m.group(1).replace("&amp;", "&")
            result = _build_hls_url(embed_url)
            if not result:
                return None

            hls_url, vixcloud_embed_url = result
            proxied = _make_proxied_stream(hls_url, vixcloud_embed_url)
            if proxied:
                return proxied

            return StreamSource(
                url=hls_url,
                site_name=self.name,
                quality="best",
                is_direct=True,
                headers={"Referer": vixcloud_embed_url},
                subtitles=None,
            )
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            return None

    def resolve(self, media: MediaResult, audio_pref: str = "sub", quality_pref: str = "best") -> Optional[StreamSource]:
        try:
            props = _get_page_props(f"{SEARCH_URL}?q={quote(media.title)}")
            titles = props.get("titles", [])
            for item in titles:
                if item.get("type", "movie") != media.media_type:
                    continue
                item_year = ""
                date_str = item.get("last_air_date") or item.get("release_date") or ""
                if date_str:
                    item_year = date_str[:4]
                if media.year and item_year and item_year != media.year:
                    continue
                title_id = item["id"]
                slug = item.get("slug", "")
                if media.media_type == "tv":
                    episode_id = None
                    if audio_pref == "dub":
                        iframe_url = f"{IFRAME_BASE}/{title_id}?next_episode=1&dub=1"
                    else:
                        iframe_url = f"{IFRAME_BASE}/{title_id}?next_episode=1"
                else:
                    iframe_url = f"{IFRAME_BASE}/{title_id}"

                resp = SESSION.get(
                    iframe_url,
                    headers={"Referer": f"{TITLE_URL}/{title_id}-{slug}", **HEADERS},
                    timeout=SCRAPE_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue

                m = re.search(r'src="([^"]*vixcloud\.co[^"]*)"', resp.text)
                if not m:
                    continue

                embed_url = m.group(1).replace("&amp;", "&")
                result = _build_hls_url(embed_url)
                if not result:
                    continue

                hls_url, vixcloud_embed_url = result
                proxied = _make_proxied_stream(hls_url, vixcloud_embed_url)
                if proxied:
                    return proxied

                return StreamSource(
                    url=hls_url,
                    site_name=self.name,
                    quality="best",
                    is_direct=True,
                    headers={"Referer": vixcloud_embed_url},
                    subtitles=None,
                )
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            pass
        return None
