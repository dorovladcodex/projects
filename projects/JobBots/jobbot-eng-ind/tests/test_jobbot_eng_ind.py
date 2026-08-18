from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobbot_eng_ind import add_new_items_to_state, render_report, vacancy_category, vacancy_key


class JobBotStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vacancy = {
            "company": "Example GmbH",
            "title": "Senior Data Engineer (m/f/d)",
            "location": "Berlin, Germany",
            "url": "https://jobs.example.test/123",
        }
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    def test_role_identity_ignores_gender_suffix_and_location(self) -> None:
        mirrored = {
            **self.vacancy,
            "title": "Senior Data Engineer (all genders)",
            "location": "Remote, Germany",
            "url": "https://board.example.test/job/456",
        }
        self.assertEqual(vacancy_key(self.vacancy), vacancy_key(mirrored))

        starred_gender_suffix = {**self.vacancy, "title": "Senior Data Engineer (m/f/*)"}
        self.assertEqual(vacancy_key(self.vacancy), vacancy_key(starred_gender_suffix))

    def test_legacy_state_entries_are_recognized(self) -> None:
        legacy_key = "example gmbh|senior data engineer (m/f/d)|berlin, germany|https://jobs.example.test/123"
        state = {legacy_key: self.vacancy}
        report, new_items, watch_items = render_report([self.vacancy], state, self.now, new_only=True)
        self.assertEqual(new_items, [])
        self.assertEqual(watch_items, [self.vacancy])
        self.assertIn("New matches in this run: 0", report)

    def test_state_changes_only_when_commit_function_is_called(self) -> None:
        state: dict[str, object] = {}
        render_report([self.vacancy], state, self.now, new_only=True)
        self.assertEqual(state, {})

        add_new_items_to_state(state, [self.vacancy], self.now)
        self.assertIn(vacancy_key(self.vacancy), state)

    def test_report_groups_data_ai_and_oracle_roles(self) -> None:
        oracle_role = {
            **self.vacancy,
            "company": "Oracle Example GmbH",
            "title": "Senior Oracle Data Integration Engineer",
            "core_tech_match": "Oracle EBS, PL/SQL, Oracle ODI",
        }
        data_role = {
            **self.vacancy,
            "company": "Data Example GmbH",
            "title": "Senior Data Engineer",
            "core_tech_match": "Azure Databricks, PySpark, SQL",
        }
        ai_role = {
            **self.vacancy,
            "company": "AI Example GmbH",
            "title": "Senior AI Engineer",
            "core_tech_match": "Azure OpenAI, LLM, RAG, Python",
        }

        report, new_items, _ = render_report([oracle_role, data_role, ai_role], {}, self.now, new_only=True)

        self.assertEqual(new_items, [oracle_role, data_role, ai_role])
        self.assertLess(report.index("Data Engineering"), report.index("AI Engineering"))
        self.assertLess(report.index("AI Engineering"), report.index("Oracle / Enterprise Data"))
        self.assertLess(report.index("Data Example GmbH"), report.index("AI Example GmbH"))
        self.assertLess(report.index("AI Example GmbH"), report.index("Oracle Example GmbH"))

    def test_oracle_category_takes_precedence_over_ai_terms(self) -> None:
        hybrid_role = {
            **self.vacancy,
            "title": "AI Integration Engineer",
            "core_tech_match": "Oracle EBS, Azure OpenAI, LLM",
        }

        self.assertEqual(vacancy_category(hybrid_role), "Oracle / Enterprise Data")


if __name__ == "__main__":
    unittest.main()
