from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.gamejob.co.kr"
DEFAULT_STATE_FILE = "sent_jobs.txt"
DEFAULT_STATE_LIMIT = 500
DEFAULT_TIMEOUT_SECONDS = 40
DEFAULT_DISCORD_RETRIES = 3
JOB_CONTAINER_SELECTORS = (
    "table.tblList tbody tr",
    ".list .devItem",
    ".devItem",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NO_RESULTS_MARKER = "\uac80\uc0c9\uacb0\uacfc\uac00 \uc5c6\uc2b5\ub2c8\ub2e4"

try:
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class JobPost:
    job_id: str
    title: str
    company: str
    link: str
    info: tuple[str, ...]
    deadline: str
    posted_at: str


class SentJobStore:
    def __init__(self, path: str | Path, limit: int) -> None:
        self.path = Path(path)
        self.limit = limit
        self._ordered_ids = self._load()
        self._known_ids = set(self._ordered_ids)

    @property
    def is_empty(self) -> bool:
        return not self._ordered_ids

    def contains(self, job_id: str) -> bool:
        return job_id in self._known_ids

    def remember(self, job_id: str) -> None:
        if job_id in self._known_ids:
            return

        self._ordered_ids.append(job_id)
        self._known_ids.add(job_id)

        if len(self._ordered_ids) > self.limit:
            overflow = len(self._ordered_ids) - self.limit
            removed_ids = self._ordered_ids[:overflow]
            self._ordered_ids = self._ordered_ids[overflow:]
            for removed_id in removed_ids:
                if removed_id not in self._ordered_ids:
                    self._known_ids.discard(removed_id)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(self._ordered_ids)
        if content:
            content += "\n"

        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(self.path)

    def _load(self) -> list[str]:
        if not self.path.exists():
            return []

        raw_lines = [
            line.strip()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return dedupe_preserve_latest(raw_lines)[-self.limit :]


def dedupe_preserve_latest(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    for item in reversed(list(items)):
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)

    ordered.reverse()
    return ordered


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    raise RuntimeError(f"Required environment variable is missing: {name}")


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc

    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")

    return parsed


def get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clip_text(value: str, limit: int, suffix: str = "...") -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value or "-"
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def extract_job_id(link: str) -> str:
    parsed = urlparse(link)
    query = parse_qs(parsed.query)

    for key in ("GI_No", "gi_no", "gi_no[]"):
        values = query.get(key)
        if values:
            return values[0]

    match = re.search(r"[?&]GI_No=(\d+)", link, flags=re.IGNORECASE)
    if match:
        return match.group(1)

    return link


def extract_text(node: BeautifulSoup | None, selector: str) -> str:
    if node is None:
        return ""

    target = node.select_one(selector)
    if target is None:
        return ""

    return clean_text(target.get_text(" ", strip=True))


def extract_info_values(node: BeautifulSoup) -> tuple[str, ...]:
    info_values = tuple(
        clean_text(span.get_text(" ", strip=True))
        for span in node.select("p.info span")
        if clean_text(span.get_text(" ", strip=True))
    )
    if info_values:
        return info_values

    desc_text = extract_text(node, ".desc")
    if desc_text:
        return (desc_text,)

    return ()


def parse_job_post(node: BeautifulSoup) -> JobPost | None:
    link_tag = node.select_one(
        "div.tit > a[href*='/Recruit/GI_Read/View'], "
        ".tit a[href*='/Recruit/GI_Read/View'], "
        ".tit a[href*='GI_No=']"
    )
    if link_tag is None:
        return None

    href = clean_text(link_tag.get("href", ""))
    if not href:
        return None

    link = urljoin(BASE_URL, href)
    title = clean_text(link_tag.get_text(" ", strip=True))
    company = (
        extract_text(node, "div.company strong")
        or extract_text(node, ".coName")
        or extract_text(node, "a[href*='/Company/Detail'] strong")
        or "-"
    )
    info = extract_info_values(node)
    deadline = extract_text(node, "span.date") or "-"
    posted_at = extract_text(node, "span.modifyDate") or "-"

    return JobPost(
        job_id=extract_job_id(link),
        title=title or "-",
        company=company,
        link=link,
        info=info,
        deadline=deadline,
        posted_at=posted_at,
    )


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_job_posts(
    session: requests.Session,
    target_url: str,
    timeout_seconds: int,
) -> list[JobPost]:
    response = session.get(target_url, timeout=timeout_seconds)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding

    soup = BeautifulSoup(response.text, "html.parser")
    job_nodes = []
    for selector in JOB_CONTAINER_SELECTORS:
        nodes = soup.select(selector)
        if nodes:
            job_nodes = nodes
            break

    if not job_nodes:
        if NO_RESULTS_MARKER in soup.get_text(" ", strip=True):
            return []
        raise RuntimeError(
            "Could not find job postings on the page. "
            "The page structure may have changed or the target URL is invalid."
        )

    posts: list[JobPost] = []
    seen_ids: set[str] = set()

    for node in job_nodes:
        post = parse_job_post(node)
        if post is None:
            continue

        if post.job_id in seen_ids:
            continue

        posts.append(post)
        seen_ids.add(post.job_id)

    return posts


def build_discord_payload(job: JobPost) -> dict:
    collected_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    info_text = "\n".join(job.info[:5]) if job.info else "-"

    fields = [
        {"name": "Company", "value": clip_text(job.company, 1024), "inline": True},
        {"name": "Deadline", "value": clip_text(job.deadline, 1024), "inline": True},
        {"name": "Details", "value": clip_text(info_text, 1024), "inline": False},
    ]

    if job.posted_at and job.posted_at != "-":
        fields.append(
            {"name": "Posted", "value": clip_text(job.posted_at, 1024), "inline": True}
        )

    return {
        "embeds": [
            {
                "title": clip_text(f"[New Job] {job.title}", 256),
                "url": job.link,
                "color": 0xFFAA33,
                "fields": fields,
                "footer": {"text": f"Collected: {collected_at}"},
            }
        ]
    }


def send_to_discord(
    session: requests.Session,
    webhook_url: str,
    job: JobPost,
    timeout_seconds: int,
    max_retries: int,
) -> None:
    payload = build_discord_payload(job)
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = session.post(webhook_url, json=payload, timeout=timeout_seconds)

            if response.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("retry_after", 1))
                except ValueError:
                    retry_after = 1.0
                time.sleep(min(max(retry_after, 1.0), 30.0))
                continue

            response.raise_for_status()
            return
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_retries:
                break
            time.sleep(min(attempt * 2, 10))

    raise RuntimeError(f"Failed to send job {job.job_id} to Discord.") from last_error


def bootstrap_if_needed(
    posts: list[JobPost],
    store: SentJobStore,
    seed_only_on_first_run: bool,
) -> bool:
    if not seed_only_on_first_run or not store.is_empty:
        return False

    for post in reversed(posts):
        store.remember(post.job_id)
    store.save()

    logging.info(
        "Seeded %s existing jobs into %s without sending notifications.",
        len(posts),
        store.path,
    )
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    webhook_url = get_required_env("DISCORD_WEBHOOK_URL")
    target_url = get_required_env("GAMEJOB_TARGET_URL")
    state_file = os.getenv("STATE_FILE", DEFAULT_STATE_FILE)
    state_limit = get_int_env("STATE_LIMIT", DEFAULT_STATE_LIMIT)
    timeout_seconds = get_int_env("REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    max_retries = get_int_env("DISCORD_MAX_RETRIES", DEFAULT_DISCORD_RETRIES)
    seed_only_on_first_run = get_bool_env("SEED_ONLY_ON_FIRST_RUN", default=False)

    store = SentJobStore(state_file, state_limit)
    session = create_session()

    logging.info("Fetching jobs from %s", target_url)
    posts = fetch_job_posts(session, target_url, timeout_seconds)
    logging.info("Fetched %s jobs from the current page.", len(posts))

    if bootstrap_if_needed(posts, store, seed_only_on_first_run):
        return 0

    new_posts = [post for post in posts if not store.contains(post.job_id)]
    if not new_posts:
        logging.info("No new jobs found.")
        return 0

    sent_count = 0
    failed_count = 0

    for job in reversed(new_posts):
        try:
            send_to_discord(session, webhook_url, job, timeout_seconds, max_retries)
        except Exception:
            failed_count += 1
            logging.exception("Failed to deliver job %s (%s)", job.job_id, job.title)
            continue

        store.remember(job.job_id)
        store.save()
        sent_count += 1
        logging.info("Delivered job %s (%s)", job.job_id, job.title)

    logging.info(
        "Run finished. sent=%s failed=%s skipped=%s",
        sent_count,
        failed_count,
        len(posts) - len(new_posts),
    )

    return 1 if failed_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception("Bot execution failed: %s", exc)
        raise SystemExit(1) from exc
