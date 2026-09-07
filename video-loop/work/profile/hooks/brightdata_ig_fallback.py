#!/usr/bin/env python3
"""Fetch Instagram / X / Threads via Bright Data — MCP HTTP first; Datasets REST IG-only fallback."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

JOBS = Path("/workspace/video-timeline-pipeline/jobs")
DATASET_ID = "gd_lyclm20il4r5helnj"
API_ROOT = "https://api.brightdata.com/datasets/v3"
MCP_HOST = "https://mcp.brightdata.com/mcp"
SECRETS = (
    Path("/home/box/agent-data/box-secrets.json"),
    Path("/home/box/sand-data/box-secrets.json"),
)
SNAPSHOT_RE = re.compile(r"^s[a-z]*_[A-Za-z0-9_-]+$")


def redact(tok: str, text: str) -> str:
    if not tok:
        return text
    return text.replace(tok, tok[:4] + "…")


def token() -> str:
    for key in ("BRIGHTDATA_MCP_TOKEN", "BRIGHTDATA_API_TOKEN"):
        t = (os.environ.get(key) or "").strip()
        if t:
            return t
    for p in SECRETS:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            sec = data.get("secrets") if isinstance(data, dict) else {}
            if isinstance(sec, dict):
                for key in ("BRIGHTDATA_MCP_TOKEN", "BRIGHTDATA_API_TOKEN"):
                    v = (sec.get(key) or "").strip()
                    if v:
                        return v
        except Exception:
            pass
    envp = Path("/workspace/video-timeline-pipeline/.env")
    if envp.exists():
        for line in envp.read_text().splitlines():
            if line.startswith("BRIGHTDATA_API_TOKEN=") or line.startswith("BRIGHTDATA_MCP_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def detect_platform(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("instagram.com", "www.instagram.com") or host.endswith(".instagram.com"):
        return "instagram"
    if host in ("x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com", "t.co"):
        return "x"
    if host in ("threads.net", "www.threads.net") or host.endswith(".threads.net"):
        return "threads"
    # path-based fallbacks
    low = (url or "").lower()
    if "instagram.com" in low:
        return "instagram"
    if "twitter.com" in low or "x.com/" in low or low.startswith("https://x.com"):
        return "x"
    if "threads.net" in low:
        return "threads"
    return ""


def content_id(url: str, platform: str) -> str:
    u = url or ""
    if platform == "instagram":
        m = re.search(r"/(?:reel|reels|p)/([^/?#]+)", u, re.I)
        return m.group(1) if m else ""
    if platform == "x":
        m = re.search(r"/status/(\d+)", u, re.I)
        return m.group(1) if m else ""
    if platform == "threads":
        m = re.search(r"/post/([\w-]+)", u, re.I)
        return m.group(1) if m else ""
    return ""


def normalize_url(url: str, platform: str, cid: str) -> str:
    if platform == "instagram" and cid:
        return f"https://www.instagram.com/reel/{cid}/"
    return (url or "").split("?")[0]


def provider_label(platform: str) -> str:
    return {
        "instagram": "brightdata-ig",
        "x": "brightdata-x",
        "threads": "brightdata-threads",
    }.get(platform, "brightdata")


def http_json(method: str, url: str, tok: str, body=None, timeout: int = 90):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")[:800]
        raise RuntimeError(f"http_{e.code}:{redact(tok, err)}") from e


def parse_sse_jsonrpc(raw: str):
    """Extract JSON-RPC payloads from streamable-HTTP SSE body."""
    payloads = []
    for line in (raw or "").splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                payloads.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue
    if payloads:
        return payloads
    try:
        return [json.loads(raw)]
    except Exception:
        return []


def mcp_post(url: str, body: dict, headers: dict, timeout: int = 120):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="ignore")
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        return r.status, hdrs, raw


def extract_json_blob(text: str):
    """Pull first JSON array/object from MCP tool text (may include security notice)."""
    if not text:
        return None
    text = text.strip()
    for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        for matcher in re.finditer(pattern, text):
            cand = matcher.group(0)
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except Exception:
        return None


def mcp_tool_call(tok: str, tool_name: str, arguments: dict, client_name: str = "irisx-brightdata"):
    """MCP initialize → session → tools/call. Returns (result_dict_or_list, raw_text_parts)."""
    groups = "social" if tool_name.startswith("web_data_") else "advanced_scraping,social"
    mcp_url = f"{MCP_HOST}?{urllib.parse.urlencode({'token': tok, 'groups': groups})}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "1.1"},
        },
    }
    status, hdrs, raw = mcp_post(mcp_url, init, headers, timeout=45)
    session_id = hdrs.get("mcp-session-id")
    if not session_id:
        raise RuntimeError(f"mcp_no_session:status={status}")
    headers2 = dict(headers)
    headers2["mcp-session-id"] = session_id
    try:
        mcp_post(
            mcp_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers2,
            timeout=30,
        )
    except Exception:
        pass
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    status, hdrs, raw = mcp_post(mcp_url, call, headers2, timeout=180)
    payloads = parse_sse_jsonrpc(raw)
    if not payloads:
        raise RuntimeError(f"mcp_empty_response:status={status}")
    chosen = None
    for p in payloads:
        if isinstance(p, dict) and ("result" in p or "error" in p):
            chosen = p
    if chosen is None:
        chosen = payloads[-1]
    if chosen.get("error"):
        err = chosen["error"]
        raise RuntimeError(f"mcp_rpc_error:{redact(tok, json.dumps(err)[:400])}")
    result = chosen.get("result") or {}
    return result


def mcp_content_texts(result) -> list[str]:
    texts: list[str] = []
    if not isinstance(result, dict):
        return texts
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                texts.append(str(item["text"]))
            elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
                res = item["resource"]
                if res.get("text"):
                    texts.append(str(res["text"]))
    return texts


def mcp_records_from_result(result) -> list:
    records: list = []
    texts = mcp_content_texts(result)
    for text in texts:
        blob = extract_json_blob(text)
        if isinstance(blob, list):
            records.extend([x for x in blob if isinstance(x, dict)])
        elif isinstance(blob, dict):
            records.append(blob)
    if isinstance(result, list):
        records.extend([x for x in result if isinstance(x, dict)])
    if isinstance(result, dict) and (
        result.get("description")
        or result.get("shortcode")
        or result.get("video_url")
        or result.get("text")
        or result.get("full_text")
        or result.get("tweet_id")
        or result.get("id")
    ):
        # avoid double-adding empty shells that are just MCP wrappers
        if "content" not in result or not isinstance(result.get("content"), list):
            records.append(result)
    return records


def wait_snapshot(tok: str, snapshot_id: str, timeout_seconds: int = 180) -> list:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        progress = http_json(
            "GET",
            f"{API_ROOT}/progress/{urllib.parse.quote(snapshot_id, safe='')}",
            tok,
            timeout=30,
        )
        status = str((progress or {}).get("status") or "").lower()
        if status == "failed":
            raise RuntimeError(f"snapshot_failed:{snapshot_id}")
        if status == "ready":
            result = http_json(
                "GET",
                f"{API_ROOT}/snapshot/{urllib.parse.quote(snapshot_id, safe='')}?format=json",
                tok,
                timeout=60,
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and isinstance(result.get("data"), list):
                return result["data"]
            return []
        time.sleep(min(5.0, max(0.5, deadline - time.monotonic())))
    raise RuntimeError(f"snapshot_timeout:{snapshot_id}")


def scrape_reel_datasets(tok: str, reel_url: str) -> list:
    """Datasets REST scrape; poll async snapshot ids including sd_. IG only."""
    q = urllib.parse.urlencode(
        {"dataset_id": DATASET_ID, "include_errors": "true", "format": "json"}
    )
    url = f"{API_ROOT}/scrape?{q}"
    payload = {"input": [{"url": reel_url}]}
    last_err = ""
    try:
        result = http_json("POST", url, tok, payload, timeout=70)
    except RuntimeError as e:
        msg = str(e)
        if "http_401" in msg or "http_403" in msg:
            raise
        result = None
        last_err = msg

    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("data"), list):
            return result["data"]
        snap = str(result.get("snapshot_id") or "")
        if SNAPSHOT_RE.fullmatch(snap):
            return wait_snapshot(tok, snap)
        if result.get("url") or result.get("shortcode") or result.get("description"):
            return [result]

    q2 = urllib.parse.urlencode(
        {"dataset_id": DATASET_ID, "include_errors": "true", "format": "json"}
    )
    trigger = f"{API_ROOT}/trigger?{q2}"
    started = http_json("POST", trigger, tok, payload, timeout=60)
    snap = str((started or {}).get("snapshot_id") or "")
    if not SNAPSHOT_RE.fullmatch(snap):
        raise RuntimeError(last_err or f"no_snapshot:{started}")
    return wait_snapshot(tok, snap)


def pick_hit(items: list, cid: str, platform: str):
    cleaned = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("error") or it.get("error_code"):
            continue
        cleaned.append(it)
    for it in cleaned:
        sc = str(
            it.get("shortcode")
            or it.get("shortCode")
            or it.get("tweet_id")
            or it.get("id")
            or it.get("post_id")
            or ""
        )
        u = str(it.get("url") or it.get("webpage_url") or "")
        if cid and (cid == sc or cid in u or cid in sc):
            return it
    for it in cleaned:
        if (
            it.get("description")
            or it.get("caption")
            or it.get("video_url")
            or it.get("text")
            or it.get("full_text")
            or it.get("body")
        ):
            return it
    return cleaned[0] if cleaned else None


def parse_threads_markdown(md: str, url: str, cid: str) -> dict:
    """Best-effort title/body/author from scrape_as_markdown output."""
    text = (md or "").strip()
    # Drop Bright Data security wrapper + untrusted fences entirely
    text = re.sub(
        r"SECURITY NOTICE:[\s\S]*?=====UNTRUSTED_[A-Fa-f0-9]+_BEGIN=====\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s*=====UNTRUSTED_[A-Fa-f0-9]+_END=====\s*", "", text, flags=re.I)
    text = re.sub(r"=====UNTRUSTED_[A-Za-z0-9_]+=====", "", text)
    text = re.sub(r"^UNTRUSTED_[A-Za-z0-9_]+=====\s*", "", text, flags=re.M)

    m_user = re.search(r"threads\.net/@([^/?#]+)", url or "", re.I)
    author = m_user.group(1) if m_user else ""

    nav = {
        "home", "search", "create", "notifications", "profile", "pin", "more",
        "back", "thread", "like", "comment", "repost", "share", "verified",
        "related threads", "instagram", "log in",
        "log in with username instead", "report a problem",
        "say more with threads",
    }
    cleaned = text.replace("\\[", "[").replace("\\]", "]")
    views = None
    likes = None
    lines_out: list[str] = []
    for raw in cleaned.splitlines():
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        if "untrusted_" in low or low.startswith("security notice"):
            continue
        if low in nav or low.startswith("log in") or "sign up for threads" in low:
            continue
        if "join the conversation" in low or "join threads to share" in low:
            continue
        # markdown debris from broken / escaped links
        if re.fullmatch(r"\]?\\?\(/[^)]*\)?", s) or re.fullmatch(r"\[?", s):
            continue
        if re.fullmatch(r"\]\\?\([^)]*\)", s):
            continue
        if s.startswith("]/(") or s.startswith("]\\(") or s.startswith("]/("):
            continue
        if s in ("](/)", "](/search)", "[", "]", r"]\(/)", r"]\(/search)"):
            continue
        if "profile picture" in low:
            continue
        if re.fullmatch(r"\d+[hm]", s):
            continue
        # engagement counts alone after we already have caption
        if re.fullmatch(r"\d+[KkMm]?", s):
            continue
        vm = re.search(r"([\d.]+[KkMm]?)\s+views", s, re.I)
        if vm:
            views = vm.group(1)
            continue
        am = re.search(r"\[@?([\w.]+)\]\(/@", s)
        if am:
            if not author:
                author = am.group(1)
            if re.fullmatch(r"\[@?[\w.]+\]\(/@[^)]+\)", s):
                continue
        if re.fullmatch(r"\[.*?\]\([^)]*\)", s) and len(s) < 120:
            continue
        if re.fullmatch(r"©\s*\d{4}", s):
            continue
        if any(x in s for x in ("Terms", "Privacy Policy", "Cookies Policy", "Consumer Health")):
            continue
        lines_out.append(s)

    lm = re.search(r"\nLike\n\s*([\d.]+[KkMm]?)\b", cleaned)
    if lm:
        likes = lm.group(1)

    body_candidates: list[str] = []
    for s in lines_out:
        if author and s.lower() in {author.lower(), f"@{author.lower()}"}:
            continue
        if s.startswith("http") and "threads.net" in s:
            continue
        body_candidates.append(s)

    title = ""
    body_parts: list[str] = []
    seen = set()
    for s in body_candidates:
        key = s.lower()
        if key in seen:
            continue
        if "untrusted" in key:
            continue
        seen.add(key)
        if not title and len(s) >= 2 and not s.startswith("["):
            title = s[:120]
        if len(body_parts) < 8:
            body_parts.append(s)
        if title and len(body_parts) >= 2:
            if "log in to see more" in key:
                break
            # stop once replies flood in
            if re.search(r"JUSTICE FOR|missing |\d{1,2}/\d{1,2}/\d{2,4}", s, re.I) and len(body_parts) >= 3:
                break

    body = "\n".join(body_parts).strip()
    if not body:
        # last resort: first non-empty non-nav line from cleaned
        for s in cleaned.splitlines():
            t = s.strip()
            if t and "untrusted" not in t.lower() and t.lower() not in nav and len(t) > 2:
                if not re.match(r"^[\[\]\\(/)]+$", t):
                    body = t
                    title = title or t[:120]
                    break
    if not title:
        title = (body.splitlines()[0] if body else (author or cid or "threads-post"))[:120]

    thin = True  # Threads scrape is always best-effort / often media-heavy
    if len(body) >= 120 and "log in to see more" not in cleaned.lower():
        thin = False
    if "log in to see more" in cleaned.lower():
        thin = True

    hit = {
        "description": body[:4000],
        "caption": body[:4000],
        "text": body[:4000],
        "title": title,
        "username": author,
        "user_posted": author,
        "url": url,
        "id": cid,
        "post_id": cid,
        "_threads_thin": thin,
        "_threads_note": "media-heavy / login-walled replies; markdown scrape best-effort",
        "_source_markdown": (md or "")[:12000],
    }
    if views is not None:
        hit["views"] = views
    if likes is not None:
        hit["likes"] = likes
    return hit



def write_job(url: str, cid: str, hit: dict, via: str, platform: str) -> Path:
    provider = provider_label(platform)
    job = JOBS / f"brightdata-{cid or platform or 'item'}"
    job.mkdir(parents=True, exist_ok=True)
    (job / "brightdata.item.json").write_text(
        json.dumps(hit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (job / "request_url.txt").write_text(url + "\n", encoding="utf-8")
    (job / "source.json").write_text(
        json.dumps(
            {"url": url, "provider": provider, "via": via, "platform": platform},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cap = (
        hit.get("description")
        or hit.get("caption")
        or hit.get("text")
        or hit.get("full_text")
        or hit.get("body")
        or ""
    ).strip()
    title = (hit.get("title") or (cap.splitlines()[0] if cap else cid) or cid or platform)[:120]
    author = (
        hit.get("user_posted")
        or hit.get("username")
        or hit.get("ownerUsername")
        or hit.get("profile_username")
        or hit.get("user_name")
        or hit.get("name")
        or ""
    )
    if isinstance(author, dict):
        author = author.get("username") or author.get("name") or author.get("screen_name") or ""
    notes = [f"# {title}", "", "## 摘要"]
    if cap:
        notes.append(cap[:2500])
    elif hit.get("_threads_thin"):
        notes.append("（Threads 抓到的 markdown 偏薄／可能需登入；以下為可用片段）")
    notes += ["", "## 主題重點"]
    for line in (cap.splitlines() if cap else [])[:8]:
        if line.strip():
            notes.append(f"- {line.strip()[:140]}")
    if hit.get("likes") is not None:
        notes.append(f"- 按讚：{hit.get('likes')}")
    if hit.get("views") is not None or hit.get("video_play_count") is not None:
        notes.append(f"- 觀看／播放：{hit.get('views') or hit.get('video_play_count')}")
    if hit.get("retweet_count") is not None or hit.get("reposts") is not None:
        notes.append(f"- 轉發：{hit.get('retweet_count') or hit.get('reposts')}")
    notes += ["", "## 操作步驟", "1. 先看完整內容再對照摘要"]
    (job / "notes.md").write_text("\n".join(notes), encoding="utf-8")
    info = {
        "title": title,
        "uploader": author,
        "duration": hit.get("length") or hit.get("video_duration") or hit.get("duration"),
        "view_count": hit.get("views") or hit.get("video_play_count") or hit.get("play_count"),
        "webpage_url": hit.get("url") or url,
        "description": cap[:2000],
        "id": hit.get("shortcode") or hit.get("post_id") or hit.get("tweet_id") or hit.get("id") or cid,
        "provider": provider,
        "via": via,
        "platform": platform,
        "video_url": hit.get("video_url") or hit.get("videoUrl") or "",
    }
    (job / "source.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return job


def fetch_instagram(tok: str, url: str, cid: str):
    reel_url = normalize_url(url, "instagram", cid)
    via = None
    items: list = []
    mcp_err = ""
    try:
        result = mcp_tool_call(
            tok,
            "web_data_instagram_reels",
            {"url": reel_url},
            client_name="irisx-brightdata-ig",
        )
        items = mcp_records_from_result(result)
        via = "mcp-http"
    except Exception as e:
        mcp_err = redact(tok, str(e))[:400]
        try:
            items = scrape_reel_datasets(tok, reel_url)
            via = "datasets-rest"
        except Exception as e2:
            rest_err = redact(tok, str(e2))[:400]
            return None, via, f"brightdata_err:mcp={mcp_err};rest={rest_err}", items
    hit = pick_hit(items, cid, "instagram")
    if not hit:
        return None, via, "brightdata_no_item", items
    if not (hit.get("description") or hit.get("caption") or hit.get("video_url")):
        return None, via, "brightdata_empty_fields", items
    return hit, via, "", items


def fetch_x(tok: str, url: str, cid: str):
    via = "mcp-http"
    try:
        result = mcp_tool_call(
            tok,
            "web_data_x_posts",
            {"url": url},
            client_name="irisx-brightdata-x",
        )
        items = mcp_records_from_result(result)
    except Exception as e:
        return None, via, f"brightdata_err:mcp={redact(tok, str(e))[:400]}", []
    hit = pick_hit(items, cid, "x")
    if not hit:
        return None, via, "brightdata_no_item", items
    if not (
        hit.get("description")
        or hit.get("caption")
        or hit.get("text")
        or hit.get("full_text")
        or hit.get("body")
    ):
        return None, via, "brightdata_empty_fields", items
    return hit, via, "", items


def fetch_threads(tok: str, url: str, cid: str):
    via = "mcp-http"
    md = ""
    try:
        result = mcp_tool_call(
            tok,
            "scrape_as_markdown",
            {"url": url},
            client_name="irisx-brightdata-threads",
        )
        texts = mcp_content_texts(result)
        md = "\n\n".join(texts).strip()
        if not md and isinstance(result, dict):
            # sometimes result is plain text wrapper
            md = str(result.get("markdown") or result.get("text") or "")
        if not md:
            # try HTML scrape as secondary within same attempt path
            result2 = mcp_tool_call(
                tok,
                "scrape_as_html",
                {"url": url},
                client_name="irisx-brightdata-threads-html",
            )
            texts2 = mcp_content_texts(result2)
            raw_html = "\n".join(texts2)
            # crude strip tags
            md = re.sub(r"<script[\s\S]*?</script>", " ", raw_html, flags=re.I)
            md = re.sub(r"<style[\s\S]*?</style>", " ", md, flags=re.I)
            md = re.sub(r"<[^>]+>", " ", md)
            md = re.sub(r"\s+", " ", md).strip()
            via = "mcp-http-html"
    except Exception as e:
        return None, via, f"brightdata_err:mcp={redact(tok, str(e))[:400]}", []

    if not md or len(md.strip()) < 20:
        return None, via, "brightdata_threads_thin_or_empty", []

    hit = parse_threads_markdown(md, url, cid)
    if hit.get("_threads_thin") and len((hit.get("description") or "")) < 40:
        return hit, via, "brightdata_threads_login_wall_or_thin", [hit]
    return hit, via, "", [hit]


def main(url: str) -> int:
    tok = token()
    if not tok:
        print(json.dumps({"ok": False, "note": "no_brightdata_token"}))
        return 0
    platform = detect_platform(url)
    if not platform:
        print(json.dumps({"ok": False, "note": "unsupported_platform"}))
        return 0
    cid = content_id(url, platform)
    hit = None
    via = None
    note = ""
    items: list = []

    if platform == "instagram":
        hit, via, note, items = fetch_instagram(tok, url, cid)
    elif platform == "x":
        hit, via, note, items = fetch_x(tok, url, cid)
    elif platform == "threads":
        hit, via, note, items = fetch_threads(tok, url, cid)
    else:
        print(json.dumps({"ok": False, "note": "unsupported_platform", "platform": platform}))
        return 0

    if note and (hit is None or (platform == "threads" and "thin" in note)):
        # For threads thin-with-some-content, still try to write if we have usable text
        if platform == "threads" and hit and (hit.get("description") or "").strip():
            pass  # allow write below even with thin warning if we have text
        else:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "note": note or "brightdata_failed",
                        "id": cid,
                        "platform": platform,
                        "via": via,
                        "raw_count": len(items or []),
                    }
                )
            )
            return 0

    if not hit:
        print(
            json.dumps(
                {
                    "ok": False,
                    "note": note or "brightdata_no_item",
                    "id": cid,
                    "platform": platform,
                    "via": via,
                    "raw_count": len(items or []),
                }
            )
        )
        return 0

    job = write_job(url, cid, hit, via or "unknown", platform)
    out = {
        "ok": True,
        "job": str(job),
        "id": cid,
        "via": via,
        "platform": platform,
        "provider": provider_label(platform),
    }
    if note:
        out["note"] = note
    # keep shortcode for IG backward compat
    if platform == "instagram":
        out["shortcode"] = cid
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
