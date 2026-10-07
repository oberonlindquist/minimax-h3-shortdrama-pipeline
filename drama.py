#!/usr/bin/env python3
"""MiniMax H3 短剧生产管线 / short-drama production pipeline for MiniMax H3.

One episode lives in one folder:
    bible.json        cast: characters, places, props, sounds -> reference files
    script.md         the episode script (input to `split`)
    shots.json        the shot list; prompts mention cast as 【name】
    production.json   state written by this tool: approvals, request IDs, files, fingerprints

Commands:
    python3 drama.py split   EP [--model gpt-5.6-luna]   draft shots.json from script.md with a chat model
    python3 drama.py check   EP                          validate every shot, print the cost table, flag stale shots
    python3 drama.py approve EP S01 S02 ... | --all      only approved shots are ever rendered (paid)
    python3 drama.py render  EP [--draft]                upload references once, submit, poll, download to EP/renders
    python3 drama.py status  EP                          one line per shot
    python3 drama.py stitch  EP [--reencode]             ffmpeg concat of finished shots, in order

Environment: H3_API_KEY (render/split), H3_BASE_URL (default https://argolink.io/v1).
Standard library only; ffmpeg only for `stitch`.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("H3_BASE_URL", "https://argolink.io/v1").rstrip("/")
USER_AGENT = "minimax-h3-shortdrama-pipeline/1.0"   # sent on every request, including upload PUTs
RATE = {"768p": 0.039, "2k": 0.064}            # USD per billed second (model page, 2026-10-07)
RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
LIST_FIELD = {"image": "reference_images", "video": "reference_videos", "audio": "reference_audios"}
TAG = {"image": "Image", "video": "Video", "audio": "Audio"}
CAP = {"image": 9, "video": 3, "audio": 3}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp4": "video/mp4",
        ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".wav": "audio/wav"}
MENTION = re.compile(r"【([^】]+)】")
MEDIA_URL_DAYS = 7


class DramaError(Exception):
    pass


# ----------------------------------------------------------------------------- files

def read_json(path, default=None):
    if not os.path.exists(path):
        if default is not None:
            return default
        raise DramaError(f"missing {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def episode(ep):
    bible = read_json(os.path.join(ep, "bible.json"))
    shots = read_json(os.path.join(ep, "shots.json"))
    state = read_json(os.path.join(ep, "production.json"), default={"shots": {}, "uploads": {}})
    return bible, shots, state


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------------- shot -> request

def plan_shot(ep, bible, shot, draft=False):
    """Return (request body with local paths, billed seconds, fingerprint). Raises DramaError."""
    sid = shot.get("id", "?")
    duration = 4 if draft else shot.get("duration", 5)
    tier = "768p" if draft else shot.get("resolution", bible.get("resolution", "768p"))
    if not isinstance(duration, int) or not 4 <= duration <= 15:
        raise DramaError(f"{sid}: 时长必须是 4-15 秒的整数 (duration 4-15 s)")
    if tier not in RATE:
        raise DramaError(f"{sid}: 分辨率只能是 768p 或 2k")
    cast = bible.get("cast", {})
    names = shot.get("cast", [])
    prompt = shot.get("prompt", "").strip()
    if not prompt:
        raise DramaError(f"{sid}: prompt 为空")

    body = {"model": "minimax-h3", "duration": duration, "resolution": tier}
    files = []
    if shot.get("first_frame"):
        if names:
            raise DramaError(f"{sid}: 首帧/首尾帧镜头不能同时使用角色参考 (frames and references cannot mix)")
        body["aspect_ratio"] = "adaptive"
        body["start_image"] = {"url": shot["first_frame"]}
        files.append(shot["first_frame"])
        if shot.get("last_frame"):
            body["end_image"] = {"url": shot["last_frame"]}
            files.append(shot["last_frame"])
    else:
        ratio = shot.get("aspect_ratio", bible.get("aspect_ratio", "9:16"))
        if ratio not in RATIOS:
            raise DramaError(f"{sid}: 画幅 {ratio} 不支持")
        body["aspect_ratio"] = ratio

    number = {"image": 0, "video": 0, "audio": 0}
    tags = {}
    ref_seconds = 0.0
    for name in names:
        if name not in cast:
            raise DramaError(f"{sid}: 【{name}】不在 bible.json 的 cast 里")
        member = cast[name]
        kind = member["type"]
        number[kind] += 1
        tags[name] = f"@{TAG[kind]} {number[kind]}"
        body.setdefault(LIST_FIELD[kind], []).append({"url": member["file"]})
        files.append(member["file"])
        if kind == "video":
            ref_seconds += member.get("seconds", 0)
    if any(number[k] > CAP[k] for k in CAP) or sum(number.values()) > 12:
        raise DramaError(f"{sid}: 参考素材超限 (最多 9 图、3 视频、3 音频，共 12)")
    if number["audio"] and not (number["image"] or number["video"]):
        raise DramaError(f"{sid}: 音频参考必须搭配至少一张图片或一段视频")

    def swap(match):
        name = match.group(1)
        if name not in tags:
            raise DramaError(f"{sid}: 提示词提到【{name}】，但镜头的 cast 没有列出")
        return tags[name]

    # "@Image 1" plus a space, so a following digit or letter cannot merge into the number;
    # the space is dropped again before punctuation.
    body["prompt"] = re.sub(r" +(?=[，。、：；！？,.:;!?”’）)\s]|$)", "", MENTION.sub(lambda m: swap(m) + " ", prompt))
    if len(body["prompt"]) > 7000:
        raise DramaError(f"{sid}: 提示词超过 7000 字符")
    billed = duration + math.floor(ref_seconds)
    digest_parts = [json.dumps(body, sort_keys=True, ensure_ascii=False)]
    for rel in files:
        path = os.path.join(ep, rel)
        digest_parts.append(file_digest(path) if os.path.exists(path) else f"missing:{rel}")
    fingerprint = hashlib.sha256("|".join(digest_parts).encode()).hexdigest()[:16]
    return body, billed, fingerprint


def cost_rows(ep, bible, shots, draft=False):
    rows, total_s, total_usd = [], 0, 0.0
    for shot in shots:
        body, billed, _ = plan_shot(ep, bible, shot, draft)
        usd = billed * RATE[body["resolution"]]
        rows.append((shot["id"], body["duration"], body["resolution"], billed, usd))
        total_s += body["duration"]
        total_usd += usd
    return rows, total_s, round(total_usd, 4)


# ----------------------------------------------------------------------------- HTTP

def http(method, url, payload=None, raw=False, auth=True, ctype=None):
    data = payload if isinstance(payload, bytes) or payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if auth:
        key = os.environ.get("H3_API_KEY")
        if not key:
            raise DramaError("请先设置 H3_API_KEY (export H3_API_KEY=...)")
        req.add_header("Authorization", f"Bearer {key}")
    if ctype or (payload is not None and not isinstance(payload, bytes)):
        req.add_header("Content-Type", ctype or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = resp.read()
    except urllib.error.HTTPError as err:
        raise DramaError(f"HTTP {err.code} {method} {url}: {err.read().decode('utf-8', 'replace')[:300]}") from None
    except urllib.error.URLError as err:
        raise DramaError(f"网络错误 {method} {url}: {err.reason}") from None
    return out if raw else json.loads(out)


def media_url(ep, state, rel):
    """Upload a local reference once and reuse its media_url until it is about to expire."""
    if rel.startswith("https://"):
        return rel
    path = os.path.join(ep, rel)
    if not os.path.exists(path):
        raise DramaError(f"找不到素材 {path}")
    digest = file_digest(path)
    cached = state["uploads"].get(rel)
    if cached and cached["digest"] == digest and cached["expires"] > time.time() + 3600:
        return cached["media_url"]
    ctype = MIME.get(os.path.splitext(rel)[1].lower())
    if not ctype:
        raise DramaError(f"{rel}: 只支持 PNG/JPEG、MP4/MOV、MP3/WAV")
    ticket = http("POST", f"{BASE}/media/uploads", {"model": "minimax-h3", "type": ctype.split("/")[0],
                                                     "content_type": ctype, "size_bytes": os.path.getsize(path)})
    with open(path, "rb") as fh:
        http("PUT", ticket["upload_url"], fh.read(), raw=True, auth=False, ctype=ctype)
    state["uploads"][rel] = {"media_url": ticket["media_url"], "digest": digest,
                             "expires": time.time() + MEDIA_URL_DAYS * 86400}
    return ticket["media_url"]


def with_urls(ep, state, body):
    out = json.loads(json.dumps(body))
    for key in ("start_image", "end_image"):
        if key in out:
            out[key]["url"] = media_url(ep, state, out[key]["url"])
    for key in LIST_FIELD.values():
        for item in out.get(key, []):
            item["url"] = media_url(ep, state, item["url"])
    return out


# ----------------------------------------------------------------------------- commands

def shot_status(ep, bible, shot, state):
    """draft / approved / submitted / pending / done / failed, or stale when inputs changed."""
    entry = state["shots"].get(shot["id"], {})
    final_fp = plan_shot(ep, bible, shot)[2]
    if entry.get("fingerprint") and entry["fingerprint"] != plan_shot(ep, bible, shot, entry.get("draft", False))[2]:
        return "stale: 渲染后输入已改 (re-approve and re-render)"
    if entry.get("approved_fingerprint") and entry["approved_fingerprint"] != final_fp:
        return "changed: 审批后已修改 (approve again)"
    if entry.get("status"):
        return entry["status"] + (" (draft)" if entry.get("draft") else "")
    return "approved" if entry.get("approved_fingerprint") else "draft"


def cmd_check(ep, args):
    bible, shots, state = episode(ep)
    rows, seconds, total = cost_rows(ep, bible, shots, args.draft)
    print("| 镜头 | 时长 | 分辨率 | 计费秒数 | 费用 | 状态 |")
    print("|---|---:|---|---:|---:|---|")
    for (sid, dur, tier, billed, usd), shot in zip(rows, shots):
        print(f"| {sid} | {dur} s | {tier} | {billed} | ${usd:.3f} | {shot_status(ep, bible, shot, state)} |")
    print(f"\n合计 total: {len(shots)} 个镜头, 成片 {seconds} 秒, ${total:.3f}")
    return 0


def cmd_approve(ep, args):
    bible, shots, state = episode(ep)
    wanted = {s["id"] for s in shots} if args.all else set(args.shots)
    unknown = wanted - {s["id"] for s in shots}
    if unknown:
        raise DramaError(f"没有这些镜头: {', '.join(sorted(unknown))}")
    for shot in shots:
        if shot["id"] in wanted:
            _, billed, fp = plan_shot(ep, bible, shot)
            entry = state["shots"].setdefault(shot["id"], {})
            entry.update(approved_fingerprint=fp, approved_at=int(time.time()))
            print(f"{shot['id']}: approved ({billed} s)")
    write_json(os.path.join(ep, "production.json"), state)
    return 0


def cmd_render(ep, args):
    bible, shots, state = episode(ep)
    cost_rows(ep, bible, shots, args.draft)                    # whole episode must validate first
    path = os.path.join(ep, "production.json")
    os.makedirs(os.path.join(ep, "renders"), exist_ok=True)
    for shot in shots:
        sid = shot["id"]
        entry = state["shots"].setdefault(sid, {})
        body, billed, fp = plan_shot(ep, bible, shot, args.draft)
        approved_fp = plan_shot(ep, bible, shot)[2]
        if entry.get("approved_fingerprint") != approved_fp:
            print(f"{sid}: 未审批或审批后已修改，跳过 (run `approve` first)", file=sys.stderr)
            continue
        if entry.get("fingerprint") == fp and entry.get("status") in ("submitted", "pending", "done"):
            continue                                           # same request already paid for or running
        request_id = http("POST", f"{BASE}/videos/generations", with_urls(ep, state, body))["request_id"]
        entry.update(request_id=request_id, fingerprint=fp, status="submitted", billed_estimate=billed,
                     draft=args.draft, error=None)
        write_json(path, state)
        print(f"{sid}: submitted {request_id}", file=sys.stderr)
    while True:
        running = [(sid, e) for sid, e in state["shots"].items() if e.get("status") in ("submitted", "pending")]
        if not running:
            break
        for sid, entry in running:
            job = http("GET", f"{BASE}/videos/{entry['request_id']}")
            entry["status"] = job.get("status")
            if job.get("status") == "done":
                out = os.path.join(ep, "renders", f"{sid}{'-draft' if entry.get('draft') else ''}.mp4")
                with open(out, "wb") as fh:
                    fh.write(http("GET", f"{BASE}/videos/{entry['request_id']}/content", raw=True))
                entry.update(file=os.path.relpath(out, ep), usage=job.get("usage"))
                print(f"{sid}: done -> {out}", file=sys.stderr)
            elif job.get("status") in ("failed", "expired"):
                entry["error"] = job.get("error")
                print(f"{sid}: {job['status']} {job.get('error')} (未计费)", file=sys.stderr)
            write_json(path, state)
        if any(e.get("status") in ("submitted", "pending") for e in state["shots"].values()):
            time.sleep(args.poll)
    return 1 if any(e.get("status") in ("failed", "expired") for e in state["shots"].values()) else 0


def cmd_status(ep, args):
    _, shots, state = episode(ep)
    for shot in shots:
        e = state["shots"].get(shot["id"], {})
        billed = (e.get("usage") or {}).get("billed_seconds", "-")
        print(f"{shot['id']:<10} {e.get('status', 'draft'):<10} approved={'yes' if e.get('approved_fingerprint') else 'no':<3} "
              f"billed={billed} file={e.get('file', '-')}")
    return 0


def cmd_stitch(ep, args, run=subprocess.run):
    _, shots, state = episode(ep)
    files = []
    for shot in shots:
        e = state["shots"].get(shot["id"], {})
        if e.get("status") != "done" or not e.get("file"):
            raise DramaError(f"{shot['id']} 还没有成片，不能拼接")
        files.append(os.path.abspath(os.path.join(ep, e["file"])))
    if not shutil.which("ffmpeg"):
        raise DramaError("需要 ffmpeg")
    listing = os.path.join(ep, "renders", "concat.txt")
    with open(listing, "w", encoding="utf-8") as fh:
        fh.writelines(f"file '{f}'\n" for f in files)
    out = os.path.join(ep, f"{os.path.basename(os.path.abspath(ep))}.mp4")
    codec = ["-c:v", "libx264", "-crf", "18", "-c:a", "aac"] if args.reencode else ["-c", "copy"]
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing, *codec, out], check=True)
    print(out)
    return 0


SPLIT_SYSTEM = (
    "你是短剧分镜师。把用户给的剧本拆成镜头，输出 JSON 数组，不要输出别的文字。"
    "每个元素: {\"id\": \"S01\", \"duration\": 4-15 的整数秒, \"cast\": [用到的角色/场景名，只能来自给定名单], "
    "\"prompt\": \"景别、主体、一个动作、运镜、环境与光线、声音和台词\"}。"
    "提示词里用【名字】指代名单中的角色或场景；台词放在引号里并写明是谁说的；一个镜头只写一个动作。"
)


def cmd_split(ep, args):
    bible = read_json(os.path.join(ep, "bible.json"))
    with open(os.path.join(ep, "script.md"), encoding="utf-8") as fh:
        script = fh.read()
    names = "、".join(bible.get("cast", {}))
    reply = http("POST", f"{BASE}/chat/completions", {
        "model": args.model,
        "messages": [{"role": "system", "content": SPLIT_SYSTEM},
                     {"role": "user", "content": f"可用名单：{names}\n\n剧本：\n{script}"}],
    })
    text = reply["choices"][0]["message"]["content"]
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise DramaError("模型没有返回 JSON 数组；请重试或手写 shots.json")
    shots = json.loads(match.group(0))
    prefix = os.path.basename(os.path.abspath(ep)).split("-")[0]
    for i, shot in enumerate(shots, 1):
        shot["id"] = f"{prefix}-S{i:02d}"
    problems = []
    for shot in shots:
        try:
            plan_shot(ep, bible, shot)
        except DramaError as err:
            problems.append(str(err))
    target = os.path.join(ep, "shots.json" if not os.path.exists(os.path.join(ep, "shots.json")) else "shots.draft.json")
    write_json(target, shots)
    print(f"{len(shots)} 个镜头 -> {target}")
    for p in problems:
        print("需要修改:", p)
    return 0 if not problems else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="MiniMax H3 短剧生产管线")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "render", "status", "stitch", "split", "approve"):
        p = sub.add_parser(name)
        p.add_argument("episode")
        if name in ("check", "render"):
            p.add_argument("--draft", action="store_true", help="每个镜头 4 秒 768p 的低成本预览")
        if name == "render":
            p.add_argument("--poll", type=float, default=10)
        if name == "stitch":
            p.add_argument("--reencode", action="store_true")
        if name == "split":
            p.add_argument("--model", default="gpt-5.6-luna")
        if name == "approve":
            p.add_argument("shots", nargs="*")
            p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    try:
        return globals()[f"cmd_{a.cmd}"](a.episode, a)
    except DramaError as err:
        print(f"错误: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
