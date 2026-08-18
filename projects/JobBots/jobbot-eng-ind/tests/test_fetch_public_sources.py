from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fetch_public_sources import estimate_fit, is_relevant


class PublicSourceFilteringTests(unittest.TestCase):
    def test_oracle_data_integration_role_is_retained(self) -> None:
        vacancy = {
            "title": "Senior Oracle Data Integration Engineer",
            "location": "Remote, Germany",
            "work_mode": "Remote",
            "core_tech_match": "Oracle EBS, OCI, Oracle ODI, GoldenGate, PL/SQL, ETL",
            "_source_text": "English-speaking Oracle ERP integration and data migration role using OCI, Oracle EBS, Oracle ODI, GoldenGate, PL/SQL, and ETL.",
        }

        self.assertTrue(is_relevant(vacancy))
        self.assertGreater(
            estimate_fit(vacancy["title"], vacancy["location"], vacancy["work_mode"], vacancy["_source_text"]),
            8.0,
        )

    def test_ai_engineer_role_is_retained(self) -> None:
        vacancy = {
            "title": "Senior AI Engineer",
            "location": "Remote, Germany",
            "work_mode": "Remote",
            "core_tech_match": "Azure OpenAI, LLM, RAG, MLOps, Python",
            "_source_text": "English-speaking production AI platform role using Azure AI, Azure OpenAI, LLM applications, RAG, LangChain, vector databases, and model serving.",
        }

        self.assertTrue(is_relevant(vacancy))
        self.assertGreater(
            estimate_fit(vacancy["title"], vacancy["location"], vacancy["work_mode"], vacancy["_source_text"]),
            8.0,
        )


if __name__ == "__main__":
    unittest.main()
