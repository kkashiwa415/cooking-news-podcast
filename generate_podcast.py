"""
Cooking News Podcast Generator
==============================

Fetches top headlines from TechCrunch, BBC, Reuters, CoinDesk, and CoinTelegraph,
deduplicates overlapping stories, generates a ~15 minute podcast script via
Claude Haiku 4.5, and converts it to MP3 audio via Microsoft Edge TTS (free).

Produces two episodes per run: MORNING and EVENING.

Intended cadence: Monday, Thursday, Saturday - set via cron.
Budget: ~$0.07/week (Claude only; Edge TTS is free).

Usage:
    # Generate both episodes using today's headlines
    python generate_podcast.py

    # Generate only the morning edition
    python generate_podcast.py --edition morning

    # Skip audio generation (script only)
    python generate_podcast.py --no-audio

Environment variables required (.env file supported):
    ANTHROPIC_API_KEY   - your Anthropic key (required)
    EDGE_TTS_VOICE      - (optional) default: en-US-AriaNeural
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

import edge_tts           # pip install edge-tts
import feedparser         # pip install feedparser
import requests           # pip install requests
from dotenv import load_dotenv  # pip install python-dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output"
CACHE_DIR = PROJECT_ROOT / "cache"
WEB_DIR = PROJECT_ROOT / "web"
AUDIO_DIR = WEB_DIR / "audio"
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)
WEB_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)

# RSS feeds. Swap any of these freely.
# History of swaps: WSJ dropped (paywall) -> Reuters. BBC -> Seeking Alpha (finance focus).
# CoinTelegraph -> Economist -> MIT Technology Review (AI-heavy tech analysis).
FEEDS: dict[str, str] = {
    "TechCrunch":    "https://techcrunch.com/feed/",
    "SeekingAlpha":  "https://seekingalpha.com/feed.xml",
    "Reuters":       "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "MITTechReview": "https://www.technologyreview.com/feed/",
}

# How many candidate stories to pull from each source before dedup/ranking.
PER_SOURCE_FETCH = 10
# Target stories per episode. 12 at ~1.2 min each ≈ 15 min episode.
TARGET_STORIES_PER_EPISODE = 12
# Deduplication threshold - higher = stricter. 0.72 catches most paraphrases.
DEDUPE_THRESHOLD = 0.72

# Using Haiku instead of Opus: ~20x cheaper, more than good enough for this task.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# Edge TTS is Microsoft's free neural TTS - no API key required.
# Full voice list: run `edge-tts --list-voices` in your terminal.
EDGE_TTS_VOICE_DEFAULT = "en-US-AriaNeural"   # warm, professional female
# Other solid options:
#   en-US-GuyNeural          - friendly male
#   en-US-JennyNeural        - news-anchor female
#   en-GB-SoniaNeural        - British female
#   en-US-AndrewMultilingualNeural - rich male

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Article:
    source: str
    title: str
    link: str
    summary: str
    published: str   # ISO string

    @property
    def fingerprint(self) -> str:
        """Stable ID for dedupe tracking across days."""
        return hashlib.md5(self.link.encode("utf-8")).hexdigest()

    @property
    def norm_title(self) -> str:
        t = self.title.lower()
        t = re.sub(r"[^a-z0-9 ]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t


# ---------------------------------------------------------------------------
# Step 1: Fetching
# ---------------------------------------------------------------------------

def strip_html(s: str) -> str:
    """Rough HTML strip for RSS summaries."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fetch_feed(source: str, url: str) -> list[Article]:
    print(f"  Fetching {source}...", flush=True)
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 (news-podcast-bot)"})
    except Exception as e:
        print(f"    ! failed: {e}")
        return []

    articles: list[Article] = []
    for entry in parsed.entries[:PER_SOURCE_FETCH]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        # Cap summary length - full articles bust context budget.
        if len(summary) > 1200:
            summary = summary[:1200] + "..."

        published = entry.get("published") or entry.get("updated") or ""
        articles.append(Article(
            source=source,
            title=title,
            link=link,
            summary=summary,
            published=published,
        ))
    print(f"    got {len(articles)} stories")
    return articles


def fetch_all() -> list[Article]:
    print("Fetching feeds...")
    all_articles: list[Article] = []
    for source, url in FEEDS.items():
        all_articles.extend(fetch_feed(source, url))
    print(f"Total fetched: {len(all_articles)} articles\n")
    return all_articles


# ---------------------------------------------------------------------------
# Step 2: Dedupe
# ---------------------------------------------------------------------------

def titles_similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def dedupe(articles: list[Article]) -> list[Article]:
    """
    Drop near-duplicate stories. When two articles cover the same story,
    prefer the richer summary (longer). Tie-break on source priority order.
    """
    print("Deduplicating...")
    source_priority = {name: i for i, name in enumerate(FEEDS.keys())}
    kept: list[Article] = []

    for art in articles:
        matched_idx = None
        for i, existing in enumerate(kept):
            if titles_similar(art.norm_title, existing.norm_title) >= DEDUPE_THRESHOLD:
                matched_idx = i
                break

        if matched_idx is None:
            kept.append(art)
            continue

        # Found a dupe. Keep the better one.
        existing = kept[matched_idx]
        art_score = (len(art.summary), -source_priority.get(art.source, 99))
        ex_score = (len(existing.summary), -source_priority.get(existing.source, 99))
        if art_score > ex_score:
            print(f"  replacing [{existing.source}] '{existing.title[:60]}...' with [{art.source}]")
            kept[matched_idx] = art
        else:
            print(f"  dropping dupe [{art.source}] '{art.title[:60]}...'")

    print(f"After dedupe: {len(kept)} articles\n")
    return kept


# ---------------------------------------------------------------------------
# Step 3: Cross-day dedupe (no repeats if story already covered yesterday)
# ---------------------------------------------------------------------------

def load_seen_fingerprints() -> set[str]:
    f = CACHE_DIR / "seen_articles.json"
    if not f.exists():
        return set()
    try:
        data = json.loads(f.read_text())
        # Keep only the last 7 days worth
        cutoff = time.time() - 7 * 86400
        return {fp for fp, ts in data.items() if ts > cutoff}
    except Exception:
        return set()


def save_seen_fingerprints(existing: set[str], new_articles: Iterable[Article]) -> None:
    f = CACHE_DIR / "seen_articles.json"
    now = time.time()
    data: dict[str, float] = {}
    if f.exists():
        try:
            data = json.loads(f.read_text())
        except Exception:
            data = {}
    for art in new_articles:
        data[art.fingerprint] = now
    # Trim old entries
    cutoff = now - 7 * 86400
    data = {fp: ts for fp, ts in data.items() if ts > cutoff}
    f.write_text(json.dumps(data))


def filter_already_covered(articles: list[Article], seen: set[str]) -> list[Article]:
    fresh = [a for a in articles if a.fingerprint not in seen]
    dropped = len(articles) - len(fresh)
    if dropped:
        print(f"Skipped {dropped} article(s) already covered in the last 7 days\n")
    return fresh


# ---------------------------------------------------------------------------
# Step 4: Split into morning + evening
# ---------------------------------------------------------------------------

def split_editions(articles: list[Article]) -> tuple[list[Article], list[Article]]:
    """
    Split the pool into a morning and evening edition.

    Strategy: walk each source's articles in order and alternate between
    morning and evening so coverage stays balanced even when sources are thin.
    Round-robin across sources for diversity within each edition.
    """
    by_source: dict[str, list[Article]] = {s: [] for s in FEEDS.keys()}
    for a in articles:
        if a.source in by_source:
            by_source[a.source].append(a)

    morning: list[Article] = []
    evening: list[Article] = []
    alternator = 0  # 0 = morning gets next, 1 = evening gets next

    # Round-robin: take the next article from each source in turn, alternating editions.
    # This guarantees both editions get cross-source coverage even with thin feeds.
    max_per_source = max((len(v) for v in by_source.values()), default=0)
    for depth in range(max_per_source):
        for source, items in by_source.items():
            if depth >= len(items):
                continue
            if len(morning) >= TARGET_STORIES_PER_EPISODE and len(evening) >= TARGET_STORIES_PER_EPISODE:
                break
            target = morning if alternator == 0 else evening
            if len(target) < TARGET_STORIES_PER_EPISODE:
                target.append(items[depth])
                alternator = 1 - alternator
            else:
                # Target edition is full - put in the other one
                other = evening if alternator == 0 else morning
                if len(other) < TARGET_STORIES_PER_EPISODE:
                    other.append(items[depth])

    return morning[:TARGET_STORIES_PER_EPISODE], evening[:TARGET_STORIES_PER_EPISODE]


# ---------------------------------------------------------------------------
# Step 5: Claude - generate narration script
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a professional podcast host writing a news briefing \
that will be read aloud by a text-to-speech engine while the listener is cooking.

Your output will be spoken directly - so write ONLY the words the host should say. \
No stage directions, no bracketed notes, no headers, no markdown. Write in flowing, \
conversational prose with natural transitions between stories. Contractions are good. \
Numbers should be written in a TTS-friendly way (e.g. "twelve billion dollars" not "$12B").

IMPORTANT: Do NOT name or attribute sources. Never say "according to TechCrunch", \
"Reuters is reporting", "as Seeking Alpha notes", etc. Just state the news directly \
as a knowledgeable host would. The listener does not care where the story came from.

For EACH story, cover these five beats, but weave them into smooth prose rather than \
labelling them. Do not use the words "What happened", "Why", etc. as section headers:
  1. What happened
  2. Why it happened / the context behind it
  3. Who is affected
  4. Why it matters
  5. The outlook - what to watch for next

Episode structure:
- Open with a warm greeting that mentions the edition (morning or evening) and \
  teases the top story.
- Transition smoothly between stories ("In other news...", "Turning to crypto...", etc.).
- Close with a short sign-off.

Target length: roughly 2,800 to 3,200 words, which reads to about 18 to 20 minutes \
at a normal podcast pace. Cover every story the listener is given - don't skip any. \
Give each story enough room to breathe (around 200 words each) but don't pad.
"""

USER_TEMPLATE = """Today is {date}. This is the {edition} edition.

Here are the {n} top stories to cover. Cover ALL of them - don't skip any. \
You may reorder slightly for better flow (e.g. group related stories together, \
move crypto stories toward the end). Remember: no source attribution in the \
spoken text - just tell the news.

{articles_block}

Write the full podcast script now. Remember: spoken prose only, no markdown, \
no stage directions, no source names."""


def build_articles_block(articles: list[Article]) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        lines.append(f"[{i}] SOURCE: {a.source}")
        lines.append(f"    TITLE: {a.title}")
        lines.append(f"    SUMMARY: {a.summary or '(no summary available)'}")
        lines.append(f"    LINK: {a.link}")
        lines.append("")
    return "\n".join(lines)


def generate_script(articles: list[Article], edition: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    user_content = USER_TEMPLATE.format(
        date=datetime.now().strftime("%A, %B %d, %Y"),
        edition=edition.upper(),
        n=len(articles),
        articles_block=build_articles_block(articles),
    )

    print(f"Generating {edition} script via Claude {ANTHROPIC_MODEL}...")

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 12000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    script = "\n".join(parts).strip()
    word_count = len(script.split())
    print(f"  script ready - {word_count} words (~{word_count / 150:.1f} min spoken)\n")
    return script


# ---------------------------------------------------------------------------
# Step 6: Edge TTS - text to speech (free, no API key)
# ---------------------------------------------------------------------------

async def _edge_tts_save(text: str, voice: str, out_path: Path) -> None:
    """Stream Edge TTS output to disk. Edge handles arbitrarily long inputs."""
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        # Slight rate/pitch tweaks make it feel less robotic for news delivery.
        rate="+0%",
        pitch="+0Hz",
    )
    await communicate.save(str(out_path))


def synthesize_audio(script: str, out_path: Path) -> None:
    voice = os.environ.get("EDGE_TTS_VOICE", EDGE_TTS_VOICE_DEFAULT)
    char_count = len(script)
    print(f"Synthesizing audio via Edge TTS ({voice}) - {char_count:,} chars -> {out_path.name}")

    try:
        asyncio.run(_edge_tts_save(script, voice, out_path))
    except Exception as e:
        raise RuntimeError(f"Edge TTS failed: {e}")

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("Edge TTS produced an empty file - check your internet connection and voice name.")

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"  wrote {out_path} ({size_mb:.1f} MB)\n")


# ---------------------------------------------------------------------------
# Step 7: Web manifest - list all episodes for the player
# ---------------------------------------------------------------------------

EPISODE_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(morning|evening)\.mp3$")


def _read_duration_seconds(path: Path) -> int:
    """Read MP3 duration in seconds. Falls back to a bitrate-based estimate."""
    try:
        from mutagen.mp3 import MP3  # type: ignore
        audio = MP3(str(path))
        if audio.info and audio.info.length:
            return int(round(audio.info.length))
    except Exception:
        pass
    # Fallback: assume ~24 kB/s (roughly 192 kbps) - close enough for display.
    try:
        return int(round(path.stat().st_size / 24000))
    except Exception:
        return 0


def build_manifest() -> None:
    """Scan AUDIO_DIR for MP3s and write web/episodes.json, newest first."""
    episodes = []
    for mp3 in sorted(AUDIO_DIR.glob("*.mp3")):
        m = EPISODE_FILENAME_RE.match(mp3.name)
        if not m:
            continue
        date_str, edition = m.group(1), m.group(2)
        episodes.append({
            "date": date_str,
            "edition": edition,
            "file": f"audio/{mp3.name}",
            "duration_sec": _read_duration_seconds(mp3),
        })

    # Newest first: sort by date desc, then evening before morning on the same day.
    edition_rank = {"evening": 0, "morning": 1}
    episodes.sort(key=lambda e: (e["date"], -edition_rank.get(e["edition"], 9)), reverse=True)

    manifest = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "episodes": episodes,
    }
    manifest_path = WEB_DIR / "episodes.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest: {manifest_path} ({len(episodes)} episode(s))")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def produce_episode(
    articles: list[Article],
    edition: str,
    make_audio: bool,
) -> None:
    if not articles:
        print(f"No articles for {edition} edition - skipping.\n")
        return

    print(f"=== {edition.upper()} EDITION: {len(articles)} stories ===")
    for i, a in enumerate(articles, 1):
        print(f"  {i}. [{a.source}] {a.title[:80]}")
    print()

    date_stamp = datetime.now().strftime("%Y-%m-%d")
    script_path = OUTPUT_DIR / f"{date_stamp}_{edition}.txt"
    audio_path = AUDIO_DIR / f"{date_stamp}_{edition}.mp3"
    meta_path = OUTPUT_DIR / f"{date_stamp}_{edition}.meta.json"

    script = generate_script(articles, edition)
    script_path.write_text(script, encoding="utf-8")
    print(f"  saved script -> {script_path}")

    meta = {
        "date": date_stamp,
        "edition": edition,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": ANTHROPIC_MODEL,
        "articles": [asdict(a) for a in articles],
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    if make_audio:
        synthesize_audio(script, audio_path)
    else:
        print("  (skipping audio generation per flag)\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a cooking-news podcast.")
    parser.add_argument(
        "--edition",
        choices=["morning", "evening", "both"],
        default="both",
        help="Which edition(s) to produce (default: both).",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Skip ElevenLabs TTS; write text scripts only.",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Don't filter out stories covered in previous runs.",
    )
    args = parser.parse_args()

    articles = fetch_all()
    articles = dedupe(articles)

    seen = set() if args.no_memory else load_seen_fingerprints()
    articles = filter_already_covered(articles, seen)

    morning, evening = split_editions(articles)

    if args.edition in ("morning", "both"):
        produce_episode(morning, "morning", make_audio=not args.no_audio)
    if args.edition in ("evening", "both"):
        produce_episode(evening, "evening", make_audio=not args.no_audio)

    # Remember what we covered so tomorrow's run skips repeats
    save_seen_fingerprints(seen, morning + evening)

    # Rebuild the web player's manifest so the newest episode shows up.
    build_manifest()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
