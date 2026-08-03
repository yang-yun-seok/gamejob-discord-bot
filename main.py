from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal
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
DEFAULT_DETAIL_TIMEOUT_SECONDS = 10
DEFAULT_DETAIL_FAILURE_LIMIT = 3
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
MODIFIED_LABEL = "\uc218\uc815"
NotificationKind = Literal["new", "updated"]

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
    modified_at: str = ""


class SentJobStore:
    def __init__(self, path: str | Path, limit: int) -> None:
        self.path = Path(path)
        self.limit = limit
        self._ordered_ids, self._modified_at = self._load()
        self._known_ids = set(self._ordered_ids)

    @property
    def is_empty(self) -> bool:
        return not self._ordered_ids

    def contains(self, job_id: str) -> bool:
        return job_id in self._known_ids

    def modified_at(self, job_id: str) -> str:
        return self._modified_at.get(job_id, "")

    def remember(self, job_id: str, modified_at: str = "") -> None:
        if job_id in self._known_ids:
            self._ordered_ids.remove(job_id)

        self._ordered_ids.append(job_id)
        self._known_ids.add(job_id)
        self._modified_at[job_id] = modified_at

        if len(self._ordered_ids) > self.limit:
            overflow = len(self._ordered_ids) - self.limit
            removed_ids = self._ordered_ids[:overflow]
            self._ordered_ids = self._ordered_ids[overflow:]
            for removed_id in removed_ids:
                if removed_id not in self._ordered_ids:
                    self._known_ids.discard(removed_id)
                    self._modified_at.pop(removed_id, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "jobs": [
                {
                    "job_id": job_id,
                    "modified_at": self._modified_at.get(job_id, ""),
                }
                for job_id in self._ordered_ids
            ],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(self.path)

    def _load(self) -> tuple[list[str], dict[str, str]]:
        if not self.path.exists():
            return [], {}

        content = self.path.read_text(encoding="utf-8").strip()
        if not content:
            return [], {}

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raw_lines = [line.strip() for line in content.splitlines() if line.strip()]
            ordered_ids = dedupe_preserve_latest(raw_lines)[-self.limit :]
            return ordered_ids, {job_id: "" for job_id in ordered_ids}

        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise RuntimeError(f"Invalid job state file format: {self.path}")

        states: dict[str, str] = {}
        for item in payload["jobs"]:
            if not isinstance(item, dict):
                continue

            job_id = str(item.get("job_id", "")).strip()
            if not job_id:
                continue

            modified_at = str(item.get("modified_at", "")).strip()
            states.pop(job_id, None)
            states[job_id] = modified_at

        limited_states = list(states.items())[-self.limit :]
        return [job_id for job_id, _ in limited_states], dict(limited_states)


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


def create_session(retry_count: int = 3) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=retry_count,
        connect=retry_count,
        read=retry_count,
        status=retry_count,
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


def parse_job_modified_at(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    date_values = [
        clean_text(node.get_text(" ", strip=True))
        for node in soup.select(".recruit-data-ddyytt p.date")
    ]

    for value in date_values:
        if not value.endswith(MODIFIED_LABEL):
            continue

        match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", value)
        if match:
            return match.group(0)

    return ""


def fetch_job_modified_at(
    session: requests.Session,
    job: JobPost,
    timeout_seconds: int,
) -> str:
    response = session.get(job.link, timeout=timeout_seconds)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return parse_job_modified_at(response.text)


def enrich_job_modified_dates(
    session: requests.Session,
    posts: list[JobPost],
    timeout_seconds: int,
    failure_limit: int,
) -> list[JobPost]:
    enriched_posts: list[JobPost] = []
    consecutive_failures = 0
    tracking_paused = False

    for post in posts:
        if tracking_paused:
            enriched_posts.append(post)
            continue

        try:
            modified_at = fetch_job_modified_at(session, post, timeout_seconds)
            if not modified_at:
                raise RuntimeError("The detail page did not expose a modification date.")
        except Exception as exc:
            consecutive_failures += 1
            logging.warning(
                "Could not read modification date for job %s: %s",
                post.job_id,
                exc,
            )
            enriched_posts.append(post)

            if consecutive_failures >= failure_limit:
                tracking_paused = True
                logging.warning(
                    "Paused modification-date checks for the remaining jobs after "
                    "%s consecutive detail-page failures.",
                    consecutive_failures,
                )
            continue

        consecutive_failures = 0
        enriched_posts.append(replace(post, modified_at=modified_at))

    return enriched_posts


def classify_notification(
    job: JobPost,
    store: SentJobStore,
) -> NotificationKind | None:
    if not store.contains(job.job_id):
        return "new"

    previous_modified_at = store.modified_at(job.job_id)
    if (
        previous_modified_at
        and job.modified_at
        and previous_modified_at != job.modified_at
    ):
        return "updated"

    return None


def build_discord_payload(
    job: JobPost,
    notification_kind: NotificationKind,
    previous_modified_at: str = "",
) -> dict:
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

    if job.modified_at:
        modified_value = job.modified_at
        if notification_kind == "updated" and previous_modified_at:
            modified_value = f"{previous_modified_at} -> {job.modified_at}"
        fields.append(
            {
                "name": "Last modified",
                "value": clip_text(modified_value, 1024),
                "inline": True,
            }
        )

    if notification_kind == "updated":
        title_prefix = "\U0001f504 [\uac31\uc2e0 \uacf5\uace0]"
        color = 0x3498DB
    else:
        title_prefix = "\U0001f195 [\uc2e0\uaddc \uacf5\uace0]"
        color = 0xFFAA33

    return {
        "embeds": [
            {
                "title": clip_text(f"{title_prefix} {job.title}", 256),
                "url": job.link,
                "color": color,
                "fields": fields,
                "footer": {"text": f"Collected: {collected_at}"},
            }
        ]
    }


def send_to_discord(
    session: requests.Session,
    webhook_url: str,
    job: JobPost,
    notification_kind: NotificationKind,
    previous_modified_at: str,
    timeout_seconds: int,
    max_retries: int,
) -> None:
    payload = build_discord_payload(job, notification_kind, previous_modified_at)
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
        store.remember(post.job_id, post.modified_at)
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
    detail_timeout_seconds = get_int_env(
        "DETAIL_TIMEOUT_SECONDS", DEFAULT_DETAIL_TIMEOUT_SECONDS
    )
    detail_failure_limit = get_int_env(
        "DETAIL_FAILURE_LIMIT", DEFAULT_DETAIL_FAILURE_LIMIT
    )
    seed_only_on_first_run = get_bool_env("SEED_ONLY_ON_FIRST_RUN", default=False)

    store = SentJobStore(state_file, state_limit)
    session = create_session()
    detail_session = create_session(retry_count=0)

    logging.info("Fetching jobs from %s", target_url)
    posts = fetch_job_posts(session, target_url, timeout_seconds)
    logging.info("Fetched %s jobs from the current page.", len(posts))
    posts = enrich_job_modified_dates(
        detail_session,
        posts,
        detail_timeout_seconds,
        detail_failure_limit,
    )
    logging.info(
        "Resolved exact modification dates for %s/%s jobs.",
        sum(bool(post.modified_at) for post in posts),
        len(posts),
    )

    if bootstrap_if_needed(posts, store, seed_only_on_first_run):
        return 0

    new_sent_count = 0
    updated_sent_count = 0
    failed_count = 0
    unchanged_count = 0
    baseline_count = 0
    state_changed = False

    for job in reversed(posts):
        previous_modified_at = store.modified_at(job.job_id)
        notification_kind = classify_notification(job, store)

        if notification_kind is None:
            unchanged_count += 1
            if store.contains(job.job_id) and not previous_modified_at and job.modified_at:
                store.remember(job.job_id, job.modified_at)
                baseline_count += 1
                state_changed = True
            continue

        try:
            send_to_discord(
                session,
                webhook_url,
                job,
                notification_kind,
                previous_modified_at,
                timeout_seconds,
                max_retries,
            )
        except Exception:
            failed_count += 1
            logging.exception(
                "Failed to deliver %s job %s (%s)",
                notification_kind,
                job.job_id,
                job.title,
            )
            continue

        store.remember(job.job_id, job.modified_at)
        store.save()
        state_changed = False

        if notification_kind == "updated":
            updated_sent_count += 1
        else:
            new_sent_count += 1

        logging.info(
            "Delivered %s job %s (%s)",
            notification_kind,
            job.job_id,
            job.title,
        )

    if state_changed:
        store.save()

    if not new_sent_count and not updated_sent_count and not failed_count:
        logging.info("No new or updated jobs found.")

    logging.info(
        "Run finished. new=%s updated=%s failed=%s unchanged=%s baselined=%s",
        new_sent_count,
        updated_sent_count,
        failed_count,
        unchanged_count,
        baseline_count,
    )

    return 1 if failed_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception("Bot execution failed: %s", exc)
        raise SystemExit(1) from exc
