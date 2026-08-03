import json
import tempfile
import unittest
from pathlib import Path

from main import (
    JobPost,
    SentJobStore,
    build_discord_payload,
    classify_notification,
    parse_job_modified_at,
)


def make_job(job_id: str = "123", modified_at: str = "2026-08-03 10:10") -> JobPost:
    return JobPost(
        job_id=job_id,
        title="Game Designer",
        company="Example Studio",
        link=f"https://www.gamejob.co.kr/Recruit/GI_Read/View?GI_No={job_id}",
        info=("Career", "Seoul"),
        deadline="~08/31",
        posted_at="10 minutes ago",
        modified_at=modified_at,
    )


class SentJobStoreTests(unittest.TestCase):
    def test_migrates_legacy_ids_and_persists_modification_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "sent_jobs.txt"
            state_path.write_text("100\n200\n100\n", encoding="utf-8")

            store = SentJobStore(state_path, limit=10)
            self.assertTrue(store.contains("100"))
            self.assertTrue(store.contains("200"))
            self.assertEqual(store.modified_at("100"), "")

            store.remember("100", "2026-08-03 10:10")
            store.save()

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["jobs"][-1]["job_id"], "100")

            reloaded = SentJobStore(state_path, limit=10)
            self.assertEqual(reloaded.modified_at("100"), "2026-08-03 10:10")


class ModificationTrackingTests(unittest.TestCase):
    def test_parses_exact_modification_date_from_detail_page(self) -> None:
        html = """
        <div class="recruit-data-ddyytt flex align-item">
          <p class="date">2026-08-03 09:10 \ub4f1\ub85d</p>
          <p class="date">2026-08-03 10:10 \uc218\uc815</p>
        </div>
        """
        self.assertEqual(parse_job_modified_at(html), "2026-08-03 10:10")

    def test_classifies_new_updated_and_unchanged_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SentJobStore(Path(temp_dir) / "state.txt", limit=10)
            store.remember("123", "2026-08-03 10:10")

            self.assertIsNone(classify_notification(make_job(), store))
            self.assertEqual(
                classify_notification(make_job(modified_at="2026-08-03 11:20"), store),
                "updated",
            )
            self.assertEqual(classify_notification(make_job(job_id="456"), store), "new")

            store.remember("789")
            self.assertIsNone(
                classify_notification(make_job(job_id="789"), store),
                "Legacy state should establish a baseline without a false update alert.",
            )

    def test_builds_distinct_discord_notifications(self) -> None:
        job = make_job(modified_at="2026-08-03 11:20")

        new_embed = build_discord_payload(job, "new")["embeds"][0]
        updated_embed = build_discord_payload(
            job,
            "updated",
            previous_modified_at="2026-08-03 10:10",
        )["embeds"][0]

        self.assertTrue(new_embed["title"].startswith("\U0001f195 [\uc2e0\uaddc \uacf5\uace0]"))
        self.assertTrue(updated_embed["title"].startswith("\U0001f504 [\uac31\uc2e0 \uacf5\uace0]"))
        modified_field = next(
            field for field in updated_embed["fields"] if field["name"] == "Last modified"
        )
        self.assertEqual(
            modified_field["value"],
            "2026-08-03 10:10 -> 2026-08-03 11:20",
        )


if __name__ == "__main__":
    unittest.main()
