from __future__ import annotations

import argparse
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SEARCH_TERMS = [
    "data engineer",
    "senior data engineer",
    "databricks",
    "azure data",
    "data platform",
    "lakehouse",
    "spark",
    "python sql",
]

TARGET_TERMS = [
    "data engineer",
    "databricks",
    "data platform",
    "lakehouse",
    "spark",
    "pyspark",
    "azure",
    "fabric",
    "etl",
    "elt",
    "python",
    "sql",
]

TARGET_TITLE_TERMS = [
    "data engineer",
    "data platform",
    "data architect",
    "analytics engineer",
    "cloud data",
    "azure data",
    "databricks",
    "lakehouse",
]

STRONG_BODY_TERMS = [
    "databricks",
    "azure data factory",
    "microsoft fabric",
    "lakehouse",
    "pyspark",
    "spark",
    "delta lake",
    "data platform",
    "data warehouse",
    "etl",
    "elt",
]

WEAK_TERMS = [
    "intern",
    "internship",
    "praktikum",
    "working student",
    "werkstudent",
    "junior",
    "student",
    "sales manager",
    "maintenance contract",
    "billing specialist",
    "full-stack",
    "full stack",
    "react developer",
    "data scientist",
    "product analyst",
    "network planning",
    "plm",
    "cad systems",
    "administrator",
    "systemadministrator",
    "software developer",
    "java entwickler",
    "devops engineer",
    "crm",
    "microsoft dynamics",
    "it-architekt",
    "it applications analyst",
    "business consultant",
    "professur",
    "professor",
]

REMOTE_COMPATIBLE_TERMS = ["germany", "deutschland", "europe", "emea", "eu", "worldwide", "remote"]
REMOTE_RESTRICTED_TERMS = ["united states", "usa only", "us only", "uk only", "spain only", "poland only"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch public JobBot source APIs")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--coverage-output", type=Path)
    args = parser.parse_args()

    vacancies: list[dict[str, Any]] = []
    coverage: list[dict[str, str]] = []
    for name, fetcher in [
        ("Arbeitnow API", fetch_arbeitnow),
        ("GermanTechJobs RSS", fetch_germantechjobs_rss),
        ("RemoteOK public JSON", fetch_remoteok),
        ("Remotive API", fetch_remotive),
    ]:
        try:
            items = fetcher()
        except Exception as exc:
            coverage.append({"source": name, "outcome": f"failed: {type(exc).__name__}: {exc}"})
            continue
        filtered = [item for item in items if is_relevant(item)]
        vacancies.extend(filtered)
        coverage.append({"source": name, "outcome": f"accepted raw candidates: {len(filtered)} / fetched: {len(items)}"})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"vacancies": vacancies}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.coverage_output:
        args.coverage_output.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Fetched {len(vacancies)} public-source candidates")


def fetch_arbeitnow() -> list[dict[str, Any]]:
    seen: set[str] = set()
    vacancies: list[dict[str, Any]] = []
    for term in SEARCH_TERMS:
        try:
            payload = get_json(f"https://www.arbeitnow.com/api/job-board-api?search={quote_query(term)}")
        except RuntimeError:
            continue
        time.sleep(0.5)
        for item in payload.get("data", []):
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            vacancies.append(to_vacancy(
                company=item.get("company_name") or "Unknown",
                title=item.get("title") or "Untitled",
                location=item.get("location") or "Germany",
                work_mode="Remote possible" if item.get("remote") else "Work mode not specified",
                salary="Not disclosed",
                source="Arbeitnow API",
                url=url,
                description=item.get("description") or "",
                source_group="Arbeitnow/EnglishJobs",
                contract_type=", ".join(item.get("job_types") or []) or "Not specified",
                found_at=item.get("created_at") or now_iso(),
            ))
    return vacancies


def fetch_remoteok() -> list[dict[str, Any]]:
    payload = get_json("https://remoteok.com/api")
    vacancies: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or "legal" in item:
            continue
        url = item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id', '')}"
        vacancies.append(to_vacancy(
            company=item.get("company") or "Unknown",
            title=item.get("position") or "Untitled",
            location=item.get("location") or "Remote",
            work_mode="Remote",
            salary=format_salary(item),
            source="RemoteOK public JSON",
            url=url,
            description=" ".join(str(item.get(key) or "") for key in ["description", "tags", "location"]),
            source_group="Remote boards",
            contract_type="Remote job",
            found_at=item.get("date") or now_iso(),
        ))
    return vacancies


def fetch_germantechjobs_rss() -> list[dict[str, Any]]:
    data = get_bytes("https://germantechjobs.de/rss")
    root = ET.fromstring(data)
    vacancies: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title_text = get_xml_text(item, "title")
        description = get_xml_text(item, "description")
        url = strip_tracking(get_xml_text(item, "link"))
        if not title_text or not url:
            continue
        title, company, salary = parse_germantechjobs_title(title_text)
        vacancies.append(to_vacancy(
            company=company,
            title=title,
            location=infer_germantechjobs_location(description),
            work_mode=infer_work_mode(description),
            salary=salary,
            source="GermanTechJobs RSS",
            url=url,
            description=description,
            source_group="GermanTechJobs/get-in-IT/WeAreDevelopers",
            contract_type="Full-time permanent likely",
            found_at=get_xml_text(item, "pubDate") or now_iso(),
        ))
    return vacancies


def fetch_remotive() -> list[dict[str, Any]]:
    seen: set[str] = set()
    vacancies: list[dict[str, Any]] = []
    for term in SEARCH_TERMS:
        try:
            payload = get_json(f"https://remotive.com/api/remote-jobs?search={quote_query(term)}")
        except RuntimeError:
            continue
        time.sleep(0.5)
        for item in payload.get("jobs", []):
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            vacancies.append(to_vacancy(
                company=item.get("company_name") or "Unknown",
                title=item.get("title") or "Untitled",
                location=item.get("candidate_required_location") or "Remote",
                work_mode="Remote",
                salary=item.get("salary") or "Not disclosed",
                source="Remotive API",
                url=url,
                description=item.get("description") or "",
                source_group="Remote boards",
                contract_type=item.get("job_type") or "Remote job",
                found_at=item.get("publication_date") or now_iso(),
            ))
    return vacancies


def to_vacancy(
    *,
    company: str,
    title: str,
    location: str,
    work_mode: str,
    salary: str,
    source: str,
    url: str,
    description: str,
    source_group: str,
    contract_type: str,
    found_at: str,
) -> dict[str, Any]:
    text = clean_text(description)
    score = estimate_fit(title, location, work_mode, text)
    language_risk = "low" if "english" in text.lower() else "medium"
    salary_likelihood = estimate_salary_likelihood(salary, title, text)
    return {
        "priority": priority_for_score(score),
        "company": clean_text(company),
        "title": clean_text(title),
        "location": clean_text(location),
        "work_mode": clean_text(work_mode),
        "salary": clean_text(salary),
        "source": source,
        "url": url,
        "language": "English-friendly likely" if language_risk == "low" else "Language not explicit",
        "contract_type": clean_text(contract_type),
        "core_tech_match": summarize_tech_match(title, text),
        "fit_score": score,
        "status": "NEW",
        "analysis": "Public API candidate. Fit is estimated from title, location, remote compatibility, and visible stack terms.",
        "recommendation": "Review the original posting before applying; tailor the CV only if the original role confirms senior data-engineering scope.",
        "found_at": normalize_date(found_at),
        "source_group": source_group,
        "language_risk": language_risk,
        "salary_likelihood": salary_likelihood,
        "_source_text": text,
    }


def is_relevant(vacancy: dict[str, Any]) -> bool:
    title = str(vacancy.get("title", "")).lower()
    text = " ".join(str(vacancy.get(field, "")) for field in ["title", "location", "work_mode", "core_tech_match", "_source_text"]).lower()
    if any(term in text for term in WEAK_TERMS):
        return False
    title_match = contains_any_term(title, TARGET_TITLE_TERMS)
    strong_body_match = any(role in title for role in ["engineer", "architect", "consultant"]) and contains_any_term(text, STRONG_BODY_TERMS)
    if not title_match and not strong_body_match:
        return False
    if not contains_any_term(text, TARGET_TERMS):
        return False
    if any(term in text for term in REMOTE_RESTRICTED_TERMS) and not any(term in text for term in ["germany", "deutschland"]):
        return False
    if "remote" in text and not any(term in text for term in REMOTE_COMPATIBLE_TERMS):
        return False
    return True


def estimate_fit(title: str, location: str, work_mode: str, text: str) -> float:
    haystack = f"{title} {location} {work_mode} {text}".lower()
    score = 5.0
    for term in ["senior", "lead", "principal"]:
        if term in haystack:
            score += 0.5
            break
    for term in ["databricks", "azure", "fabric", "spark", "pyspark", "lakehouse"]:
        if term in haystack:
            score += 0.45
    for term in ["sql", "python", "etl", "elt", "data platform", "data engineer"]:
        if term in haystack:
            score += 0.25
    if any(term in haystack for term in ["remote", "germany", "deutschland", "nrw", "dusseldorf", "duesseldorf", "essen"]):
        score += 0.4
    return round(min(score, 9.2), 1)


def priority_for_score(score: float) -> str:
    if score >= 8.8:
        return "A"
    if score >= 8.3:
        return "A-"
    if score >= 7.8:
        return "B+"
    if score >= 7.0:
        return "B"
    if score >= 6.2:
        return "B-"
    return "C"


def summarize_tech_match(title: str, text: str) -> str:
    haystack = f"{title} {text}".lower()
    terms = [
        term
        for term in ["Azure", "Databricks", "Microsoft Fabric", "Spark", "PySpark", "Python", "SQL", "ETL", "ELT", "Lakehouse", "Kafka", "Airflow", "dbt"]
        if contains_term(haystack, term.lower())
    ]
    return ", ".join(terms) if terms else clean_text(title)


def estimate_salary_likelihood(salary: str, title: str, text: str) -> str:
    haystack = f"{salary} {title} {text}".lower()
    numbers = [int(match.replace(",", "").replace(".", "")) for match in re.findall(r"\b\d[\d,.]{3,}\b", haystack)]
    if any(number >= 85000 for number in numbers):
        return "high"
    if any(term in haystack for term in ["senior", "lead", "principal", "architect", "contract"]):
        return "medium"
    if numbers:
        return "low"
    return "unknown"


def format_salary(item: dict[str, Any]) -> str:
    minimum = item.get("salary_min")
    maximum = item.get("salary_max")
    if minimum and maximum:
        return f"{minimum}-{maximum} {item.get('salary_currency') or ''}".strip()
    if minimum:
        return f"From {minimum} {item.get('salary_currency') or ''}".strip()
    if maximum:
        return f"Up to {maximum} {item.get('salary_currency') or ''}".strip()
    return "Not disclosed"


def get_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "JobBotEngInd/0.1 (+https://remoteok.com)"})
    try:
        with urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"URL error for {url}: {exc.reason}") from exc


def get_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "JobBotEngInd/0.1"})
    try:
        with urlopen(request, timeout=25) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"URL error for {url}: {exc.reason}") from exc


def get_xml_text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    return clean_text(value)


def parse_germantechjobs_title(title_text: str) -> tuple[str, str, str]:
    salary = "Not disclosed"
    salary_match = re.search(r"\[([^\]]+)\]\s*$", title_text)
    if salary_match:
        salary = salary_match.group(1).strip()
        title_text = title_text[: salary_match.start()].strip()
    if " @ " in title_text:
        title, company = title_text.rsplit(" @ ", 1)
    else:
        title, company = title_text, "Unknown"
    return clean_text(title), clean_text(company), clean_text(salary)


def infer_germantechjobs_location(description: str) -> str:
    text = clean_text(description)
    for city in ["Düsseldorf", "Duesseldorf", "Essen", "Dortmund", "Bochum", "Duisburg", "Köln", "Koeln", "Cologne", "Wuppertal", "Ratingen", "Berlin", "Munich", "München", "Hamburg", "Germany"]:
        if city.lower() in text.lower():
            return city
    return "Germany"


def infer_work_mode(description: str) -> str:
    text = clean_text(description).lower()
    if any(term in text for term in ["remote", "homeoffice", "home office", "mobile work", "mobiles arbeiten"]):
        if any(term in text for term in ["hybrid", "office", "büro", "buro"]):
            return "Hybrid / remote-friendly"
        return "Remote-friendly"
    return "Work mode not specified"


def strip_tracking(url: str) -> str:
    return url.split("?", 1)[0]


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "Not specified"


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10:
        return text
    return now_iso()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quote_query(value: str) -> str:
    return re.sub(r"\s+", "%20", value.strip())


def contains_any_term(text: str, terms: list[str]) -> bool:
    return any(contains_term(text, term) for term in terms)


def contains_term(text: str, term: str) -> bool:
    term = term.lower()
    if len(term) <= 3 or term in {"dbt", "etl", "elt", "sql"}:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


if __name__ == "__main__":
    main()
