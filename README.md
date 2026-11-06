# Pontual

How often does each CP train actually run late, and by how much?

Pontual records the final delay of every CP (Comboios de Portugal) train, every day, and turns it into a dashboard with three views:

- **Journey:** pick where you leave from and where you're going (e.g. Aveiro → Porto Campanhã) to see every direct train between them, with lateness measured on arrival at your destination. Journeys that need a change of train aren't covered yet.
- **By station:** every train through one station.
- **Worst trains:** the network-wide ranking.

For each train you choose a threshold and see:

- how many times a week it is **at least X minutes late**
- what share of its runs that is
- its **typical** delay (median) and its **bad-day** delay (90th percentile)
- how often it was cancelled
- a day-by-day history, and a breakdown by weekday

It runs entirely on free GitHub infrastructure. There is no server to maintain.

---

## Setup (about 10 minutes)

1. **Create a new public repository** on GitHub, for example `pontual`.
   Public matters: GitHub Actions minutes are unlimited for public repositories. A private repo would use roughly 3,000 of the 2,000 free minutes a month.
2. **Upload this folder's contents** to the repository, keeping the folder structure (`.github/`, `collector/`, `site/`, `supabase/`, `README.md`, `.gitignore`).
   On github.com: *Add file → Upload files*, then drag the contents of the folder in.
   Note: the `.github` folder is hidden on macOS. Press `Cmd+Shift+.` in Finder to show it.
3. **Turn on GitHub Pages:** *Settings → Pages → Build and deployment → Source: GitHub Actions*.
4. **Allow the collector to save data:** *Settings → Actions → General → Workflow permissions → Read and write permissions → Save*.
5. **Start it:** *Actions tab → Collect delays → Run workflow*. Then do the same for *Publish dashboard*.

Your dashboard will be at `https://<your-username>.github.io/<repo-name>/`.

From then on, collection runs every 15 minutes and the dashboard refreshes hourly, with no further action needed.

### First-day checklist

The collector uses the same live data feed that cp.pt uses. It has been tested against real CP responses, but not live from GitHub's servers, so check these on day one:

- [ ] The first *Collect delays* run is green, and `data/health.json` shows `"ok"` close to `"requests"`. If every request fails, CP may be blocking GitHub's servers. See *Running it elsewhere* below.
- [ ] `data/catalog.json` lists over 1,000 trains after a few runs. (The first run discovers them; it takes a few runs across the day to see them all.)
- [ ] After a few hours, `data/days/<today>.jsonl` fills up, mostly with `"st":"OK"` records.
- [ ] The next morning, yesterday's file includes late-evening trains (due 22:00–00:00). If those are missing, CP switches to the new day's run at midnight. Ask for a fix to fetch them before midnight.

Useful statistics need about **2 weeks** of data. Trends by weekday need about **4–8 weeks**.

### Rider ratings (optional, about 5 minutes)

Each train has a question: *Does this record match your experience?*, with the answers *Matches*, *It's worse*, *It's better*. Each row then shows, for example, "88% of 26 riders agree". Ratings need a small database. Supabase's free tier is plenty.

1. Create a free account at supabase.com and click **New project** (any name and password; pick the *West EU* region).
2. In the project, open **SQL Editor → New query**, paste the whole of `supabase/ratings.sql`, and click **Run**.
3. Open **Project Settings → API**. Copy the **Project URL** and the **publishable** key (older projects call it the **anon public** key).
4. On GitHub, edit `site/config.js` and paste the two values between the quotes. Commit.

The dashboard redeploys automatically and the rating buttons appear. Until `config.js` is filled in, ratings are simply hidden.

How ratings work:
- No sign-in. Each browser gets a random id, so one browser has one vote per train, and a visitor can change or remove their vote.
- Visitors can only call two database functions: one to cast a rating and one to read the totals. They can't read the table, so no one sees who voted what.
- Limitation: someone determined could clear their browser data and vote again. For a community tool that's usually acceptable. If it becomes a problem, sign-in can be added later.
- The publishable key is meant to be public, so it's safe in `config.js`. Never put the **secret** / **service_role** key there.

---

## How it works

```
every 15 min ─ collector/collect.py
   1. read the departure boards of 18 hub stations  →  discover train numbers
   2. for each train whose scheduled arrival has passed:
        COMPLETED  → save final delay at every stop
        CANCELLED  → save as cancelled
        still running → try again next run
        no status an hour after it was due → didn't run today
   3. commit data/ to the repository

hourly ─ collector/build_stats.py  →  site/data/*.json  →  GitHub Pages
```

Each train is fetched **once**, just after it finishes, because CP keeps a finished train's final per-stop delays available. That is about 1,500 small requests a day across the whole network: comparable to a handful of people using the CP app.

The 18 hub stations were chosen so that, between them, their boards see every train in the CP timetable. They are listed at the top of `collect.py`.

### Data

| File | What it holds |
|---|---|
| `data/days/YYYY-MM-DD.jsonl(.gz)` | One line per train per day: service, status, and `[station, scheduled arrival, scheduled departure, delay]` for every stop. Files older than 2 days are gzipped. |
| `data/catalog.json` | Every known train and its current timetable. |
| `data/stations.json` | Station code → name, latitude, longitude. |
| `data/health.json` | Stats from the last collector run. |

The raw data grows by about 55 MB a year.

A service belongs to the day it **departs**. Delays are CP's own figures. Early arrivals count as 0. At a station, the delay is the delay when the train leaves (or arrives, if it ends there). "Worst trains" uses the delay at the final stop.

---

## Running it elsewhere

If CP blocks GitHub's servers, or you want faster collection, run the same script on any always-on machine: a Raspberry Pi, a home server, or a small VPS. Python 3.9+ is all it needs.

```bash
git clone https://github.com/<you>/<repo>.git && cd <repo>
python3 collector/collect.py          # collect once
python3 collector/build_stats.py      # rebuild site/data
python3 -m http.server -d site 8000   # view at http://localhost:8000
```

Cron, every 15 minutes, pushing results to GitHub so Pages keeps publishing:

```
*/15 * * * * cd /path/to/repo && python3 collector/collect.py && git add data && git commit -qm data && git push -q
```

Then disable the *Collect delays* workflow on GitHub (*Actions → Collect delays → ⋯ → Disable workflow*).

---

## Caveats

- **Unofficial.** CP doesn't publish this feed as a public API. It could change without notice; if collection breaks, the *Collect delays* workflow fails and GitHub emails you. Check CP's terms of use before any commercial use.
- **Probabilities, not predictions.** "Late 4 times a week" describes the past. Strikes, weather and engineering works dominate any single day.
- **CP trains only.** Fertagus and the metro systems are separate operators.
- **GitHub schedules are best-effort.** Runs can be delayed by several minutes at busy times. The collector tolerates this, since it retries any train it hasn't recorded yet for up to 8 hours.
