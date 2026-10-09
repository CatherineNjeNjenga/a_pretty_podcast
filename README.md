# Pretty Tough: Maria vs guest, weekly pipeline (GitHub Actions + Turso)

Tests whether YouTube viewers talk about Maria more than the guest, and updates the answer
as each new episode passes its 7-day comment snapshot.

## How it works
- `episodes.csv` is the only file you edit each week: add one row per new episode
  (`video_url, published, guest, guest_aliases, guest_tier`). Aliases are lowercase, pipe-separated
  (include nicknames). Tier is optional: 1 mega-famous, 2 well known, 3 niche.
- A daily GitHub Actions run (`.github/workflows/weekly.yml`) looks for episodes that are 7+ days old and
  not yet processed, scrapes their comments once (fixed snapshot, so episodes are comparable), scores
  sentiment with VADER, and saves everything to Turso.
- It then re-tags all stored comments, recomputes the cumulative Maria-minus-guest gap, appends a row to
  `weekly_metrics`, and uploads charts and CSVs as a workflow artifact (kept 90 days).
- Name tagging happens at analysis time, so fixing an alias in `episodes.csv` corrects past episodes too.

## One-time setup
1. **Turso**
       turso db create pretty-tough
       turso db show pretty-tough --url          # -> TURSO_DATABASE_URL (libsql://...)
       turso db tokens create pretty-tough       # -> TURSO_AUTH_TOKEN
   Tables are created automatically on the first run.
2. **YouTube Data API key** (free): Google Cloud console > create a project > enable "YouTube Data API v3"
   > Credentials > Create credentials > API key (restrict it to that API). Reads cost 1 quota unit per call
   against a default 10,000 units/day, so a weekly run uses a tiny fraction.
   (Optional fallback: set the repo variable `COMMENT_SOURCE=apify` and see `scrape_youtube.py`.)
3. **GitHub repo** (private is fine): push these files, then under Settings > Secrets and variables > Actions add
   - secrets: `YOUTUBE_API_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`
   - optional variables: `INCLUDE_REPLIES` (true/false, default false = top-level comments only), `MAX_SCAN`
     (default 30000 comments scanned per episode), `CHANNEL_ID`, `TITLE_FILTER`
   - only if you use the Apify fallback: secret `APIFY_TOKEN`, variables `COMMENT_SOURCE=apify`, `APIFY_ACTOR_ID`
4. Run it once by hand: Actions tab > pretty-tough-weekly > Run workflow (tick "force" to build charts early).

## New-episode alerts (watch_feed.py)
Each daily run first reads the channel's latest uploads (through the YouTube API when `YOUTUBE_API_KEY` is set, otherwise the public RSS feed; default channel: @mariasharapova,
ID `UCX8NFD2nhnXv6gYt0Cj6ogw`). For any video whose title matches `TITLE_FILTER` (default "pretty tough",
case-insensitive) and is not yet in `episodes.csv`, it opens a GitHub issue with a suggested row to paste in.
GitHub emails you about the issue. Optional repo variables: `CHANNEL_ID`, `TITLE_FILTER`.
Before relying on it, run `python watch_feed.py --list` once: it prints every video in the feed and
whether it matches, so you can adjust the filter to the real episode title format. The guest guess
only works for titles shaped like "... with Guest Name"; always check the suggested aliases.

## See what is on the channel, no Python needed (list-episodes workflow)
After the repo and the `YOUTUBE_API_KEY` secret exist: Actions tab > list-episodes > Run workflow.
Open the finished run: the job summary shows every playlist (with its `PL...` ID) and the latest uploads
with a "matches filter" column. `uploads.csv` and `playlists.csv` are attached as an artifact.
Use it to find the Pretty Tough playlist and to check whether `TITLE_FILTER` matches your real titles.

## Keep only podcast episodes (PLAYLIST_ID)
Quickest test: when you click Run workflow on list-episodes, paste the playlist ID (or link) into the
"Podcast playlist ID" box. If that box is missing from the dialog, the new `list-episodes.yml` is not in your repo yet.
Variables must be added as *repository* variables (Settings > Secrets and variables > Actions > Variables >
Repository variables), not environment variables or secrets.

The channel posts other videos too, so by default the list is unfiltered and the title filter is only a guess.
The reliable fix: run the list-episodes workflow, find the Pretty Tough playlist in its Playlists table, and set
the repo variable `PLAYLIST_ID` to its ID (starts with `PL`). With it set, the new-episode alerts, the
back-catalogue helper and list-episodes all read only that playlist and ignore `TITLE_FILTER`. Private and
deleted videos are skipped. If the playlist also holds clips or trailers, remove them from the playlist on YouTube
(if you manage it) or delete those rows from `episodes.csv`.

## Turning the playlist list into episodes.csv (no Python needed)
`uploads.csv` is just a listing (`published, video_id, title, url, matches_filter`); it is not the file the pipeline reads.
Each list-episodes run also attaches `episodes_draft.csv`, already in the `episodes.csv` shape
(`video_url, published, guest, guest_aliases, guest_tier`, oldest episode first). Download it from the run's
artifact, open it in Excel or Google Sheets, and fill in `guest` (name), `guest_aliases` (lowercase, pipe-separated,
e.g. `christina|tosi`, include nicknames) and optionally `guest_tier` (1 mega-famous, 2 well known, 3 niche) for every
row. Use the `title_for_reference` column to see who the guest is. It can stay in the file (the pipeline ignores it).
Save it as `episodes.csv`, replacing the empty one in the repo, and commit. The pipeline stops with a clear message if
any row still has a blank guest or aliases.

## Back catalogue (backfill_episodes.py)
To load past episodes: `export YOUTUBE_API_KEY=...` then `python backfill_episodes.py --from 2026-01-01`.
It writes `episodes_draft.csv` (matching uploads not yet in `episodes.csv`, guests guessed from titles).
Review it, drop the `title_for_reference` column, and append the rows to `episodes.csv`. To stop the watcher
alerting about older uploads, set the repo variable `WATCH_FROM_DATE` (YYYY-MM-DD) to your start date.
Older episodes are fine to include: the pipeline counts only comments posted in each episode's first 7 days
(see "Comment window" below), so they are measured the same way as new ones.

## Weekly routine
1. When the GitHub issue arrives, paste its row into `episodes.csv` (fix guest and aliases) and commit.
2. About a week after upload the daily run processes it. Download the artifact, then use the PNGs
   and `verdict.txt` in your Substack post.

## Comment window (what counts)
For every episode only comments posted in its first 7 days count: the upload day plus the next six, in UTC.
An episode becomes eligible once it is 7 days old, so new episodes have a complete window and old episodes
(including the back catalogue) are cut to the same window. Comments after that are fetched but ignored.
YouTube lists comments newest-first, so the pipeline has to scan back to reach an old episode's first week. It scans up
to `MAX_SCAN` comments per episode (default 30000, about 300 quota units). If an episode has more comments than that,
it is left pending with a message in the log instead of being counted with an incomplete window; raise `MAX_SCAN` to
include it. An unexpected fetch error on one episode does not stop the others; the run is marked failed at the end
and the episode is retried next time. If the daily quota runs out, the rest are retried the next day.
Note: replies are excluded by default (`INCLUDE_REPLIES`), so the window applies to top-level comments.

## Data retention (YouTube's 30-day rule)
YouTube's developer policies allow storing public comment data for at most 30 calendar days. So:
- Each comment is tagged by `classify.py` (first match wins): **Noise** (link only, timestamps, "1st/top", self-promotion),
  **Request** (asks for a guest, English and Russian; wins over any name, so "Мария, пригласи X" is not counted as
  engagement with Maria), **Maria / Guest / Both** (names, including Russian and Chinese spellings, accents and stretched
  letters like "Lindseeeyyy"), **Show** (judges the show or episode), **Pair** (about both hosts, no names: "two queens"),
  **Unnamed** (she/her or a role like presenter, no name), **Reaction** (emoji-only or a short feeling like "Congratulations!"),
  **Topic** (readable comment about the subject, 5+ words) and **Neither** (the rest).
- Only Maria, Guest and Both enter the Maria-vs-guest gap. The tone score is not used for tagging (it scores 0 on emoji,
  Russian and short comments); it is still stored for Show and tone charts.
- On your 187-comment labelled sample the rules agree with your labels on about 83% (lenient mapping: your Topic may
  come out as Unnamed or Neither, your Show as Pair). Tune the word lists at the top of `classify.py`.
- Add misspellings and nicknames to `guest_aliases` (e.g. "giggler", "jinny bass"); the rules do not guess typos.
- Each run re-tags stored comments whose text is still held, so changing a rule or alias applies to everything from
  the last 28 days; comments whose text is already purged keep the tag they had.
- `python sample_neither.py --about Both` (or the "sample-comments" workflow, which has a category dropdown) writes a random
  sample of one category to a short-lived CSV (`comment_sample.csv`) for review and labelling.
- Charts: `what_comments_are_about.png` (per-episode split: Maria, guest, both, show, everything else) and
  `comment_categories.png` (all categories, one bar each).
- Raw comment text is deleted from Turso 28 days after fetching. Tags, scores, like counts and all
  statistics are kept permanently, so charts and the cumulative result are unaffected.
- Alias fixes in `episodes.csv` re-tag only comments whose text is still stored (the first 28 days).
- Exports and the workflow artifact contain no comment text and no usernames.
- If you quote comments in an article, quote only fresh ones, without handles. Check the current policies
  at developers.google.com/youtube/terms/developer-policies.

## Notes
- Cron runs on GitHub's UTC clock and can start a few minutes late. Scheduled workflows in a repo with no
  activity for 60 days are paused by GitHub; a commit to `episodes.csv` keeps it alive.
- Stats are only reported once 3 episodes have 10+ comments naming Maria or the guest.
- Usernames are never collected.
- Local testing: with no Turso variables set, the code uses a local `pretty_tough.db` SQLite file.
- `scrape_reviews.py`, `analyze.py` (Apple Podcasts reviews) and the manual `analyze_youtube.py` CLI are
  kept; `run_weekly.py` reuses functions from `analyze_youtube.py`.
