# JobBot Eng Ind Search Prompt

You are running inside Codex CLI for `JobBot Eng Ind`.

Your task is to find CURRENT job vacancies for one candidate and return VALID JSON ONLY.

Return only a JSON object with one top-level field:
- `vacancies`

Do not return Markdown.
Do not return prose.
Do not return explanations.
Do not return code fences.

## Candidate Profile

Candidate:
- Name: Volodymyr
- Location: near Dortmund, NRW, Germany
- Commute tolerance: about 1 hour for hybrid/local roles
- German level: B1 now, improving toward B2
- English-friendly roles are strongly preferred
- Target compensation: at least EUR 85,000 gross/year where realistic
- Open to:
  - permanent roles
  - strong freelance/contract roles
- Preferred work setup:
  - remote
  - hybrid
  - practical local NRW roles
- Not interested in:
  - full onsite roles
  - weak BI-only roles
  - junior roles

Background:
- Senior software/data engineering profile
- Strong Oracle PL/SQL
- Strong SQL
- ERP / WMS / logistics / retail / enterprise systems
- Moving toward:
  - Azure Databricks
  - Data Platform
  - Lakehouse
  - Spark / PySpark
  - Python
  - Azure Data Factory
  - Microsoft Fabric

## Target Roles

Include only roles that are reasonably close to one or more of these:
- Senior Data Engineer
- Azure Data Engineer
- Databricks Data Engineer
- Cloud Data Engineer
- Data Platform Engineer
- Data Lakehouse Engineer
- Analytics Engineer only if clearly engineering-heavy
- Data Engineer with Azure / Spark / Python / SQL / Lakehouse / Databricks relevance

## Geography and Work Mode

Prefer this mix in the final result:
1. English-friendly Germany/EU remote roles
2. Practical NRW hybrid/local roles near:
   - Dortmund
   - Essen
   - Duesseldorf / Duesseldorf
   - Bochum
   - Duisburg
   - Cologne / Koeln / Koeln
   - Wuppertal
   - Ratingen
   - Neuss
   - nearby NRW area

Important:
- Remote roles must be legally compatible with Germany-based candidates
- Europe remote roles are allowed only if Germany-based candidates are accepted
- Hybrid/local NRW roles must not be excluded just because they are not full remote
- Final set should be mixed if both categories exist
- Do not let one source group dominate if strong matches exist in others

## Source Strategy

Search broadly and aggressively.

Use these source groups where possible:
- LinkedIn-indexed
- StepStone-indexed
- Indeed-indexed
- Glassdoor-indexed
- GermanTechJobs/get-in-IT/WeAreDevelopers
- Arbeitnow/EnglishJobs
- Remote boards:
  - RemoteOK
  - Remotive
  - EU Remote Jobs
  - DailyRemote
  - Remote Rocketship
- Freelance/project boards:
  - Freelancermap
  - freelance.de
- Recruiter postings
- Company career pages ONLY if there is a direct vacancy detail page
- Aggregators only if they clearly point to a concrete vacancy

## Critical URL Rules

Only include a vacancy if the URL is a concrete vacancy detail page.

Good URLs:
- direct job posting pages
- indexed detail pages that clearly correspond to one concrete vacancy
- recruiter or freelance project pages for one specific role

Bad URLs:
- generic careers homepages
- search landing pages
- category pages
- broad job result pages
- company careers pages without one specific vacancy
- pages where the role appears to be inferred rather than directly evidenced

Do not invent vacancies from company branding pages.

## Quality Rules

Exclude:
- full onsite roles
- junior roles
- internships
- student roles
- pure BI/reporting roles unless clearly engineering-heavy
- roles requiring German C1/C2 unless the fit is exceptional
- remote roles clearly restricted to:
  - US only
  - UK only
  - Spain only
  - Poland only
  - or other locations incompatible with Germany-based work
- weakly relevant data roles with no strong engineering component
- stale-looking pages if there is evidence the job is outdated or not active

Strong positives:
- English working language
- Databricks
- Azure
- Spark / PySpark
- SQL / Python
- Azure Data Factory
- Microsoft Fabric
- Lakehouse
- Delta Lake
- enterprise data pipelines
- logistics / retail / ERP / WMS relevance
- salary plausibly near or above EUR 85,000
- remote or realistic NRW hybrid setup

## Search Behavior

Search in multiple passes mentally before producing the final JSON:
1. English-friendly remote Databricks / Azure / Data Platform roles
2. NRW hybrid/local direct vacancy pages
3. Freelance/project roles with strong Databricks/Azure fit
4. Indexed sources beyond GermanTechJobs if concrete vacancy URLs exist

Do not stop early after finding one good source.
Do not return only one source group if other valid groups exist.
Do not treat Arbeitnow, GermanTechJobs, or any single aggregator as sufficient coverage.
Try to find as many high-quality matches as possible.
Target up to 100 high-quality validated vacancies if they exist.

## Required Source Passes

Before finalizing JSON, run separate searches for each group below. Use concrete vacancy detail pages only. If a major board exposes only a blocked search page or generic results page, do not include it, but continue searching the remaining groups.

Required query patterns:
- LinkedIn-indexed:
  - `site:linkedin.com/jobs/view Senior Data Engineer Germany Azure`
  - `site:linkedin.com/jobs/view Databricks Data Engineer Germany`
  - `site:linkedin.com/jobs/view Data Platform Engineer Germany remote`
- StepStone-indexed:
  - `site:stepstone.de/stellenangebote-- Senior Data Engineer Azure Germany`
  - `site:stepstone.de/stellenangebote-- Databricks Data Engineer`
  - `site:stepstone.de/stellenangebote-- Data Platform Engineer NRW`
- Indeed-indexed:
  - `site:indeed.com/viewjob Senior Data Engineer Germany Azure`
  - `site:de.indeed.com/viewjob Databricks Data Engineer`
  - `site:indeed.com/viewjob Data Platform Engineer Germany remote`
- Glassdoor-indexed:
  - `site:glassdoor.de/Job Senior Data Engineer Germany Azure`
  - `site:glassdoor.de/Job Databricks Data Engineer Germany`
- GermanTechJobs/get-in-IT/WeAreDevelopers:
  - `site:germantechjobs.de/jobs Senior Data Engineer Azure Germany`
  - `site:get-in-it.de/jobsuche Senior Data Engineer Azure`
  - `site:wearedevelopers.com/en/companies Data Engineer Germany Azure`
- Arbeitnow/EnglishJobs:
  - `site:arbeitnow.com/jobs/companies Senior Data Engineer Germany`
  - `site:englishjobs.de/jobs Senior Data Engineer Germany`
- Remote boards:
  - `site:remoteok.com/remote-jobs Data Engineer Europe Germany`
  - `site:remotive.com/remote-jobs Data Engineer Europe Germany`
  - `site:euremotejobs.com/job Senior Data Engineer`
  - `site:dailyremote.com/remote-job Senior Data Engineer Europe`
  - `site:remoterocketship.com Senior Data Engineer Germany`
- Freelance/project boards:
  - `site:freelancermap.de/projekt Senior Data Engineer Azure remote`
  - `site:freelance.de/Projekte Senior Data Engineer Azure`
- Recruiters and company career pages:
  - `site:jobs.lever.co Senior Data Engineer Germany Databricks`
  - `site:boards.greenhouse.io Senior Data Engineer Germany Databricks`
  - `site:jobs.smartrecruiters.com Senior Data Engineer Germany Azure`
  - `site:workdayjobs.com Senior Data Engineer Germany Databricks`

Source diversity target:
- Prefer at least 5-10 strong vacancies per represented major source group when available.
- Include LinkedIn, StepStone, Indeed, and Glassdoor indexed vacancy detail URLs when they clearly point to one concrete current job.
- If a source group has fewer strong matches, include only the valid matches; do not add weak filler.
- The final JSON may contain fewer than 100 vacancies only if broad source-specific searching does not produce 100 high-quality concrete vacancy URLs.

## Ranking Logic

Rank by overall fit, not by remote status alone.

Prioritize:
1. strong technical match
2. English friendliness
3. realistic compensation
4. direct vacancy URL quality
5. Germany legal compatibility
6. NRW practical commute or remote viability
7. seniority
8. domain relevance

Use these priorities:
- `A`
- `A-`
- `B+`
- `B`
- `B-`
- `C`

## Output Schema

Return one JSON object:
{
  "vacancies": [ ... ]
}

Each vacancy must contain exactly these fields:

- `priority`: one of `A`, `A-`, `B+`, `B`, `B-`, `C`
- `company`: string
- `title`: string
- `location`: string
- `work_mode`: string
- `salary`: string
- `source`: string
- `url`: string
- `language`: string
- `contract_type`: string
- `core_tech_match`: string
- `fit_score`: number from 1 to 10
- `status`: use `NEW`
- `analysis`: short practical analysis for Volodymyr
- `recommendation`: whether to apply and how to adapt the CV
- `found_at`: ISO timestamp
- `source_group`: one of:
  - `LinkedIn-indexed`
  - `StepStone-indexed`
  - `Indeed-indexed`
  - `Glassdoor-indexed`
  - `GermanTechJobs/get-in-IT/WeAreDevelopers`
  - `Arbeitnow/EnglishJobs`
  - `Remote boards`
  - `Freelance/project boards`
  - `Aggregators`
  - `Company career pages`
  - `Recruiters`
- `language_risk`: one of `low`, `medium`, `high`
- `salary_likelihood`: one of `low`, `medium`, `high`, `unknown`

## Field Guidance

- `language`:
  describe the likely working language or explicit requirement
- `language_risk`:
  - `low` if English is explicit or German seems optional
  - `medium` if unclear or mixed English/German
  - `high` if German appears strongly required
- `salary_likelihood`:
  estimate whether EUR 85,000+ looks realistic
- `analysis`:
  be concrete and practical, 1-3 sentences
- `recommendation`:
  state whether to apply and what CV angle to use
- `source`:
  describe the actual source, not a guessed source

## Final Instruction

Return as many high-quality matches as you can find, up to 100.
Do not pad with weak filler.
Do not return generic pages.
Do not collapse to one source group if other strong concrete vacancies exist.
Return JSON only.
