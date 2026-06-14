from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any


REQUIRED_FIELDS = [
    "priority",
    "company",
    "title",
    "location",
    "work_mode",
    "salary",
    "source",
    "url",
    "language",
    "contract_type",
    "core_tech_match",
    "fit_score",
    "status",
    "analysis",
    "recommendation",
    "found_at",
    "source_group",
    "language_risk",
    "salary_likelihood",
]

SOURCE_GROUPS = [
    "LinkedIn-indexed",
    "StepStone-indexed",
    "Indeed-indexed",
    "Glassdoor-indexed",
    "GermanTechJobs/get-in-IT/WeAreDevelopers",
    "Arbeitnow/EnglishJobs",
    "Remote boards",
    "Freelance/project boards",
    "Aggregators",
    "Company career pages",
    "Recruiters",
]

PRIORITY_ORDER = {"A": 0, "A-": 1, "B+": 2, "B": 3, "B-": 4, "C": 5}
LANGUAGE_RISK_PENALTY = {"low": 0.0, "medium": 0.25, "high": 0.75}
MAX_OUTPUT_ITEMS = 50


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    text = path.read_text(encoding="utf-8-sig").strip()
    return json.loads(text) if text else fallback


def normalize_vacancy(raw: dict[str, Any]) -> dict[str, Any]:
    vacancy = {field: raw.get(field, "not specified") for field in REQUIRED_FIELDS}
    vacancy["fit_score"] = _number(raw.get("fit_score"), default=5.0)
    vacancy["priority"] = _enum(str(raw.get("priority", "C")), PRIORITY_ORDER, "C")
    vacancy["status"] = _enum(str(raw.get("status", "NEW")), {"NEW": 1, "previously seen": 1}, "NEW")
    vacancy["language_risk"] = _enum(str(raw.get("language_risk", "medium")), LANGUAGE_RISK_PENALTY, "medium")
    vacancy["salary_likelihood"] = _enum(
        str(raw.get("salary_likelihood", "unknown")),
        {"low": 1, "medium": 1, "high": 1, "unknown": 1},
        "unknown",
    )
    vacancy["found_at"] = raw.get("found_at") or datetime.now(timezone.utc).isoformat()
    return vacancy


def score_vacancy(vacancy: dict[str, Any]) -> float:
    text = " ".join(
        str(vacancy.get(field, ""))
        for field in ["title", "work_mode", "language", "core_tech_match", "analysis", "recommendation"]
    ).lower()
    score = float(vacancy["fit_score"])
    if "english" in text:
        score += 0.5
    if any(term in text for term in ["databricks", "azure", "fabric", "spark", "pyspark", "lakehouse"]):
        score += 0.6
    if any(term in text for term in ["remote", "homeoffice", "home office", "100%"]):
        score += 0.25
    if is_nrw_local_hybrid(vacancy):
        score += 0.35
    if is_english_remote(vacancy):
        score += 0.35
    if has_direct_job_url(vacancy):
        score += 0.3
    if "german c1" in text or "c1" in str(vacancy.get("language", "")).lower():
        score -= 0.7
    score -= LANGUAGE_RISK_PENALTY.get(vacancy["language_risk"], 0.25)
    return max(1.0, min(10.0, score))


def should_reject(vacancy: dict[str, Any]) -> str | None:
    text = " ".join(str(value) for value in vacancy.values()).lower()
    if not vacancy.get("url") or vacancy["url"] == "not specified":
        return "missing url"
    if looks_like_generic_careers_page(vacancy["url"]):
        return "generic careers page without direct vacancy detail"
    if not any(term in text for term in ["data engineer", "databricks", "data platform", "analytics engineer", "etl"]):
        return "weak role match"
    if "onsite" in text and not any(term in text for term in ["remote", "hybrid", "homeoffice", "home office"]):
        return "onsite without remote/hybrid signal"
    if "german c1" in text and score_vacancy(vacancy) < 8.0:
        return "German C1 role without exceptional fit"
    if "company careers search" in str(vacancy.get("source", "")).lower() and not has_direct_job_url(vacancy):
        return "company careers search without direct vacancy url"
    return None


def looks_like_generic_careers_page(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    path = parsed.path.lower().rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return True
    generic_paths = {
        "",
        "/",
        "/careers",
        "/career",
        "/jobs",
        "/job-search",
        "/karriere",
        "/stellenangebote",
        "/stellen",
        "/en/careers",
        "/de/careers",
        "/de-de/careers",
        "/global/en/search-results",
        "/en/about-us/careers",
    }
    if path in generic_paths:
        return True
    generic_tokens = ["careers", "career", "jobs", "job-search", "stellenangebote", "karriere", "search-results"]
    detail_tokens = [
        "view",
        "jobdetails",
        "jobdetail",
        "position",
        "posting",
        "vacancy",
        "role",
        "apply",
        "stellenanzeige",
        "stellenangebote--",
        "jobs/view",
    ]
    path_parts = [part for part in path.split("/") if part]
    if any(token in path for token in detail_tokens):
        return False
    if len(path_parts) >= 2 and any(char.isdigit() for char in path) and len(path) >= 30:
        return False
    if len(path_parts) <= 2 and any(token in path for token in generic_tokens):
        return True
    if any(token in path for token in generic_tokens) and not any(token in path for token in detail_tokens):
        if len(path_parts) <= 3 and not any(char.isdigit() for char in path):
            return True
    return False


def has_direct_job_url(vacancy: dict[str, Any]) -> bool:
    url = str(vacancy.get("url", ""))
    if not url or url == "not specified":
        return False
    if looks_like_generic_careers_page(url):
        return False
    path = urlparse(url).path.lower()
    source = str(vacancy.get("source", "")).lower()
    direct_tokens = ["jobdetails", "jobdetail", "/view/", "/jobs/view", "/positions/", "/position/", "/vacancy/", "/role/"]
    if any(token in path for token in direct_tokens):
        return True
    if any(domain in url.lower() for domain in ["linkedin.com/jobs/view", "greenhouse.io", "lever.co", "remotive.com", "freelancermap.com"]):
        return True
    if any(token in source for token in ["indexed", "recruiter", "freelance", "remotive", "greenhouse", "lever"]):
        return True
    return len([part for part in path.split("/") if part]) >= 3


def is_nrw_local_hybrid(vacancy: dict[str, Any]) -> bool:
    text = " ".join(str(vacancy.get(field, "")) for field in ["location", "work_mode", "analysis"]).lower()
    nrw_terms = ["dortmund", "essen", "dusseldorf", "duesseldorf", "bochum", "duisburg", "cologne", "koln", "koeln", "wuppertal", "ratingen", "nrw"]
    return any(term in text for term in nrw_terms) and any(term in text for term in ["hybrid", "remote", "flexible", "home office", "homeoffice"])


def is_english_remote(vacancy: dict[str, Any]) -> bool:
    text = " ".join(str(vacancy.get(field, "")) for field in ["location", "work_mode", "language", "analysis"]).lower()
    remote_signal = any(term in text for term in ["remote", "home office", "homeoffice", "fully remote", "full remote", "100% remote"])
    english_signal = "english" in text
    germany_compatible = not any(term in text for term in ["us-only", "uk-only", "spain-only", "poland-only"])
    return remote_signal and english_signal and germany_compatible


def mix_bucket(vacancy: dict[str, Any]) -> str:
    if is_nrw_local_hybrid(vacancy):
        return "nrw"
    if is_english_remote(vacancy):
        return "remote"
    return "other"


def mix_and_limit(vacancies: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    buckets = {"nrw": [], "remote": [], "other": []}
    for vacancy in vacancies:
        buckets[mix_bucket(vacancy)].append(vacancy)

    result: list[dict[str, Any]] = []
    primary_order = ["nrw", "remote", "other"]
    if buckets["remote"] and (not buckets["nrw"] or float(buckets["remote"][0]["fit_score"]) > float(buckets["nrw"][0]["fit_score"])):
        primary_order = ["remote", "nrw", "other"]

    while len(result) < limit and any(buckets.values()):
        for bucket_name in primary_order:
            if buckets[bucket_name] and len(result) < limit:
                result.append(buckets[bucket_name].pop(0))
    return result


def source_coverage(vacancies: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen_text = " ".join(str(vacancy.get("source_group", "")) for vacancy in vacancies).lower()
    coverage = []
    for group in SOURCE_GROUPS:
        outcome = "strong matches found" if group.lower().split("/")[0] in seen_text else "not represented in validated output"
        coverage.append({"source_group": group, "outcome": outcome})
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate, filter, and rank JobBot Eng Ind vacancies")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--coverage-output", type=Path)
    parser.add_argument("--rejects-output", type=Path)
    args = parser.parse_args()

    raw_items = load_json(args.input, [])
    if isinstance(raw_items, dict) and "vacancies" in raw_items:
        raw_items = raw_items["vacancies"]
    if not isinstance(raw_items, list):
        raise ValueError("Input must be a JSON array or an object with a vacancies array")

    accepted: list[dict[str, Any]] = []
    rejects: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            rejects.append({"reason": "item is not an object", "item": str(raw)})
            continue
        vacancy = normalize_vacancy(raw)
        key = "|".join(str(vacancy.get(part, "")).strip().lower() for part in ["company", "title", "location", "url"])
        if key in seen_keys:
            rejects.append({"reason": "duplicate", "item": vacancy.get("url", "")})
            continue
        seen_keys.add(key)
        reason = should_reject(vacancy)
        if reason:
            rejects.append({"reason": reason, "item": vacancy.get("url", "")})
            continue
        vacancy["_rank_score"] = round(score_vacancy(vacancy), 3)
        accepted.append(vacancy)

    accepted.sort(key=lambda item: (PRIORITY_ORDER.get(item["priority"], 99), -item["_rank_score"], -float(item["fit_score"])))
    accepted = mix_and_limit(accepted, MAX_OUTPUT_ITEMS)
    for item in accepted:
        item.pop("_rank_score", None)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(accepted, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.coverage_output:
        args.coverage_output.write_text(json.dumps(source_coverage(accepted), ensure_ascii=False, indent=2), encoding="utf-8")
    if args.rejects_output:
        args.rejects_output.write_text(json.dumps(rejects, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Accepted {len(accepted)} vacancies, rejected {len(rejects)}")


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _enum(value: str, allowed: dict[str, Any], default: str) -> str:
    return value if value in allowed else default


if __name__ == "__main__":
    main()
