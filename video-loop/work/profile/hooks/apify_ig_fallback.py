#!/usr/bin/env python3
"""Fetch one Instagram reel via Apify when yt-dlp fails."""
from __future__ import annotations
import json, os, re, sys, time, urllib.request
from pathlib import Path

JOBS = Path("/workspace/video-timeline-pipeline/jobs")


def token() -> str:
    t = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or ""
    if t:
        return t
    p = Path("/home/box/agent-data/box-secrets.json")
    if p.exists():
        data = json.loads(p.read_text())
        sec = data.get("secrets") if isinstance(data, dict) else {}
        if isinstance(sec, dict):
            return sec.get("APIFY_TOKEN") or sec.get("APIFY_API_TOKEN") or ""
    return ""


def api(tok: str, method: str, path: str, body=None, timeout=180):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.apify.com/v2{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def shortcode(url: str) -> str:
    m = re.search(r"/(?:reel|p)/([^/?#]+)", url)
    return m.group(1) if m else ""


def wait_run(tok: str, rid: str, loops: int = 72):
    run = None
    for _ in range(loops):
        time.sleep(5)
        run = api(tok, "GET", f"/actor-runs/{rid}")["data"]
        if run.get("status") in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
            return run
    return run


def dataset_items(tok: str, run: dict) -> list:
    ds = run.get("defaultDatasetId")
    if not ds:
        return []
    items = api(tok, "GET", f"/datasets/{ds}/items?clean=true&format=json")
    if not isinstance(items, list):
        items = items.get("data") or []
    return items


def pick_hit(items: list, code: str):
    for it in items:
        if it.get("error") or it.get("success") is False:
            continue
        sc = str(it.get("shortCode") or it.get("postCode") or it.get("code") or "")
        u = str(it.get("url") or "")
        if code and (code in sc or code in u or sc == code):
            return it
    for it in items:
        if not it.get("error") and it.get("success") is not False and (it.get("caption") or it.get("transcript")):
            return it
    return None


def write_job(url: str, code: str, hit: dict) -> Path:
    job = JOBS / f"apify-{code or 'ig'}"
    job.mkdir(parents=True, exist_ok=True)
    (job / "apify.item.json").write_text(json.dumps(hit, ensure_ascii=False, indent=2))
    (job / "request_url.txt").write_text(url + "\n")
    (job / "source.json").write_text(json.dumps({"url": url}, ensure_ascii=False, indent=2))
    cap = (hit.get("caption") or hit.get("text") or "").strip()
    tr = hit.get("transcript") or hit.get("transcription") or ""
    if isinstance(tr, dict):
        tr = tr.get("text") or ""
    title = (cap.splitlines()[0] if cap else code)[:120]
    notes = [f"# {title}", "", "## 摘要"]
    if cap:
        notes.append(cap[:2500])
    if tr:
        notes += ["", f"逐字稿／口播：{str(tr)[:4000]}"]
    notes += ["", "## 主題重點"]
    for line in (cap.splitlines() if cap else [])[:8]:
        if line.strip():
            notes.append(f"- {line.strip()[:140]}")
    if tr:
        notes.append(f"- 口播重點：{str(tr)[:160]}")
    notes += ["", "## 操作步驟"]
    if tr:
        parts = [p.strip() for p in re.split(r"[。！？\.\!\?]", str(tr)) if p.strip()][:6]
        for i, p in enumerate(parts or [str(tr)[:120]], 1):
            notes.append(f"{i}. {p[:160]}")
    else:
        notes.append("1. 先看完整短片再對照 caption")
    (job / "notes.md").write_text("\n".join(notes), encoding="utf-8")
    info = {
        "title": title,
        "uploader": hit.get("ownerUsername") or hit.get("ownerFullName") or hit.get("username") or "",
        "duration": hit.get("videoDuration") or hit.get("duration"),
        "view_count": hit.get("videoViewCount") or hit.get("videoPlayCount") or hit.get("playCount"),
        "webpage_url": hit.get("url") or url,
        "description": cap[:2000],
        "id": hit.get("shortCode") or hit.get("postCode") or code,
    }
    (job / "source.info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    return job


def main(url: str) -> int:
    tok = token()
    if not tok:
        print(json.dumps({"ok": False, "note": "no_apify_token"}))
        return 0
    code = shortcode(url)

    # A) cheap metadata first
    try:
        run = api(tok, "POST", "/acts/data-slayer~instagram-post-details/runs?waitForFinish=0", {
            "postUrls": [code, url] if code else [url],
        })["data"]
        run = wait_run(tok, run["id"], loops=36)
        if run and run.get("status") == "SUCCEEDED":
            hit = pick_hit(dataset_items(tok, run), code)
            if hit and (hit.get("caption") or hit.get("transcript")):
                # still try official for transcript if missing
                if not hit.get("transcript"):
                    pass  # fall through to B but keep as backup
                else:
                    job = write_job(url, code, hit)
                    print(json.dumps({"ok": True, "job": str(job), "shortcode": code, "via": "data-slayer"}))
                    return 0
            backup = hit
        else:
            backup = None
    except Exception:
        backup = None

    # B) official reel scraper with transcript (single URL only — keep cost down)
    try:
        run = api(tok, "POST", "/acts/apify~instagram-reel-scraper/runs?waitForFinish=0", {
            "username": [url],
            "resultsLimit": 1,
            "includeTranscript": True,
            "includeDownloadedVideo": False,
            "includeSharesCount": False,
        })["data"]
        run = wait_run(tok, run["id"], loops=72)
        if run and run.get("status") == "SUCCEEDED":
            hit = pick_hit(dataset_items(tok, run), code)
            if hit:
                job = write_job(url, code, hit)
                print(json.dumps({"ok": True, "job": str(job), "shortcode": code, "via": "instagram-reel-scraper", "run_id": run.get("id")}))
                return 0
    except Exception as e:
        print(json.dumps({"ok": False, "note": f"official_err:{e}"}))
        return 0

    # C) use backup metadata-only if any
    if backup and (backup.get("caption") or backup.get("text")):
        job = write_job(url, code, backup)
        print(json.dumps({"ok": True, "job": str(job), "shortcode": code, "via": "data-slayer-backup"}))
        return 0

    print(json.dumps({"ok": False, "note": "apify_no_item", "shortcode": code}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
