from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from validate_and_rank import looks_like_generic_careers_page, score_vacancy, source_coverage, validate_vacancies


def vacancy(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "priority": "A-",
        "company": "Example GmbH",
        "title": "Senior Data Engineer",
        "location": "Remote, Germany",
        "work_mode": "Remote",
        "salary": "Not disclosed",
        "source": "Example board",
        "url": "https://jobs.example.test/role/123",
        "language": "English required",
        "contract_type": "Permanent",
        "core_tech_match": "Azure, Databricks, Python, SQL",
        "fit_score": 8.5,
        "status": "NEW",
        "analysis": "Germany-compatible remote senior data-engineering position.",
        "recommendation": "Apply with a senior data-platform focused CV.",
        "found_at": "2026-08-17T10:00:00+00:00",
        "source_group": "Remote boards",
        "language_risk": "low",
        "salary_likelihood": "high",
    }
    item.update(overrides)
    return item


class ValidationTests(unittest.TestCase):
    def test_rejected_first_copy_does_not_hide_later_valid_copy(self) -> None:
        invalid = vacancy(language="German C1 required", fit_score=5.0)
        valid = vacancy(language="English required", fit_score=8.5)

        accepted, rejects = validate_vacancies([invalid, valid])

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["language"], "English required")
        self.assertEqual(rejects[0]["reason"], "German C1 role without exceptional fit")

    def test_cross_source_mirrors_are_deduplicated(self) -> None:
        first = vacancy(url="https://one.example.test/jobs/senior-data-engineer-123456")
        second = vacancy(
            title="Senior Data Engineer (all genders)",
            source="Another board",
            url="https://two.example.test/jobs/senior-data-engineer-654321",
            source_group="LinkedIn-indexed",
        )

        accepted, rejects = validate_vacancies([first, second])

        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejects[0]["reason"], "duplicate role across sources")

    def test_us_only_remote_role_is_rejected(self) -> None:
        accepted, rejects = validate_vacancies(
            [vacancy(location="Remote, US only", work_mode="Fully remote")]
        )

        self.assertEqual(accepted, [])
        self.assertEqual(rejects[0]["reason"], "remote role restricted outside Germany-compatible locations")

    def test_united_states_remote_role_is_rejected(self) -> None:
        accepted, rejects = validate_vacancies(
            [vacancy(location="Remote, United States", work_mode="Fully remote")]
        )

        self.assertEqual(accepted, [])
        self.assertEqual(rejects[0]["reason"], "remote role restricted outside Germany-compatible locations")

    def test_oracle_data_integration_role_is_accepted_and_ranked(self) -> None:
        oracle_role = vacancy(
            title="Senior Oracle Data Integration Engineer",
            core_tech_match="Oracle EBS, OCI, Oracle ODI, GoldenGate, PL/SQL, ETL",
            analysis="English-friendly Oracle ERP integration and data migration role for Germany.",
        )

        accepted, rejects = validate_vacancies([oracle_role])

        self.assertEqual(rejects, [])
        self.assertEqual(len(accepted), 1)
        self.assertGreater(score_vacancy(accepted[0]), 9.0)

    def test_ai_engineer_role_is_accepted_and_ranked(self) -> None:
        ai_role = vacancy(
            title="Senior AI Engineer",
            core_tech_match="Azure OpenAI, LLM, RAG, MLOps, Python, data pipelines",
            analysis="English-friendly Germany-compatible production AI platform role with LLM applications and model serving.",
        )

        accepted, rejects = validate_vacancies([ai_role])

        self.assertEqual(rejects, [])
        self.assertEqual(len(accepted), 1)
        self.assertGreater(score_vacancy(accepted[0]), 9.0)

    def test_company_careers_requisition_urls_are_not_rejected_as_generic(self) -> None:
        self.assertFalse(looks_like_generic_careers_page("https://www.rhenus.group/karriere/JR122018/"))
        self.assertFalse(looks_like_generic_careers_page("https://www.deichmann-karriere.de/jobs/2026-34530/"))

    def test_nrw_hybrid_company_role_is_germany_compatible(self) -> None:
        local_role = vacancy(
            title="Data Engineer",
            location="Holzwickede, NRW",
            work_mode="Hybrid with mobile work",
            url="https://www.rhenus.group/karriere/JR122018/",
            source_group="Company career pages",
        )

        accepted, rejects = validate_vacancies([local_role])

        self.assertEqual(rejects, [])
        self.assertEqual(len(accepted), 1)

    def test_coverage_uses_exact_source_group_names(self) -> None:
        coverage = source_coverage([vacancy(source_group="Remote boards")])
        outcomes = {item["source_group"]: item["outcome"] for item in coverage}

        self.assertEqual(outcomes["Remote boards"], "strong matches found")
        self.assertEqual(outcomes["Recruiters"], "not represented in validated output")


if __name__ == "__main__":
    unittest.main()
