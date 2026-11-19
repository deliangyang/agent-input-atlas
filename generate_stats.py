#!/usr/bin/env python3
"""
Extract short user inputs from AI agent session logs and build a word-cloud
Top-N stats page.

Supported agents: cursor / claude / codex (alias: xcode) / gemini / kimi / pi

Usage:
    python generate_stats.py
    python generate_stats.py --top 50 --max-chars 10 --max-words 10
    python generate_stats.py --agents cursor,claude,codex --output output/stats.html
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_INVALID_ARGS = 2

HOME = Path.home()

# Agent aliases: xcode -> codex
AGENT_ALIASES = {
    "xcode": "codex",
    "claude-code": "claude",
    "claude_code": "claude",
}

DEFAULT_SOURCES: Dict[str, Path] = {
    "cursor": HOME / ".cursor" / "projects",
    "claude": HOME / ".claude" / "projects",
    "codex": HOME / ".codex" / "sessions",
    "gemini": HOME / ".gemini" / "tmp",
    "kimi": HOME / ".kimi-code",
    "pi": HOME / ".pi" / "agent" / "sessions",
}

# Drop whole messages that start with these noise prefixes
NOISE_PREFIXES = (
    "<ide_opened_file>",
    "<ide_selection>",
    "<ide_opened_resource>",
    "<command-message>",
    "<command-name>",
    "<local-command",
    "<environment_context>",
    "<turn_aborted>",
    "<skill>",
    "<system-reminder>",
    "[ai-team",
    "[Image:",
    "You are the Noop",
    "Execute the selected diff-tab",
    "Execute the selected diff",
)

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>", re.DOTALL | re.IGNORECASE)
MULTI_SPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a word-cloud page from short agent session inputs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --top 80 --max-chars 10
  %(prog)s --agents cursor,claude,codex
  %(prog)s --agents xcode   # xcode is treated as codex
        """,
    )
    parser.add_argument(
        "--top",
        type=int,
        default=40,
        help="Top N phrases per agent (default: 40)",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=10,
        help="Max character length for CJK-heavy text (default: 10)",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=10,
        help="Max word count for non-CJK text (default: 10)",
    )
    parser.add_argument(
        "--agents",
        type=str,
        default="cursor,claude,codex,gemini,kimi,pi",
        help="Comma-separated agents; alias xcode=codex is supported",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "output" / "wordcloud.html",
        help="Output HTML path",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="Minimum occurrence count to include (default: 1)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser.parse_args()


def normalize_agent_name(name: str) -> str:
    key = name.strip().lower()
    return AGENT_ALIASES.get(key, key)


def extract_text_blocks(content) -> str:
    """Extract plain text from message.content (str or list)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "input_text"):
                    parts.append(item.get("text") or "")
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def clean_user_text(raw: str) -> Optional[str]:
    """Strip system tags and keep the real user query."""
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # Cursor (and some tools) wrap the real query in <user_query>
    m = USER_QUERY_RE.search(text)
    if m:
        text = m.group(1).strip()
    else:
        text = TIMESTAMP_RE.sub("", text).strip()

    lowered = text.lstrip()
    for prefix in NOISE_PREFIXES:
        if lowered.startswith(prefix):
            return None

    if "<tool_result" in text[:80] or text.startswith("[{"):
        return None
    if text.startswith("Caveat:") or text.startswith("[Request interrupted"):
        return None

    text = TAG_STRIP_RE.sub(" ", text)
    text = MULTI_SPACE_RE.sub(" ", text).strip()

    if not text or text in {".", "...", "?", "？", "!", "！"}:
        return None

    if text.startswith("Execute the selected") or text.startswith("[Image:"):
        return None

    return text


def is_short_input(text: str, max_chars: int, max_words: int) -> bool:
    """
    Short-input check:
    - CJK-heavy text: non-whitespace length <= max_chars
    - otherwise: whitespace-split word count <= max_words
    """
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False

    cjk_count = len(CJK_RE.findall(text))
    if cjk_count >= max(1, int(len(compact) * 0.3)):
        return len(compact) <= max_chars
    return len(text.split()) <= max_words


# ---------------------------------------------------------------------------
# Per-agent parsers
# ---------------------------------------------------------------------------


def iter_cursor_inputs(root: Path) -> Iterator[str]:
    if not root.exists():
        return
    for path in root.rglob("*.jsonl"):
        if "agent-transcript" not in str(path):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("role") != "user":
                        continue
                    msg = obj.get("message") or {}
                    raw = extract_text_blocks(msg.get("content"))
                    cleaned = clean_user_text(raw)
                    if cleaned:
                        yield cleaned
        except OSError as exc:
            logger.debug("skip %s: %s", path, exc)


def iter_claude_inputs(root: Path) -> Iterator[str]:
    if not root.exists():
        return
    for path in root.rglob("*.jsonl"):
        if "subagents" in path.parts:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    msg = obj.get("message") or {}
                    raw = extract_text_blocks(msg.get("content"))
                    cleaned = clean_user_text(raw)
                    if cleaned:
                        yield cleaned
        except OSError as exc:
            logger.debug("skip %s: %s", path, exc)


def iter_codex_inputs(root: Path) -> Iterator[str]:
    """Prefer event_msg.user_message to avoid environment_context noise."""
    if not root.exists():
        return
    for path in root.rglob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "event_msg":
                        continue
                    payload = obj.get("payload") or {}
                    if payload.get("type") != "user_message":
                        continue
                    raw = payload.get("message") or ""
                    cleaned = clean_user_text(raw)
                    if cleaned:
                        yield cleaned
        except OSError as exc:
            logger.debug("skip %s: %s", path, exc)


def iter_gemini_inputs(root: Path) -> Iterator[str]:
    if not root.exists():
        return
    for path in root.rglob("session-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("skip %s: %s", path, exc)
            continue
        for msg in data.get("messages") or []:
            if msg.get("type") != "user":
                continue
            cleaned = clean_user_text(str(msg.get("content") or ""))
            if cleaned:
                yield cleaned


def iter_kimi_inputs(root: Path) -> Iterator[str]:
    if not root.exists():
        return
    # user-history is the cleanest source
    hist = root / "user-history"
    if hist.exists():
        for path in hist.glob("*.jsonl"):
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        raw = obj.get("content") or obj.get("text") or ""
                        cleaned = clean_user_text(str(raw))
                        if cleaned:
                            yield cleaned
            except OSError as exc:
                logger.debug("skip %s: %s", path, exc)

    sessions = root / "sessions"
    if not sessions.exists():
        return
    for path in sessions.rglob("wire.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") not in ("message", "user_message", "chat.message"):
                        if obj.get("role") != "user" and obj.get("type") != "user":
                            continue
                    raw = (
                        obj.get("content")
                        or obj.get("text")
                        or extract_text_blocks(
                            obj.get("message", {}).get("content")
                            if isinstance(obj.get("message"), dict)
                            else None
                        )
                        or ""
                    )
                    cleaned = clean_user_text(str(raw))
                    if cleaned:
                        yield cleaned
        except OSError as exc:
            logger.debug("skip %s: %s", path, exc)


def iter_pi_inputs(root: Path) -> Iterator[str]:
    if not root.exists():
        return
    for path in root.rglob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "message":
                        continue
                    msg = obj.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    raw = extract_text_blocks(msg.get("content"))
                    cleaned = clean_user_text(raw)
                    if cleaned:
                        yield cleaned
        except OSError as exc:
            logger.debug("skip %s: %s", path, exc)


PARSERS: Dict[str, Callable[[Path], Iterator[str]]] = {
    "cursor": iter_cursor_inputs,
    "claude": iter_claude_inputs,
    "codex": iter_codex_inputs,
    "gemini": iter_gemini_inputs,
    "kimi": iter_kimi_inputs,
    "pi": iter_pi_inputs,
}


def collect_stats(
    agents: List[str],
    max_chars: int,
    max_words: int,
    min_count: int,
) -> Dict[str, dict]:
    """Return {agent: {total_raw, total_short, unique, items}}."""
    result: Dict[str, dict] = {}

    for agent in agents:
        root = DEFAULT_SOURCES.get(agent)
        parser = PARSERS.get(agent)
        if not root or not parser:
            logger.warning("unknown agent, skip: %s", agent)
            continue
        if not root.exists():
            logger.warning("path missing, skip %s: %s", agent, root)
            result[agent] = {"total_raw": 0, "total_short": 0, "unique": 0, "items": []}
            continue

        logger.info("scanning %s -> %s", agent, root)
        counter: Counter = Counter()
        total_raw = 0
        for text in parser(root):
            total_raw += 1
            if is_short_input(text, max_chars=max_chars, max_words=max_words):
                counter[text.strip()] += 1

        items = [
            {"text": t, "count": c}
            for t, c in counter.most_common()
            if c >= min_count
        ]
        result[agent] = {
            "total_raw": total_raw,
            "total_short": sum(counter.values()),
            "unique": len(counter),
            "items": items,
        }
        logger.info(
            "  %s: raw=%d short=%d unique=%d",
            agent,
            total_raw,
            sum(counter.values()),
            len(counter),
        )

    return result


def build_html(stats: Dict[str, dict], top_n: int, max_chars: int, max_words: int) -> str:
    """Build a self-contained multilingual stats page."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    payload = {}
    for agent, data in stats.items():
        items = data.get("items") or []
        top_items = items[:top_n]
        payload[agent] = {
            "total_raw": data.get("total_raw", 0),
            "total_short": data.get("total_short", 0),
            "unique": data.get("unique", 0),
            "top": top_items,
            "cloud": [[it["text"], it["count"]] for it in top_items],
        }

    all_counter: Counter = Counter()
    total_raw_all = 0
    total_short_all = 0
    for data in stats.values():
        total_raw_all += data.get("total_raw", 0)
        total_short_all += data.get("total_short", 0)
        for it in data.get("items") or []:
            all_counter[it["text"]] += it["count"]
    all_items = [{"text": t, "count": c} for t, c in all_counter.most_common(top_n)]
    payload["all"] = {
        "total_raw": total_raw_all,
        "total_short": total_short_all,
        "unique": len(all_counter),
        "top": all_items,
        "cloud": [[it["text"], it["count"]] for it in all_items],
    }

    agents_order = ["all"] + [a for a in stats.keys()]
    data_json = json.dumps(payload, ensure_ascii=False)
    agents_json = json.dumps(agents_order, ensure_ascii=False)
    meta_json = json.dumps(
        {
            "generated_at": generated_at,
            "top_n": top_n,
            "max_chars": max_chars,
            "max_words": max_words,
        },
        ensure_ascii=False,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Agent Short Input Word Cloud</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,600;0,9..40,700;1,9..40,400&family=Noto+Sans+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet" />
<script src="https://cdn.jsdelivr.net/npm/wordcloud@1.2.2/src/wordcloud2.min.js"></script>
<style>
  :root {{
    --bg0: #0f1419;
    --bg1: #171d25;
    --ink: #e8eef4;
    --muted: #8b9aab;
    --line: rgba(232, 238, 244, 0.08);
    --accent: #3ecf8e;
    --accent-dim: rgba(62, 207, 142, 0.15);
    --warm: #f0a46e;
    --card: rgba(255,255,255,0.03);
    --shadow: 0 24px 60px rgba(0,0,0,0.35);
    --radius: 18px;
    --font: "DM Sans", "Noto Sans SC", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, monospace;
  }}

  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: var(--font);
    color: var(--ink);
    background:
      radial-gradient(1200px 600px at 10% -10%, rgba(62,207,142,0.12), transparent 55%),
      radial-gradient(900px 500px at 90% 0%, rgba(240,164,110,0.10), transparent 50%),
      linear-gradient(180deg, #0c1015 0%, var(--bg0) 40%, #121820 100%);
    min-height: 100vh;
  }}

  .wrap {{
    width: min(1180px, calc(100% - 2rem));
    margin: 0 auto;
    padding: 2.5rem 0 4rem;
  }}

  header.hero {{
    display: grid;
    gap: 0.75rem;
    margin-bottom: 2rem;
    animation: rise 0.7s ease both;
  }}
  .hero-top {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }}
  .eyebrow {{
    font-family: var(--mono);
    font-size: 0.75rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
  }}
  .lang-switch {{
    display: inline-flex;
    gap: 0.25rem;
    padding: 0.2rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: var(--card);
  }}
  .lang-btn {{
    appearance: none;
    border: 0;
    background: transparent;
    color: var(--muted);
    font-family: var(--mono);
    font-size: 0.72rem;
    font-weight: 600;
    padding: 0.35rem 0.7rem;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }}
  .lang-btn.active {{
    background: var(--accent);
    color: #04140c;
  }}
  h1 {{
    margin: 0;
    font-size: clamp(1.8rem, 4vw, 2.6rem);
    font-weight: 700;
    letter-spacing: -0.03em;
    line-height: 1.15;
  }}
  .sub {{
    margin: 0;
    color: var(--muted);
    max-width: 46rem;
    line-height: 1.6;
  }}
  .meta {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem 1rem;
    margin-top: 0.4rem;
    font-family: var(--mono);
    font-size: 0.78rem;
    color: var(--muted);
  }}
  .meta span {{
    background: var(--card);
    border: 1px solid var(--line);
    padding: 0.28rem 0.65rem;
    border-radius: 999px;
  }}

  .tabs {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin: 1.5rem 0 1.25rem;
    animation: rise 0.7s ease 0.08s both;
  }}
  .tab {{
    appearance: none;
    border: 1px solid var(--line);
    background: var(--card);
    color: var(--muted);
    font-family: var(--font);
    font-weight: 600;
    font-size: 0.9rem;
    padding: 0.55rem 1rem;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.2s, color 0.2s, border-color 0.2s, transform 0.15s;
  }}
  .tab:hover {{ color: var(--ink); border-color: rgba(62,207,142,0.35); transform: translateY(-1px); }}
  .tab.active {{
    color: #04140c;
    background: var(--accent);
    border-color: var(--accent);
  }}

  .stats {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.85rem;
    margin-bottom: 1.25rem;
    animation: rise 0.7s ease 0.12s both;
  }}
  @media (max-width: 720px) {{
    .stats {{ grid-template-columns: 1fr; }}
  }}
  .stat {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 1rem 1.15rem;
    box-shadow: var(--shadow);
  }}
  .stat .label {{
    font-size: 0.78rem;
    color: var(--muted);
    letter-spacing: 0.04em;
  }}
  .stat .value {{
    margin-top: 0.35rem;
    font-size: 1.7rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }}
  .stat .value em {{
    font-style: normal;
    color: var(--accent);
  }}

  .grid {{
    display: grid;
    grid-template-columns: 1.2fr 0.8fr;
    gap: 1rem;
    animation: rise 0.7s ease 0.18s both;
  }}
  @media (max-width: 900px) {{
    .grid {{ grid-template-columns: 1fr; }}
  }}

  .panel {{
    background: linear-gradient(160deg, rgba(255,255,255,0.04), rgba(255,255,255,0.015));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
    min-height: 420px;
  }}
  .panel-hd {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    padding: 1rem 1.15rem 0.5rem;
    border-bottom: 1px solid var(--line);
  }}
  .panel-hd h2 {{
    margin: 0;
    font-size: 1rem;
    font-weight: 700;
  }}
  .panel-hd small {{
    color: var(--muted);
    font-family: var(--mono);
    font-size: 0.72rem;
  }}

  #cloud {{
    width: 100%;
    height: 460px;
    cursor: default;
  }}

  .list {{
    max-height: 520px;
    overflow: auto;
    padding: 0.4rem 0.4rem 0.8rem;
  }}
  .row {{
    display: grid;
    grid-template-columns: 2.2rem 1fr auto;
    gap: 0.7rem;
    align-items: center;
    padding: 0.65rem 0.75rem;
    border-radius: 12px;
    transition: background 0.15s;
  }}
  .row:hover {{ background: rgba(62,207,142,0.08); }}
  .rank {{
    font-family: var(--mono);
    font-size: 0.78rem;
    color: var(--muted);
    text-align: right;
  }}
  .rank.top3 {{ color: var(--warm); font-weight: 600; }}
  .phrase {{
    font-size: 0.95rem;
    line-height: 1.35;
    word-break: break-word;
  }}
  .count {{
    font-family: var(--mono);
    font-size: 0.82rem;
    color: var(--accent);
    background: var(--accent-dim);
    padding: 0.2rem 0.55rem;
    border-radius: 999px;
    white-space: nowrap;
  }}

  .empty {{
    padding: 3rem 1.5rem;
    text-align: center;
    color: var(--muted);
  }}

  footer {{
    margin-top: 2rem;
    color: var(--muted);
    font-size: 0.8rem;
    font-family: var(--mono);
  }}

  @keyframes rise {{
    from {{ opacity: 0; transform: translateY(12px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="hero-top">
        <div class="eyebrow" data-i18n="eyebrow">Agent Input Atlas</div>
        <div class="lang-switch" role="group" aria-label="Language">
          <button type="button" class="lang-btn" data-lang="en">EN</button>
          <button type="button" class="lang-btn" data-lang="zh">中文</button>
        </div>
      </div>
      <h1 data-i18n="title">Short Input Word Cloud · Top N</h1>
      <p class="sub" data-i18n="subtitle"></p>
      <div class="meta">
        <span data-i18n-dynamic="generated"></span>
        <span data-i18n-dynamic="topn"></span>
        <span data-i18n-dynamic="limits"></span>
      </div>
    </header>

    <nav class="tabs" id="tabs" data-i18n-aria="tabsAria"></nav>

    <section class="stats" id="stats"></section>

    <section class="grid">
      <div class="panel">
        <div class="panel-hd">
          <h2 data-i18n="cloudTitle">Word Cloud</h2>
          <small data-i18n="cloudHint">Size ∝ frequency</small>
        </div>
        <canvas id="cloud" width="800" height="460"></canvas>
      </div>
      <div class="panel">
        <div class="panel-hd">
          <h2 data-i18n="rankTitle">Leaderboard</h2>
          <small id="list-label"></small>
        </div>
        <div class="list" id="list"></div>
      </div>
    </section>

    <footer data-i18n="footer"></footer>
  </div>

<script>
const DATA = {data_json};
const AGENTS = {agents_json};
const META = {meta_json};

const I18N = {{
  en: {{
    eyebrow: "Agent Input Atlas",
    title: "Short Input Word Cloud · Top N",
    subtitle: "From Cursor / Claude / Codex / Gemini / Kimi / Pi session logs. Only user inputs within ≤{{maxChars}} CJK characters or ≤{{maxWords}} words, grouped by agent.",
    generated: "Generated {{time}}",
    topn: "Top {{n}}",
    limits: "≤{{maxChars}} chars / ≤{{maxWords}} words",
    tabsAria: "Agent filter",
    all: "All",
    cloudTitle: "Word Cloud",
    cloudHint: "Size ∝ frequency",
    rankTitle: "Leaderboard",
    raw: "Raw user messages",
    short: "Short inputs matched",
    unique: "Unique phrases",
    empty: "No short inputs for this agent",
    emptyCloud: "No word-cloud data",
    footer: "Generated locally · nothing uploaded · xcode maps to codex",
  }},
  zh: {{
    eyebrow: "Agent 输入图谱",
    title: "短输入词云 · Top N",
    subtitle: "从 Cursor / Claude / Codex / Gemini / Kimi / Pi 的 session 日志中，只统计 ≤{{maxChars}} 字（中文）或 ≤{{maxWords}} 词（英文）的用户输入，按 agent 分桶。",
    generated: "生成于 {{time}}",
    topn: "Top {{n}}",
    limits: "≤{{maxChars}} 字 / ≤{{maxWords}} 词",
    tabsAria: "Agent 切换",
    all: "全部",
    cloudTitle: "词云",
    cloudHint: "字号 ∝ 出现次数",
    rankTitle: "排行榜",
    raw: "原始用户消息",
    short: "短输入命中",
    unique: "去重短语",
    empty: "这个 agent 暂无符合条件的短输入",
    emptyCloud: "暂无词云数据",
    footer: "数据仅本地生成 · 未上传 · xcode 已映射为 codex",
  }},
}};

const AGENT_LABEL = {{
  cursor: "Cursor",
  claude: "Claude",
  codex: "Codex",
  gemini: "Gemini",
  kimi: "Kimi",
  pi: "Pi",
}};

const LANG_KEY = "agent-input-stat-lang";
const tabsEl = document.getElementById("tabs");
const statsEl = document.getElementById("stats");
const listEl = document.getElementById("list");
const cloudEl = document.getElementById("cloud");
const listLabelEl = document.getElementById("list-label");

let current = "all";
let lang = detectLang();
let cloudTimer = null;

function detectLang() {{
  try {{
    const saved = localStorage.getItem(LANG_KEY);
    if (saved && I18N[saved]) return saved;
  }} catch (_) {{}}
  const nav = (navigator.language || navigator.userLanguage || "en").toLowerCase();
  if (nav.startsWith("zh")) return "zh";
  return "en";
}}

function t(key, vars) {{
  const dict = I18N[lang] || I18N.en;
  let s = dict[key] ?? I18N.en[key] ?? key;
  if (vars) {{
    Object.keys(vars).forEach((k) => {{
      s = s.replaceAll("{{" + k + "}}", String(vars[k]));
    }});
  }}
  return s;
}}

function setLang(next) {{
  if (!I18N[next]) return;
  lang = next;
  try {{ localStorage.setItem(LANG_KEY, lang); }} catch (_) {{}}
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  applyStaticI18n();
  render();
}}

function applyStaticI18n() {{
  document.title = t("title");
  document.querySelectorAll("[data-i18n]").forEach((el) => {{
    el.textContent = t(el.getAttribute("data-i18n"));
  }});
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => {{
    el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
  }});

  const sub = document.querySelector('[data-i18n="subtitle"]');
  if (sub) {{
    sub.textContent = t("subtitle", {{
      maxChars: META.max_chars,
      maxWords: META.max_words,
    }});
  }}

  document.querySelector('[data-i18n-dynamic="generated"]').textContent =
    t("generated", {{ time: META.generated_at }});
  document.querySelector('[data-i18n-dynamic="topn"]').textContent =
    t("topn", {{ n: META.top_n }});
  document.querySelector('[data-i18n-dynamic="limits"]').textContent =
    t("limits", {{ maxChars: META.max_chars, maxWords: META.max_words }});

  document.querySelectorAll(".lang-btn").forEach((btn) => {{
    btn.classList.toggle("active", btn.getAttribute("data-lang") === lang);
  }});
}}

function fmt(n) {{
  return Number(n || 0).toLocaleString(lang === "zh" ? "zh-CN" : "en-US");
}}

function agentLabel(a) {{
  if (a === "all") return t("all");
  return AGENT_LABEL[a] || a;
}}

function renderTabs() {{
  tabsEl.innerHTML = "";
  AGENTS.forEach((a) => {{
    if (!DATA[a]) return;
    const btn = document.createElement("button");
    btn.className = "tab" + (a === current ? " active" : "");
    btn.type = "button";
    btn.textContent = agentLabel(a);
    btn.addEventListener("click", () => {{
      current = a;
      render();
    }});
    tabsEl.appendChild(btn);
  }});
}}

function renderStats(d) {{
  statsEl.innerHTML = `
    <div class="stat"><div class="label">${{t("raw")}}</div><div class="value"><em>${{fmt(d.total_raw)}}</em></div></div>
    <div class="stat"><div class="label">${{t("short")}}</div><div class="value"><em>${{fmt(d.total_short)}}</em></div></div>
    <div class="stat"><div class="label">${{t("unique")}}</div><div class="value"><em>${{fmt(d.unique)}}</em></div></div>
  `;
}}

function renderList(d) {{
  listLabelEl.textContent = t("topn", {{ n: META.top_n }});
  const top = d.top || [];
  if (!top.length) {{
    listEl.innerHTML = `<div class="empty">${{escapeHtml(t("empty"))}}</div>`;
    return;
  }}
  listEl.innerHTML = top.map((it, i) => {{
    const rankClass = i < 3 ? "rank top3" : "rank";
    return `
      <div class="row">
        <div class="${{rankClass}}">${{String(i + 1).padStart(2, "0")}}</div>
        <div class="phrase">${{escapeHtml(it.text)}}</div>
        <div class="count">×${{it.count}}</div>
      </div>`;
  }}).join("");
}}

function escapeHtml(s) {{
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}}

function stopCloud() {{
  try {{
    if (typeof WordCloud !== "undefined" && WordCloud.isSupported) {{
      WordCloud.stop();
    }}
  }} catch (_) {{}}
}}

function truncateLabel(text, maxLen) {{
  const s = String(text || "").trim();
  if (s.length <= maxLen) return s;
  return s.slice(0, Math.max(1, maxLen - 1)) + "…";
}}

function buildCloudList(raw) {{
  // Truncate labels and cap count to avoid wordcloud2 shrink recursion
  return (raw || [])
    .filter((x) => x && x[0] && Number(x[1]) > 0)
    .slice(0, 60)
    .map(([text, count]) => [truncateLabel(text, 14), Math.max(1, Number(count) || 1)]);
}}

function renderCloud(d) {{
  stopCloud();

  const ctx = cloudEl.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, cloudEl.width, cloudEl.height);

  const list = buildCloudList(d.cloud);

  if (!list.length || typeof WordCloud === "undefined") {{
    ctx.fillStyle = "#8b9aab";
    ctx.font = "16px DM Sans, Noto Sans SC, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(t("emptyCloud"), cloudEl.width / 2, cloudEl.height / 2);
    return;
  }}

  const max = Math.max(...list.map((x) => x[1]));
  const min = Math.min(...list.map((x) => x[1]));
  const minSize = 12;
  const maxSize = Math.min(48, Math.floor(cloudEl.height / 8));

  WordCloud(cloudEl, {{
    list,
    gridSize: Math.max(8, Math.round(cloudEl.width / 64)),
    weightFactor: (w) => {{
      if (max === min) return (minSize + maxSize) / 2;
      const ratio = (w - min) / (max - min);
      return minSize + ratio * (maxSize - minSize);
    }},
    fontFamily: "DM Sans, Noto Sans SC, sans-serif",
    color: () => {{
      const palette = ["#3ecf8e", "#7dd3a7", "#f0a46e", "#8ec8ff", "#e8eef4", "#c4b5fd"];
      return palette[Math.floor(Math.random() * palette.length)];
    }},
    rotateRatio: 0.12,
    rotationSteps: 2,
    backgroundColor: "rgba(0,0,0,0)",
    shuffle: true,
    drawOutOfBound: false,
    // shrinkToFit can recurse until stack overflow on long phrases
    shrinkToFit: false,
    abortThreshold: 1000,
    clearCanvas: true,
  }});
}}

function fitCanvas() {{
  const parent = cloudEl.parentElement;
  // Use CSS pixels as canvas buffer size; DPR scaling confuses wordcloud2 layout
  const w = Math.max(320, Math.floor(parent.clientWidth));
  const h = 460;
  if (cloudEl.width !== w || cloudEl.height !== h) {{
    cloudEl.width = w;
    cloudEl.height = h;
  }}
  cloudEl.style.width = w + "px";
  cloudEl.style.height = h + "px";
}}

function render() {{
  renderTabs();
  const d = DATA[current] || {{ total_raw: 0, total_short: 0, unique: 0, top: [], cloud: [] }};
  renderStats(d);
  renderList(d);
  fitCanvas();
  renderCloud(d);
}}

document.querySelectorAll(".lang-btn").forEach((btn) => {{
  btn.addEventListener("click", () => setLang(btn.getAttribute("data-lang")));
}});

window.addEventListener("resize", () => {{
  if (cloudTimer) clearTimeout(cloudTimer);
  cloudTimer = setTimeout(() => {{
    fitCanvas();
    renderCloud(DATA[current] || {{ cloud: [] }});
  }}, 200);
}});

applyStaticI18n();
render();
</script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    if args.top < 1:
        logger.error("--top must be >= 1")
        return EXIT_INVALID_ARGS

    agents = []
    seen = set()
    for raw in args.agents.split(","):
        name = normalize_agent_name(raw)
        if not name or name in seen:
            continue
        if name not in PARSERS:
            logger.error("unsupported agent: %s (available: %s)", raw, ", ".join(PARSERS))
            return EXIT_INVALID_ARGS
        seen.add(name)
        agents.append(name)

    if not agents:
        logger.error("no valid agents specified")
        return EXIT_INVALID_ARGS

    try:
        stats = collect_stats(
            agents=agents,
            max_chars=args.max_chars,
            max_words=args.max_words,
            min_count=args.min_count,
        )
        html_doc = build_html(
            stats=stats,
            top_n=args.top,
            max_chars=args.max_chars,
            max_words=args.max_words,
        )
        out: Path = args.output
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_doc, encoding="utf-8")
        logger.info("wrote %s", out)
        print(str(out))
        return EXIT_SUCCESS
    except Exception as exc:
        logger.exception("failed: %s", exc)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
