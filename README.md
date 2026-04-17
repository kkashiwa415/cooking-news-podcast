# Cooking News Podcast

A one-tap personal news podcast for cooking. Every run pulls the top stories
from 5 sources, writes two 18-20 minute scripts (morning + evening), narrates
them with neural TTS, and publishes a minimalist web player. You save one
bookmark to your phone, tap it while prepping dinner, and the newest episode
plays.

Runs on autopilot: GitHub Actions generates episodes Monday / Thursday /
Saturday, Vercel hosts the player for free.

- **Script writer:** Claude Haiku 4.5
- **Narrator:** Microsoft Edge TTS (free, no API key)
- **Cost:** ~$0.10/week for Claude; everything else is free

## How the pieces fit together

```
 RSS feeds ──► generate_podcast.py ──► output/*.txt (transcripts)
                     │                 output/*.meta.json (article sources)
                     │                 web/audio/*.mp3 (episodes)
                     ▼
              web/episodes.json (manifest of newest-first episodes)
                     │
                     ▼
              web/index.html  ◄── Vercel serves this; your phone opens it
```

## Sources

TechCrunch · Seeking Alpha · Reuters · CoinDesk · MIT Technology Review

12 stories per edition. No source attribution in the spoken script — just the
news.

## Local setup (test before deploying)

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Copy the env template and add your Anthropic key
cp .env.example .env
# edit .env and paste your key
```

Get an Anthropic key: <https://console.anthropic.com/>

### Generate a dry-run script (no audio)

```bash
python generate_podcast.py --no-audio --edition morning
```

Confirms feeds work and Claude can write the script. No API calls to TTS.

### Generate a real episode

```bash
python generate_podcast.py --edition morning
```

Output:

```
output/2026-04-20_morning.txt         # transcript
output/2026-04-20_morning.meta.json   # article sources, timestamps
web/audio/2026-04-20_morning.mp3      # audio (~18-20 min)
web/episodes.json                     # manifest for the player
```

### Preview the web player locally

```bash
python -m http.server 8000 --directory web
```

Then open <http://localhost:8000>. You should see a big amber play button and
the newest episode label.

## Deploy — full walkthrough

This gets you from "folder on my laptop" to "bookmark on my phone that plays
the newest episode." Each step is one action. You do not need any prior
devops experience.

### 1. Put the code on GitHub

1. Create a GitHub account at <https://github.com> if you don't have one.
2. Click **+** in the top right → **New repository**. Name it something like
   `cooking-news-podcast`. Public is fine — the API key never lives in the
   code, only in GitHub's encrypted secrets.
3. On your computer, open a terminal in this project folder and run:

   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/cooking-news-podcast.git
   git push -u origin main
   ```

   Replace `YOUR_USERNAME` with your GitHub username.

### 2. Add your Anthropic key as a GitHub secret

1. On GitHub, open your new repo → **Settings** (top tabs).
2. Left sidebar → **Secrets and variables** → **Actions**.
3. Click **New repository secret**.
4. Name: `ANTHROPIC_API_KEY`. Value: paste your key (`sk-ant-...`). Save.

**Never paste the key anywhere else** — not in README, not in code, not in
commit messages.

### 3. Test the GitHub Action manually

1. In your repo, click the **Actions** tab.
2. Pick the **Generate Podcast** workflow on the left.
3. Click **Run workflow** → leave edition as `both` → **Run workflow**.
4. Wait about 2-3 minutes. Click the run to watch logs. On success, new commits
   appear on `main` with `2026-XX-XX_morning.mp3`, `_evening.mp3`, and an
   updated `episodes.json`.

### 4. Deploy the player to Vercel

1. Go to <https://vercel.com/signup> and sign in with your GitHub account.
   (Sign-in-with-GitHub is simplest; it auto-connects your repos.)
2. Click **Add New** → **Project**.
3. Find your `cooking-news-podcast` repo in the list and click **Import**.
4. On the configuration screen:
   - **Framework Preset:** Other
   - **Root Directory:** click *Edit* → select `web`
   - Build Command / Output Directory: leave empty (pure static files)
5. Click **Deploy**. 30-60 seconds later Vercel gives you a URL like
   `cooking-news-podcast-yourname.vercel.app`.

### 5. Add it to your phone's home screen

**iPhone (Safari):**

1. Open the Vercel URL in Safari.
2. Tap the **Share** icon → **Add to Home Screen** → **Add**.
3. Tap the new icon on your home screen. It opens full-screen — no browser UI.
4. Tap the big play button. Done.

**Android (Chrome):**

1. Open the Vercel URL in Chrome.
2. Tap the three-dot menu → **Install app** (or **Add to Home screen**).
3. Tap the icon on your home screen and tap play.

The web app caches the episode list in localStorage, so if you tap the
bookmark with flaky signal it still shows the last-known newest episode.

## From now on, episodes appear automatically

Every Monday, Thursday, and Saturday at 8 AM and 7 PM Eastern, GitHub Actions
runs the script, commits new MP3s + a refreshed `episodes.json` to your repo,
and Vercel auto-deploys within a minute. Your home-screen icon always plays
the latest.

To change the schedule, edit the `cron:` lines in
`.github/workflows/generate.yml`. Cron days-of-week: `0`=Sun, `1`=Mon,
`2`=Tue, `3`=Wed, `4`=Thu, `5`=Fri, `6`=Sat. Times are UTC.

## Customization

| What | Where |
|---|---|
| Swap / add RSS feeds | `FEEDS` dict near the top of `generate_podcast.py` |
| Stories per episode | `TARGET_STORIES_PER_EPISODE` (default 12) |
| Dedup aggressiveness | `DEDUPE_THRESHOLD` (0.0 - 1.0, default 0.72) |
| Voice personality / prompt | `SYSTEM_PROMPT` in `generate_podcast.py` |
| TTS voice | `EDGE_TTS_VOICE` in `.env`. List voices: `edge-tts --list-voices` |
| Player colors / fonts | `web/index.html` — the `:root` CSS vars and the Google Fonts `<link>` |
| Script quality (upgrade to Opus) | Change `ANTHROPIC_MODEL` to `claude-opus-4-7` (~$0.30/day extra) |

## Troubleshooting

**GitHub Action fails with `ANTHROPIC_API_KEY not set`**
The secret is missing or named wrong. Settings → Secrets and variables →
Actions. Must be exactly `ANTHROPIC_API_KEY`.

**GitHub Action fails with `403` on push**
Under repo Settings → Actions → General → Workflow permissions, select
**Read and write permissions**, then re-run.

**Vercel deploy succeeded but player shows "Can't reach the episode list"**
The manifest path is wrong. Confirm `web/episodes.json` is in the repo and
Vercel's Root Directory is `web` (not the repo root). Force a fresh deploy
from the Vercel **Deployments** tab.

**Player loads but audio doesn't play on iOS**
iOS Safari requires a real user tap before audio plays. The big play button
counts — if it doesn't start on tap, check the browser console. Most
commonly this is a 404 on the MP3: confirm the MP3 exists at the URL shown
in the `file` field of `episodes.json` relative to the player.

**Vercel warning: deployment approaching 100 MB limit**
At ~5 MB per MP3 × 6 episodes/week, you'll hit Vercel's free-tier
deployment limit in ~3 months. When it matters, either:
- Delete MP3s older than 2 weeks from `web/audio/` in the repo, or
- Move audio to GitHub's raw-file hosting and change `episodes.json` file
  paths to full `https://raw.githubusercontent.com/...` URLs (Vercel
  redeploys stay small, and raw.githubusercontent.com serves MP3s fine).

**Lock-screen controls don't show on iOS**
The Media Session API is wired up, but iOS Safari is inconsistent. Works
reliably on Chrome/Android. If iOS doesn't show artwork, it still
shows title + artist on the lock screen.

**Edge TTS fails with a connection error**
Edge TTS is an unofficial use of Microsoft's neural voices. It occasionally
rate-limits or drops. The script raises a clear error. Usually retrying
fixes it. If it breaks long-term, swap `synthesize_audio` to use `gTTS` or
`pyttsx3` — about 5 lines of code.

**I want to manually delete an episode**
Delete the MP3 in `web/audio/` and the matching files in `output/`, commit,
push. On the next run, the manifest is rebuilt from whatever MP3s remain.

## Known limitations

- RSS summaries are often short. For richer narration, add article-body
  fetching with `trafilatura` (roughly triples Claude cost).
- Edge TTS is unofficial. If it breaks, switching TTS providers is a small
  code change.
- Seeking Alpha may rate-limit aggressive polling; the User-Agent header in
  the script helps.
- MIT Technology Review has no AI-only RSS feed, so the site-wide feed is
  used — mostly AI but occasional biotech / climate.

## Files at a glance

```
generate_podcast.py       # the generator (fetch → dedup → script → audio → manifest)
requirements.txt          # Python deps
.env.example              # copy to .env and fill in your key
.github/workflows/
  generate.yml            # scheduled GitHub Actions job
web/
  index.html              # the player (single file)
  manifest.webmanifest    # PWA metadata for Add to Home Screen
  icon.svg                # app icon
  episodes.json           # rebuilt every run
  audio/*.mp3             # episodes, committed to the repo
output/
  *.txt                   # transcripts
  *.meta.json             # article sources, generation timestamps
cache/
  seen_articles.json      # cross-day dedup memory (7-day rolling)
```
