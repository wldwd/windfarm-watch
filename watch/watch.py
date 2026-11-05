#!/usr/bin/env python3
"""Wind farm approvals checker.

Reads each page in watch/sources.json, finds lines that mention the watched
projects, and records any line it hasn't seen before in data/changes.json.
The first time a source is read, its current lines are recorded silently
(a "baseline"), so only genuinely new lines are reported after that.

Writes:
  data/changes.json  newest findings first (shown on the website)
  data/seen.json     fingerprints of lines already seen
  data/status.json   last-checked time and which sources could be read
  alert.md / alert_title.txt  only when there is something new (used to open a GitHub issue)

Usage:
  python watch/watch.py                 # live run
  python watch/watch.py --fixtures DIR  # read DIR/<source-id>.(html|pdf|xml) instead of the web
"""
import argparse
import datetime as dt
import hashlib
import html
import io
import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PERTH = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; WindFarmWatch/1.0; family approvals tracker)"
MAX_NEW_PER_SOURCE = 12      # more than this in one run is summarised as "page changed a lot"
KEEP_CHANGES = 300


def now_perth():
    return dt.datetime.now(PERTH).replace(microsecond=0)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------- fetching ----------

def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as e:  # network errors, HTTP errors
            last = e
            time.sleep(3 * (i + 1))
    raise last


class _Text(HTMLParser):
    BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
             "br", "section", "article", "header", "footer", "dt", "dd", "option", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0
        self.links = []          # (text, href)
        self._href, self._atext = None, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._atext = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1
        if tag == "a" and self._href:
            t = " ".join("".join(self._atext).split())
            if t:
                self.links.append((t, self._href))
            self._href = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self._href is not None:
                self._atext.append(data)


def html_lines(raw):
    p = _Text()
    p.feed(raw.decode("utf-8", errors="replace"))
    text = "".join(p.parts)
    lines = [" ".join(l.split()) for l in text.split("\n")]
    return [l for l in lines if len(l) > 3], p.links


def pdf_lines(raw):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf not installed")
    reader = PdfReader(io.BytesIO(raw))
    out = []
    for page in reader.pages:
        for l in (page.extract_text() or "").split("\n"):
            l = " ".join(l.split())
            if len(l) > 3:
                out.append(l)
    return out


def rss_items(raw):
    text = raw.decode("utf-8", errors="replace")
    items = []
    for block in re.findall(r"<item>(.*?)</item>", text, flags=re.S | re.I):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, flags=re.S | re.I)
            if not m:
                return ""
            v = m.group(1).strip()
            v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
            return html.unescape(re.sub(r"<[^>]+>", "", v)).strip()
        items.append({"title": tag("title"), "link": tag("link"), "date": tag("pubDate")})
    return items


# ---------- matching ----------

def compile_list(pats):
    return [re.compile(p, re.I) for p in pats]


def which_project(text, projects):
    for name, pats in projects.items():
        if any(p.search(text) for p in pats):
            return name
    return ""


def fingerprint(source_id, text):
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha1(f"{source_id}|{norm}".encode()).hexdigest()[:16]


WINDOW_RE = re.compile(r"comment|submission|advertis|closing|closes|have your say|open for", re.I)
LODGED_RE = re.compile(r"lodg|development application|referr|approv|refus", re.I)


def classify(kind, text):
    """Official sources: a line about comments becomes a 'window'. News keeps its kind."""
    if kind != "news" and WINDOW_RE.search(text):
        return "window"
    return kind


def is_flagged(text):
    """True when a line talks about comment windows or lodgement, so news is worth an alert too."""
    return bool(WINDOW_RE.search(text) or LODGED_RE.search(text))


def find_link(text, links, base):
    """Best-effort link for a line: an <a> whose text appears in the line."""
    for t, href in links:
        if len(t) > 6 and t.lower() in text.lower() and href and not href.startswith(("#", "javascript", "mailto")):
            return urllib.request.urljoin(base, href)
    return base


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", help="read sources from this folder instead of the web")
    args = ap.parse_args()

    cfg = load_json(ROOT / "watch" / "sources.json", {})
    projects = {k: compile_list(v) for k, v in cfg.get("projects", {}).items()}
    seen = load_json(DATA / "seen.json", {})
    changes = load_json(DATA / "changes.json", [])
    status = load_json(DATA / "status.json", {})

    stamp = now_perth()
    health, new_items = [], []

    for src in cfg.get("sources", []):
        sid, url, typ = src["id"], src["url"], src.get("type", "html")
        match, also = compile_list(src.get("match", [])), compile_list(src.get("also", []))
        h = {"id": sid, "name": src["name"], "url": url, "checked": stamp.isoformat(), "ok": True, "error": ""}
        try:
            if args.fixtures:
                ext = {"html": "html", "pdf": "pdf", "rss": "xml"}[typ]
                fp = Path(args.fixtures) / f"{sid}.{ext}"
                if not fp.exists():
                    raise FileNotFoundError(f"no fixture {fp.name}")
                raw = fp.read_bytes()
            else:
                raw = fetch(url)

            candidates = []  # (text, link, date)
            if typ == "pdf":
                for l in pdf_lines(raw):
                    candidates.append((l, url, ""))
            elif typ == "rss":
                for it in rss_items(raw):
                    candidates.append((it["title"], it["link"] or url, it["date"]))
            else:
                lines, links = html_lines(raw)
                for l in lines:
                    candidates.append((l, None, ""))
                h["_links"] = links

            hits = []
            for text, link, date in candidates:
                if not any(p.search(text) for p in match):
                    continue
                if also and not any(p.search(text) for p in also):
                    continue
                if link is None:
                    link = find_link(text, h.get("_links", []), url)
                hits.append((text[:400], link, date))
            h.pop("_links", None)

            src_seen = seen.setdefault(sid, {"baseline": stamp.isoformat(), "keys": []})
            first_time = not src_seen["keys"] and src_seen.get("baseline") == stamp.isoformat()
            known = set(src_seen["keys"])
            fresh = []
            for text, link, date in hits:
                k = fingerprint(sid, text)
                if k in known:
                    continue
                known.add(k)
                fresh.append((text, link, date))
            src_seen["keys"] = sorted(known)
            h["matches"] = len(hits)

            if first_time:
                h["note"] = f"baseline recorded ({len(fresh)} lines)"
            elif len(fresh) > MAX_NEW_PER_SOURCE:
                proj = which_project(" ".join(t for t, _, _ in fresh), projects)
                new_items.append({
                    "date": stamp.isoformat(), "source": src["name"], "source_id": sid, "url": url,
                    "project": proj, "kind": src.get("kind", ""),
                    "text": f"{len(fresh)} new mentions on this page. It may have been reorganised, or something significant was added. Worth a look.",
                })
            else:
                for text, link, date in fresh:
                    new_items.append({
                        "date": stamp.isoformat(), "source": src["name"], "source_id": sid, "url": link or url,
                        "project": which_project(text, projects), "kind": classify(src.get("kind", ""), text),
                        "flag": is_flagged(text), "text": text, "published": date,
                    })
        except Exception as e:
            h["ok"] = False
            h["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            h.pop("_links", None)
        health.append(h)

    changes = (new_items + changes)[:KEEP_CHANGES]
    status["last_checked"] = stamp.isoformat()
    status["sources"] = health
    save_json(DATA / "changes.json", changes)
    save_json(DATA / "seen.json", seen)
    save_json(DATA / "status.json", status)

    alert_md, alert_title = ROOT / "alert.md", ROOT / "alert_title.txt"
    for p in (alert_md, alert_title):
        if p.exists():
            p.unlink()
    # Alert on anything from official or developer sources, and on news that mentions
    # lodgement, approval or comment. Other news still shows on the website.
    important = [c for c in new_items if c["kind"] != "news" or c.get("flag")]
    if important:
        first = important[0]
        label = first["project"] or first["source"]
        title = f"New: {label} on {first['source']}"
        if len(important) > 1:
            title += f" (+{len(important) - 1} more)"
        body = ["New wind farm approval activity found on " + stamp.strftime("%a %d %b %Y, %I:%M %p") + " (Perth).", ""]
        for c in important:
            body.append(f"- **{c['source']}**{' · ' + c['project'] if c['project'] else ''}: {c['text']}  \n  {c['url']}")
        body += ["", "Check the website and the source before acting. Comment windows can be as short as 7 days."]
        alert_md.write_text("\n".join(body) + "\n", encoding="utf-8")
        alert_title.write_text(title[:180] + "\n", encoding="utf-8")

    ok = sum(1 for h in health if h["ok"])
    print(f"Checked {len(health)} sources ({ok} readable). New findings: {len(new_items)} ({len(important)} alert-worthy).")
    for h in health:
        print(f"  {'OK ' if h['ok'] else 'ERR'} {h['id']}: {h.get('note') or h.get('error') or str(h.get('matches', 0)) + ' matching lines'}")


if __name__ == "__main__":
    sys.exit(main())
