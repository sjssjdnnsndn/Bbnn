#!/usr/bin/env python3
"""
YouTube Video Scraper Bot
Extracted from advanced multi-site scraper.

Supports:
  • All YouTube URL formats (watch, shorts, youtu.be, embed)
  • Multiple quality options (best, 1080p, 720p, 480p, 360p, 144p, worst)
  • HLS manifest extraction (web_safari client)
  • Adaptive MP4 stream extraction (mediaconnect client)
  • Cookie-based auth bypass for age-gated / private videos

Usage:
  python bot.py <youtube_url>
  python bot.py <youtube_url> --quality 720p
  python bot.py <youtube_url> --cookies "session=abc123; token=xyz"
  python bot.py <youtube_url> --cookies-file /path/to/cookies.txt
"""

import sys
import json
import re
import io
import os
import tempfile
from typing import Dict, List, Optional
from urllib.parse import urlparse

# ── Optional: curl_cffi for oEmbed metadata fallback ──────────────────────────
try:
    from curl_cffi import requests as curl_requests
    CURL_AVAILABLE = True
except ImportError:
    CURL_AVAILABLE = False

# ── yt-dlp (required) ─────────────────────────────────────────────────────────
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

QUALITY_ORDER = ['2160p', '1080p', '720p', '480p', '360p', '240p', '144p']

# YouTube domains for URL detection
YOUTUBE_DOMAINS = ('youtube.com', 'youtu.be', 'youtube-nocookie.com', 'yt.be')

# yt-dlp quality format strings
YTDLP_QUALITY_MAP = {
    'best':  'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
    'worst': 'worstvideo+worstaudio/worst',
    '2160p': 'bestvideo[height<=2160][ext=mp4]+bestaudio/best[height<=2160]',
    '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio/best[height<=1080]',
    '720p':  'bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720]',
    '480p':  'bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]',
    '360p':  'bestvideo[height<=360][ext=mp4]+bestaudio/best[height<=360]',
}

# HLS-compatible quality map (used for web_safari / mediaconnect clients)
YTDLP_QUALITY_MAP_HLS = {
    'best':  'bestvideo[height<=2160]+bestaudio/best',
    'worst': 'worstvideo+worstaudio/worst',
    '2160p': 'bestvideo[height<=2160]+bestaudio/best[height<=2160]/best',
    '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
    '720p':  'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
    '480p':  'bestvideo[height<=480]+bestaudio/best[height<=480]/best',
    '360p':  'bestvideo[height<=360]+bestaudio/best[height<=360]/best',
    '144p':  'bestvideo[height<=144]+bestaudio/best[height<=144]/best',
}

# Player clients tried in order (first success wins)
# web_safari  → HLS manifests 144p-1080p, NO cookies needed ✓
# mediaconnect → adaptive mp4 streams, NO cookies needed ✓
YOUTUBE_PLAYER_CLIENTS = [
    ['web_safari'],
    ['mediaconnect'],
    ['mweb'],
    ['web_creator'],
    ['ios'],
    ['android'],
    ['web'],
]

# Clients that return HLS manifests instead of direct mp4
HLS_CLIENTS = {'web_safari', 'mediaconnect'}


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def is_youtube_url(url: str) -> bool:
    domain = urlparse(url).netloc.lower().lstrip('www.')
    return any(domain == d or domain.endswith('.' + d) for d in YOUTUBE_DOMAINS)


def _extract_youtube_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from any YouTube URL format."""
    patterns = [
        r'(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})',
        r'^([A-Za-z0-9_-]{11})$',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _guess_type(url: str) -> str:
    url_l = url.lower()
    if '.m3u8' in url_l:
        return 'HLS'
    if '.mpd' in url_l:
        return 'DASH'
    if '.mp4' in url_l:
        return 'MP4'
    if '.webm' in url_l:
        return 'WEBM'
    return 'STREAM'


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _write_cookie_file(url: str, cookies_str: str) -> Optional[str]:
    """Convert a cookie string (name=val; name2=val2) to a Netscape cookie file for yt-dlp."""
    try:
        domain = urlparse(url).netloc
        lines = ["# Netscape HTTP Cookie File\n"]
        for part in cookies_str.split(';'):
            part = part.strip()
            if '=' in part:
                name, val = part.split('=', 1)
                # domain, include_subdomains, path, secure, expiry, name, value
                lines.append(
                    f"{domain}\tTRUE\t/\tFALSE\t0\t{name.strip()}\t{val.strip()}\n"
                )
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.writelines(lines)
        tmp.close()
        return tmp.name
    except Exception:
        return None


# ---------------------------------------------------------------------------
# yt-dlp core helpers
# ---------------------------------------------------------------------------

def _build_ydl_opts(quality_pref: str, cookies_file: str,
                    extra_args: dict = None, hls_mode: bool = False) -> dict:
    """Build yt-dlp options dict."""
    if hls_mode:
        fmt = YTDLP_QUALITY_MAP_HLS.get(quality_pref, YTDLP_QUALITY_MAP_HLS['best'])
    else:
        fmt = YTDLP_QUALITY_MAP.get(quality_pref, YTDLP_QUALITY_MAP['best'])

    opts = {
        'format':         fmt,
        'quiet':          True,
        'no_warnings':    True,
        'skip_download':  True,
        'noplaylist':     True,
        'extract_flat':   False,
        'socket_timeout': 30,
        'retries':        2,
        'logtostderr':    False,
        'http_headers': {
            'User-Agent':      BROWSER_UA,
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }
    if cookies_file and os.path.exists(cookies_file):
        opts['cookiefile'] = cookies_file
    if extra_args:
        opts.update(extra_args)
    return opts


def _parse_ytdlp_info(info: dict, url: str) -> dict:
    """Convert a yt-dlp info dict into standard result format."""
    formats = info.get('formats', [])
    all_urls: List[Dict] = []

    for f in formats:
        f_url = f.get('url', '')
        if not f_url or f_url.startswith('blob:'):
            continue
        height = f.get('height')
        all_urls.append({
            "url":       f_url,
            "format_id": f.get('format_id', ''),
            "ext":       f.get('ext', ''),
            "quality":   f"{height}p" if height else f.get('format_note', '?'),
            "vcodec":    f.get('vcodec', 'none'),
            "acodec":    f.get('acodec', 'none'),
            "filesize":  f.get('filesize') or f.get('filesize_approx'),
            "tbr":       f.get('tbr'),
        })

    def _sort_key(f):
        hv = f['vcodec'] != 'none'
        ha = f['acodec'] != 'none'
        h  = int(f['quality'].replace('p', '')) \
             if f['quality'].endswith('p') and f['quality'][:-1].isdigit() else 0
        return (hv and ha, hv, h)

    all_urls.sort(key=_sort_key, reverse=True)

    best_fmt      = all_urls[0] if all_urls else {}
    best_url      = best_fmt.get('url', '')
    requested_url = info.get('url', '') or best_url
    manifest_url  = info.get('manifest_url', '')
    thumbs = [t.get('url', '') for t in info.get('thumbnails', []) if t.get('url')]

    return {
        "success":          True,
        "best_link":        requested_url or manifest_url or best_url,
        "type":             _guess_type(requested_url or best_url),
        "quality":          best_fmt.get('quality', 'unknown'),
        "is_full_video":    True,
        "is_paywalled":     False,
        "access_type":      "public",
        "title":            info.get('title', ''),
        "duration_seconds": info.get('duration'),
        "uploader":         info.get('uploader') or info.get('channel', ''),
        "view_count":       info.get('view_count'),
        "thumbnail":        thumbs[-1] if thumbs else info.get('thumbnail', ''),
        "all_formats":      all_urls,
        "manifest_url":     manifest_url,
        "site":             info.get('extractor_key', 'unknown').lower(),
        "webpage_url":      info.get('webpage_url', url),
    }


def _ytdlp_extract(url: str, quality_pref: str = 'best',
                   cookies_str: str = '', cookies_file: str = '',
                   player_clients: List[List[str]] = None) -> Dict:
    """
    Core yt-dlp extraction — tries multiple player clients in order.
    Returns standard result dict on success or failure.
    """
    if not YTDLP_AVAILABLE:
        return {"success": False, "error": "yt-dlp not installed. Run: pip install yt-dlp"}

    _tmp_cookie_file = None
    try:
        # Build cookie file from string if provided
        if not cookies_file and cookies_str:
            _tmp_cookie_file = _write_cookie_file(url, cookies_str)
            if _tmp_cookie_file:
                cookies_file = _tmp_cookie_file

        clients_to_try = player_clients or [None]  # None = yt-dlp default
        last_error = ''

        for client in clients_to_try:
            extra = {}
            client_name = client[0] if client else ''
            if client:
                extra['extractor_args'] = {'youtube': {'player_client': client}}

            use_hls = client_name in HLS_CLIENTS
            opts = _build_ydl_opts(quality_pref, cookies_file, extra, hls_mode=use_hls)

            try:
                old_stderr = sys.stderr
                sys.stderr = io.StringIO()
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                finally:
                    sys.stderr = old_stderr

                if info:
                    result = _parse_ytdlp_info(info, url)
                    result['player_client'] = client_name or 'default'
                    return result

            except yt_dlp.utils.DownloadError as e:
                last_error = str(e)
                if 'bot' in last_error.lower() and not cookies_file and not use_hls:
                    continue
                continue
            except Exception as e:
                last_error = str(e)
                continue

        is_bot_blocked = 'bot' in last_error.lower()
        is_private = any(k in last_error.lower() for k in
                         ('private', 'login', 'sign in', 'age', 'unavailable', 'members only'))
        return {
            "success":       False,
            "error":         last_error,
            "is_paywalled":  is_bot_blocked or is_private,
            "needs_cookies": is_bot_blocked or is_private,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "is_paywalled": False}
    finally:
        if _tmp_cookie_file and os.path.exists(_tmp_cookie_file):
            try:
                os.unlink(_tmp_cookie_file)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# YouTube oEmbed metadata (fallback when streams are blocked)
# ---------------------------------------------------------------------------

def _youtube_oembed_meta(video_id: str) -> Dict:
    """
    Fetch basic metadata via YouTube oEmbed API — no auth needed.
    Used as enrichment when direct stream extraction is blocked.
    """
    if not CURL_AVAILABLE:
        return {}
    try:
        r = curl_requests.get(
            f"https://www.youtube.com/oembed?url=https://youtu.be/{video_id}&format=json",
            headers=BROWSER_HEADERS, timeout=10, impersonate="chrome124",
        )
        if r.status_code == 200:
            d = r.json()
            return {
                "title":     d.get("title", ""),
                "uploader":  d.get("author_name", ""),
                "thumbnail": d.get("thumbnail_url", ""),
            }
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Main YouTube scraper
# ---------------------------------------------------------------------------

def scrape_youtube(input_url: str, quality_pref: str = 'best',
                   cookies_str: str = '', cookies_file: str = '') -> Dict:
    """
    Extract YouTube video stream URLs without downloading.

    Bypass strategy (tried in order):
      1. web_safari client   → HLS manifests 144p-1080p, no cookies needed
      2. mediaconnect client → adaptive mp4 streams, no cookies needed
      3. mweb / web_creator / ios / android / web → region/video-specific fallbacks
      4. With cookies        → full quality guaranteed, all formats available

    Returns dict with:
      success          → bool
      best_link        → best stream URL (HLS manifest or direct mp4)
      type             → "HLS" | "MP4" | "WEBM" | "STREAM"
      quality          → e.g. "1080p"
      quality_menu     → {"1080p": url, "720p": url, ...}
      title            → video title
      uploader         → channel name
      thumbnail        → thumbnail URL
      duration_seconds → video duration
      view_count       → view count
      player_client    → which yt-dlp client succeeded
      all_formats      → full list of available streams
    """
    if not YTDLP_AVAILABLE:
        return {"success": False, "error": "yt-dlp not installed", "site": "youtube"}

    video_id = _extract_youtube_id(input_url)

    result = _ytdlp_extract(
        input_url, quality_pref, cookies_str, cookies_file,
        player_clients=YOUTUBE_PLAYER_CLIENTS,
    )

    result["site"]     = "youtube"
    result["video_id"] = video_id

    # Failure path — enrich with oEmbed metadata where possible
    if not result["success"]:
        meta = _youtube_oembed_meta(video_id) if video_id else {}
        result.update(meta)

        if result.get("needs_cookies"):
            result["error"] = (
                "All bypass methods failed — YouTube is blocking datacenter IP. "
                "Provide browser cookies to force extraction."
            )
            result["how_to_fix"] = {
                "step1": "Install 'Get cookies.txt LOCALLY' Chrome/Firefox extension",
                "step2": "Visit youtube.com (logged in) → click extension → Export Cookies",
                "step3": "python bot.py <url> --cookies-file /path/to/cookies.txt",
            }
        return result

    # Success path — build quality menu and classify stream type
    all_fmts    = result.get("all_formats", [])
    client_used = result.get("player_client", "")

    combined   = [f for f in all_fmts if f["vcodec"] != "none" and f["acodec"] != "none"]
    video_only = [f for f in all_fmts if f["vcodec"] != "none" and f["acodec"] == "none"]
    audio_only = [f for f in all_fmts if f["acodec"] != "none" and f["vcodec"] == "none"]

    # Quality menu from combined (HLS) streams first, fall back to video-only
    quality_menu: Dict[str, str] = {}
    for f in (combined or video_only):
        q = f.get("quality", "")
        if q and q not in quality_menu:
            quality_menu[q] = f["url"]

    best_combined   = combined[0]["url"]   if combined   else None
    best_video_only = video_only[0]["url"] if video_only else None
    best_audio_only = audio_only[0]["url"] if audio_only else None

    best_link    = result.get("best_link", "")
    manifest_url = result.get("manifest_url", "")
    if manifest_url and not best_link:
        best_link = manifest_url

    stream_type = "HLS" if (
        "manifest.googlevideo.com" in best_link or
        ".m3u8" in best_link or
        client_used in ("web_safari",)
    ) else _guess_type(best_link)

    result["best_link"]           = best_combined or best_link or manifest_url
    result["type"]                = stream_type
    result["best_combined_url"]   = best_combined
    result["best_video_only_url"] = best_video_only
    result["best_audio_only_url"] = best_audio_only
    result["quality_menu"]        = quality_menu
    result["note"] = (
        "HLS manifest — playable in VLC/ffmpeg/any HLS player. "
        "To download: yt-dlp <url>  or  ffmpeg -i <manifest_url> -c copy out.mp4"
    ) if stream_type == "HLS" else None

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args:
        print(json.dumps({
            "success": False,
            "error": (
                "Usage: python bot.py <youtube_url> "
                "[--cookies \"name=val; name2=val2\"] "
                "[--cookies-file /path/to/cookies.txt] "
                "[--quality best|2160p|1080p|720p|480p|360p|144p|worst]"
            )
        }, indent=2))
        sys.exit(1)

    url = args[0]

    if not is_youtube_url(url):
        print(json.dumps({
            "success": False,
            "error": f"Not a YouTube URL: {url}",
            "supported_domains": list(YOUTUBE_DOMAINS),
        }, indent=2))
        sys.exit(1)

    cookies_str  = ''
    cookies_file = ''
    quality_pref = 'best'

    if '--cookies' in args:
        idx = args.index('--cookies')
        if idx + 1 < len(args):
            cookies_str = args[idx + 1]

    if '--cookies-file' in args:
        idx = args.index('--cookies-file')
        if idx + 1 < len(args):
            cookies_file = args[idx + 1]

    if '--quality' in args:
        idx = args.index('--quality')
        if idx + 1 < len(args):
            quality_pref = args[idx + 1]

    try:
        result = scrape_youtube(url, quality_pref, cookies_str, cookies_file)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0 if result.get("success") else 1)
    except Exception as e:
        print(json.dumps({
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
