# JobBot Eng Ind Search Prompt

You are running inside Codex CLI for `JobBot Eng Ind`. Produce current, English-friendly Senior Data Engineer job matches as valid JSON only.

Return only a JSON object with one top-level field: `vacancies`. Do not include Markdown, prose, comments, or code fences.

## Candidate

- Based near Dortmund, NRW, Germany.
- Target salary: at least EUR 85,000 gross/year where realistic.
- German: B1 now, improving toward B2.
- English-friendly roles are preferred.
- Strong background: Oracle PL/SQL, ERP/WMS, logistics/retail, SQL, senior software/data engineering.
- Desired direction: Azure Databricks, Data Platform, Lakehouse, Spark/PySpark, Python, SQL, Azure Data Factory, Microsoft Fabric.

## Include

- Senior Data Engineer.
- Azure Data Engineer.
- Databricks Data Engineer.
- Cloud Data Engineer.
- Data Platform Engineer.
- Data Lakehouse Engineer.
- Analytics Engineer only when it is engineering-heavy.
- Germany remote roles.
- Europe remote roles only if legally open to Germany-based candidates.
- NRW hybrid roles around Dortmund, Essen, Duesseldorf, Bochum, Duisburg, Cologne, Wuppertal, Ratingen.
- Freelance/project roles only when technically strong and remote-friendly.

## Exclude

- Full onsite roles.
- Junior roles.
- Data Analyst / BI-only roles unless engineering-heavy.
- Roles requiring German C1/C2 unless the technical fit is exceptional.
- Remote roles that are clearly US-only, UK-only, Spain-only, Poland-only, or outside Germany/EU unless they explicitly accept Germany-based candidates.
- Jobs without a credible source URL.

## Source Coverage

Search broadly. Use indexed snippets if direct pages are blocked.

Check and represent these groups where possible:

- LinkedIn-indexed.
- StepStone-indexed.
- Indeed-indexed.
- Glassdoor-indexed.
- GermanTechJobs/get-in-IT/WeAreDevelopers.
- Arbeitnow/EnglishJobs.
- Remote boards: RemoteOK, Remotive, Remote Rocketship, DailyRemote, EU Remote Jobs.
- Freelance/project boards: Freelancermap, freelance.de.
- Aggregators: Adzuna, Joblift, Kimeta.
- Company career pages.
- Recruiters.

## Ranking

Rank by overall fit, not by remote status alone.

Prioritize:

- English working language.
- Azure Databricks / Databricks / Spark / PySpark / SQL / Python / Azure Data Factory / Microsoft Fabric.
- Salary likelihood near or above EUR 85,000.
- Remote or practical NRW hybrid setup.
- Germany-based legal employability.
- Seniority.
- Logistics, retail, ERP, WMS, Oracle or enterprise data background relevance.

## Output Schema

Every item must contain exactly these fields:

- `priority`: one of `A`, `A-`, `B+`, `B`, `B-`, `C`.
- `company`: string.
- `title`: string.
- `location`: string.
- `work_mode`: string.
- `salary`: string.
- `source`: string.
- `url`: string.
- `language`: string.
- `contract_type`: string.
- `core_tech_match`: string.
- `fit_score`: number from 1 to 10.
- `status`: use `NEW`.
- `analysis`: short practical analysis for Volodymyr.
- `recommendation`: whether to apply and how to adapt the CV.
- `found_at`: ISO timestamp.
- `source_group`: one source group name.
- `language_risk`: one of `low`, `medium`, `high`.
- `salary_likelihood`: one of `low`, `medium`, `high`, `unknown`.

Return as many high-quality matches as you find in the `vacancies` array. Do not stop at an arbitrary small number, but do not include low-quality filler.
