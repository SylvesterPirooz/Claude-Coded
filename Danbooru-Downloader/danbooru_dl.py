#!/usr/bin/env python3
"""
Download every original-resolution image from a Danbooru tag search.

Command line:
    python3 danbooru_dl.py "https://danbooru.donmai.us/posts?tags=hatsune_miku+rating%3Ag"
    python3 danbooru_dl.py "..." --out ~/Pictures/danbooru --max 50 --dry-run

Window (GUI):
    python3 danbooru_gui.py

Optional login (lifts some restrictions for Gold+ accounts):
    export DANBOORU_LOGIN=yourname
    export DANBOORU_API_KEY=yourkey      # from your Danbooru profile -> API Key

Only the Python standard library is used.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "danbooru-tag-downloader/1.0"
PAGE_LIMIT = 200          # posts fetched per API request (the API's max)
SITE_PAGE_SIZE = 20       # posts per page on the Danbooru website (its default)
REQUEST_DELAY = 0.5       # seconds between requests, be polite


class DownloadError(Exception):
    pass


def parse_search_url(url: str) -> tuple[str, str]:
    """Return (base_url, tags) from a Danbooru search URL."""
    parsed = urllib.parse.urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise DownloadError("Give the full URL, e.g. https://danbooru.donmai.us/posts?tags=...")
    tags = urllib.parse.parse_qs(parsed.query).get("tags", [""])[0].strip()
    if not tags:
        raise DownloadError("No 'tags=' found in that URL.")
    return f"{parsed.scheme}://{parsed.netloc}", tags


def folder_name_for(tags: str) -> str:
    """Make a safe folder name from the tag string."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", tags).replace(" ", "+")
    return name.strip(". ")[:150] or "danbooru"


def make_auth(login: str | None, api_key: str | None) -> str | None:
    if login and api_key:
        return b64encode(f"{login}:{api_key}".encode()).decode()
    return None


def make_request(url: str, auth: str | None) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if auth:
        req.add_header("Authorization", f"Basic {auth}")
    return req


def get_json(url: str, auth: str | None, log=print, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(make_request(url, auth), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = 5 * attempt
                log(f"  HTTP {e.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            body = e.read().decode(errors="replace")[:300]
            raise DownloadError(f"API error {e.code}: {body}")
        except urllib.error.URLError as e:
            log(f"  Network error ({e.reason}), retrying...")
            time.sleep(5 * attempt)
    raise DownloadError("Giving up after repeated errors.")


def count_posts(base: str, tags: str, auth: str | None, log=print) -> int | None:
    """Total number of posts for the search (None if Danbooru won't say)."""
    try:
        url = f"{base}/counts/posts.json?{urllib.parse.urlencode({'tags': tags})}"
        return get_json(url, auth, log).get("counts", {}).get("posts")
    except (DownloadError, AttributeError):
        return None




# ---------------------------------------------------------------------------
# Speed control
# ---------------------------------------------------------------------------

# name: (parallel downloads, seconds between download starts)
SPEED_PRESETS = {
    "Gentle":   (1, 1.0),
    "Balanced": (3, 0.3),
    "Fast":     (6, 0.15),
}
DEFAULT_PRESET = "Balanced"
MAX_WORKERS = 8
MIN_GAP, MAX_GAP = 0.1, 2.0


class StartLimiter:
    """
    One shared gap between download *starts*, across all parallel downloads.
    Each download calls wait_turn() before starting; no two starts happen
    closer together than `gap` seconds. Transfers themselves aren't slowed.
    """
    def __init__(self, gap: float):
        self.gap = gap
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait_turn(self, should_stop=lambda: False) -> bool:
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_start)
            self._next_start = start_at + self.gap
        # sleep outside the lock (in small steps so Stop stays responsive)
        while (left := start_at - time.monotonic()) > 0:
            if should_stop():
                return False
            time.sleep(min(left, 0.1))
        return not should_stop()


class LiveStats:
    """Counters the GUI reads while a download runs (updated from worker threads)."""
    def __init__(self):
        self._lock = threading.Lock()
        self.total = None          # posts to process (None = unknown)
        self.completed = 0         # posts finished, any outcome
        self.files = 0             # files actually downloaded
        self.bytes = 0             # bytes downloaded so far (updates mid-file)
        self.started = time.monotonic()

    def add_bytes(self, n):
        with self._lock:
            self.bytes += n

    def finish_post(self, downloaded=False):
        with self._lock:
            self.completed += 1
            if downloaded:
                self.files += 1


def download_file(url: str, dest: str, auth: str | None, on_bytes=None) -> None:
    tmp = dest + ".part"
    with urllib.request.urlopen(make_request(url, auth), timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    os.replace(tmp, dest)   # only appears under its real name once complete


def iter_posts(base: str, tags: str, auth: str | None, log=print):
    """
    Yield every post for the search, newest first.
    Uses page=b<id> ("before id") paging, which isn't capped at page 1000
    like numbered pages are for free accounts.
    """
    before_id = None
    page_no = 1
    while True:
        params = {"tags": tags, "limit": PAGE_LIMIT}
        if before_id is not None:
            params["page"] = f"b{before_id}"
        url = f"{base}/posts.json?{urllib.parse.urlencode(params)}"
        posts = get_json(url, auth, log)
        if isinstance(posts, dict):   # error object
            raise DownloadError(f"API error: {posts.get('message', posts)}")
        if not posts:
            return
        log(f"Fetched batch {page_no} of the post list ({len(posts)} posts)")
        yield from posts
        before_id = min(p["id"] for p in posts)
        page_no += 1
        time.sleep(REQUEST_DELAY)   # the pause stays for post-list (API) requests


def run_download(url, out=".", max_posts=0, max_pages=0, dry_run=False, auth=None,
                 workers=SPEED_PRESETS[DEFAULT_PRESET][0],
                 gap=SPEED_PRESETS[DEFAULT_PRESET][1],
                 log=print, live: LiveStats | None = None,
                 should_stop=lambda: False) -> dict:
    """
    Do the whole job. Used by both the command line and the GUI.
      max_posts     - only the first N posts (0 = no limit)
      max_pages     - only the first N website pages, 20 posts each (0 = no limit)
      workers       - how many files download at the same time
      gap           - minimum seconds between any two download starts
      log(msg)      - called with status text (may be called from worker threads)
      live          - optional LiveStats the caller can read for progress/speed
      should_stop() - return True to stop early
    Returns a dict of counts.
    """
    workers = max(1, min(int(workers), MAX_WORKERS))
    gap = max(MIN_GAP, min(float(gap), MAX_GAP))
    live = live or LiveStats()

    base, tags = parse_search_url(url)
    folder = os.path.join(os.path.expanduser(out or "."), folder_name_for(tags))
    if not dry_run:
        os.makedirs(folder, exist_ok=True)
    log(f"Tags:   {tags}\nFolder: {folder}")

    if max_pages:
        page_posts = max_pages * SITE_PAGE_SIZE
        max_posts = min(max_posts, page_posts) if max_posts else page_posts
        log(f"Limit:  first {max_pages} page(s) = {page_posts} posts")
    elif max_posts:
        log(f"Limit:  first {max_posts} posts")

    total = count_posts(base, tags, auth, log)
    if total is not None and max_posts:
        total = min(total, max_posts)
    elif max_posts:
        total = max_posts
    live.total = total
    log(f"Posts:  {total if total is not None else 'unknown'}")
    if not dry_run:
        log(f"Speed:  {workers} at once, {gap:g}s between starts")
    log("")

    stats = {"done": 0, "skipped": 0, "unavailable": 0, "failed": 0, "stopped": False,
             "folder": folder}
    stats_lock = threading.Lock()
    limiter = StartLimiter(gap)
    # Don't queue up thousands of posts ahead of the downloaders.
    slots = threading.BoundedSemaphore(workers * 2)
    live.started = time.monotonic()

    def bump(key):
        with stats_lock:
            stats[key] += 1

    def fetch(n, filename, file_url, dest, size_mb):
        """Runs in a worker thread."""
        try:
            if not limiter.wait_turn(should_stop):
                return                       # stopped while waiting its turn
            log(f"  [{n}] {filename} ({size_mb:.1f} MB)")
            try:
                download_file(file_url, dest, auth, on_bytes=live.add_bytes)
                bump("done")
                live.finish_post(downloaded=True)
            except (urllib.error.URLError, OSError) as e:
                log(f"    failed {filename}: {e}")
                bump("failed")
                live.finish_post()
        finally:
            slots.release()

    seen = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for post in iter_posts(base, tags, auth, log):
            if should_stop():
                break
            if max_posts and seen >= max_posts:
                break
            seen += 1

            # file_url is the original ("view original") file. It's missing for
            # posts that are deleted/banned or restricted to higher account levels.
            file_url = post.get("file_url")
            if not file_url:
                bump("unavailable")
                live.finish_post()
                continue

            ext = post.get("file_ext") or file_url.rsplit(".", 1)[-1]
            filename = f"{post['id']}_{post.get('md5', 'nomd5')}.{ext}"
            dest = os.path.join(folder, filename)
            size_mb = (post.get("file_size") or 0) / 1_048_576

            if os.path.exists(dest):
                bump("skipped")
                live.finish_post()
            elif dry_run:
                log(f"  would get {filename} ({post.get('image_width')}x"
                    f"{post.get('image_height')}, {size_mb:.1f} MB)")
                bump("done")
                live.finish_post()
            else:
                # wait for a free slot, checking Stop while we wait
                while not slots.acquire(timeout=0.2):
                    if should_stop():
                        break
                else:
                    pool.submit(fetch, seen, filename, file_url, dest, size_mb)
                    continue
                break   # Stop was pressed while waiting for a slot
        # leaving the "with" waits for downloads already in progress to finish

    stats["stopped"] = should_stop()
    verb = "would download" if dry_run else "downloaded"
    log(f"\n{'Stopped' if stats['stopped'] else 'Done'}. {verb}: {stats['done']}, "
        f"already had: {stats['skipped']}, unavailable: {stats['unavailable']}, "
        f"failed: {stats['failed']}")
    return stats


def main():
    ap = argparse.ArgumentParser(description="Download all original images from a Danbooru tag search.")
    ap.add_argument("url", help="Danbooru search URL, e.g. https://danbooru.donmai.us/posts?tags=...")
    ap.add_argument("--out", default=".", help="Parent directory for the tag folder (default: current dir)")
    ap.add_argument("--max", type=int, default=0, help="Only the first N posts (0 = all)")
    ap.add_argument("--pages", type=int, default=0,
                    help=f"Only the first N website pages, {SITE_PAGE_SIZE} posts each (0 = all)")
    ap.add_argument("--speed", choices=list(SPEED_PRESETS), default=DEFAULT_PRESET,
                    help="Speed preset (default: %(default)s)")
    ap.add_argument("--workers", type=int, help=f"Parallel downloads, 1-{MAX_WORKERS} (overrides --speed)")
    ap.add_argument("--gap", type=float,
                    help=f"Seconds between download starts, {MIN_GAP}-{MAX_GAP} (overrides --speed)")
    ap.add_argument("--dry-run", action="store_true", help="List what would be downloaded, download nothing")
    args = ap.parse_args()

    workers, gap = SPEED_PRESETS[args.speed]
    if args.workers is not None:
        workers = args.workers
    if args.gap is not None:
        gap = args.gap

    auth = make_auth(os.environ.get("DANBOORU_LOGIN"), os.environ.get("DANBOORU_API_KEY"))
    try:
        run_download(args.url, args.out, max_posts=args.max, max_pages=args.pages,
                     dry_run=args.dry_run, auth=auth, workers=workers, gap=gap)
    except DownloadError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run again to resume; finished files are skipped.")
