# weekly-feeds

A small automation that runs every Friday on GitHub Actions and collects one week of links into [`latest.md`](latest.md):

- **AI news and papers:** Hacker News stories with 150+ points, a Google News search, and the week's top Hugging Face papers.
- **India finance:** the Nifty 50 and Sensex weekly change, filtered RBI and SEBI press releases, and Google News searches.
- **Jobs:** new operations, CRM, automation and analyst roles in India or open to remote work, from a company watchlist on Greenhouse, Lever, Ashby and Workable, plus Remotive.

A copy of each week is kept in [`archive/`](archive/). It uses only public feeds and public job-board APIs, needs no API keys, and calls no AI. A personal Claude task reads `latest.md` on Saturdays and writes a plain-language briefing from it.

## How it works

1. `.github/workflows/collect.yml` starts the job on Fridays at 14:23 UTC, or by hand from the Actions tab ("Run workflow").
2. `collect.py` downloads each feed, keeps the last 7 days (14 for jobs), filters job titles and locations, and writes `latest.md`.
3. The workflow commits the new file. Sources that failed are listed under "Problems this run" instead of stopping the job.

## Changing it

Edit the SETTINGS section at the top of `collect.py`: the job title words, the places, the company watchlist and the feeds.
