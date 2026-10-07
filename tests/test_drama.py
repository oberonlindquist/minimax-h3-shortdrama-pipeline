"""Offline tests for drama.py: fake HTTP, temporary episode folders, no key or charges."""
import io
import json
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import drama  # noqa: E402

EXAMPLE = os.path.join(ROOT, "episodes", "E01-rain-letter")


@pytest.fixture
def ep(tmp_path):
    target = tmp_path / "E01-rain-letter"
    shutil.copytree(EXAMPLE, target)
    refs = target / "refs"
    refs.mkdir(exist_ok=True)
    for name in ("linxia.png", "zhouyu.png", "bookstore.png", "street.png", "letter-blurry.png", "letter-sharp.png"):
        (refs / name).write_bytes(b"png:" + name.encode())
    (refs / "rain-10s.wav").write_bytes(b"wav")
    (refs / "door-push-3s.mp4").write_bytes(b"mp4")
    return str(target)


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def api(monkeypatch):
    log = {"calls": [], "chat": None}

    def urlopen(req, timeout=None):
        url = req.full_url
        log["calls"].append((req.get_method(), url, req.data))
        log.setdefault("agents", set()).add(req.get_header("User-agent"))
        if url.endswith("/media/uploads"):
            n = len(log["calls"])
            return Reply(json.dumps({"upload_url": f"https://put.test/{n}", "media_url": f"https://cdn.test/{n}"}).encode())
        if url.startswith("https://put.test/"):
            return Reply(b"")
        if url.endswith("/videos/generations"):
            return Reply(json.dumps({"request_id": f"job-{len(log['calls'])}"}).encode())
        if url.endswith("/content"):
            return Reply(b"MP4")
        if "/videos/" in url:
            return Reply(json.dumps({"status": "done", "usage": {"billed_seconds": 6}}).encode())
        if url.endswith("/chat/completions"):
            return Reply(json.dumps({"choices": [{"message": {"content": log["chat"]}}]}).encode())
        raise AssertionError(url)

    monkeypatch.setenv("H3_API_KEY", "k")
    monkeypatch.setattr(drama.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(drama.time, "sleep", lambda s: None)
    return log


def submits(log):
    return [json.loads(d) for m, u, d in log["calls"] if u.endswith("/videos/generations")]


def test_example_episode_prices_out(ep):
    bible, shots, _ = drama.episode(ep)
    rows, seconds, total = drama.cost_rows(ep, bible, shots)
    assert len(rows) == 8 and seconds == 47
    assert total == pytest.approx(50 * 0.039)          # 47 output s + 3 s door-push reference clip


def test_mentions_become_numbered_references(ep):
    bible, shots, _ = drama.episode(ep)
    body, billed, _ = drama.plan_shot(ep, bible, shots[3])
    assert body["prompt"].startswith("过肩镜头：@Image 1 把一封泛黄的信")
    assert "@Image 2" in body["prompt"] and "@Image 3" in body["prompt"]
    assert [r["url"] for r in body["reference_images"]] == ["refs/linxia.png", "refs/zhouyu.png", "refs/bookstore.png"]
    s02, billed02, _ = drama.plan_shot(ep, bible, shots[1])
    assert "@Video 1" in s02["prompt"] and billed02 == 9


@pytest.mark.parametrize("bad, message", [
    ({"id": "X", "duration": 20, "prompt": "p"}, "4-15"),
    ({"id": "X", "resolution": "1080p", "prompt": "p"}, "768p"),
    ({"id": "X", "cast": ["雨声"], "prompt": "【雨声】"}, "音频参考"),
    ({"id": "X", "cast": ["无名氏"], "prompt": "p"}, "不在"),
    ({"id": "X", "cast": [], "prompt": "【林夏】走进来"}, "没有列出"),
    ({"id": "X", "cast": ["林夏"], "first_frame": "refs/a.png", "prompt": "p"}, "不能同时"),
])
def test_rejections(ep, bad, message):
    bible, _, _ = drama.episode(ep)
    with pytest.raises(drama.DramaError, match=message):
        drama.plan_shot(ep, bible, bad)


def test_only_approved_shots_render_and_uploads_are_reused(ep, api):
    drama.main(["approve", ep, "E01-S03", "E01-S07"])
    assert drama.main(["render", ep]) == 0
    sent = submits(api)
    assert len(sent) == 2
    uploads = [u for m, u, d in api["calls"] if u.endswith("/media/uploads")]
    assert len(uploads) == 2                            # zhouyu.png and bookstore.png, each once
    state = json.load(open(os.path.join(ep, "production.json")))
    assert state["shots"]["E01-S03"]["status"] == "done"
    assert open(os.path.join(ep, "renders", "E01-S03.mp4"), "rb").read() == b"MP4"
    assert api["agents"] == {"minimax-h3-shortdrama-pipeline/1.0"}      # uploads, PUTs, submits, polls

    api["calls"].clear()
    drama.main(["render", ep])                          # nothing new: same fingerprints, already done
    assert submits(api) == []


def test_changing_an_input_marks_the_shot_stale_and_blocks_render(ep, api, capsys):
    drama.main(["approve", ep, "--all"])
    drama.main(["render", ep])
    with open(os.path.join(ep, "refs", "zhouyu.png"), "wb") as fh:
        fh.write(b"a different face")
    capsys.readouterr()
    drama.main(["check", ep])
    table = capsys.readouterr().out
    assert table.count("stale") == 5                    # S03-S07 all use 周屿
    api["calls"].clear()
    drama.main(["render", ep])
    assert submits(api) == []                           # stale shots need a fresh approval before money is spent


def test_split_drafts_a_shot_list_from_the_script(ep, api):
    api["chat"] = 'Here you go:\n[{"duration": 5, "cast": ["林夏"], "prompt": "【林夏】推门进来"}, ' \
                  '{"duration": 30, "cast": [], "prompt": "too long"}]'
    code = drama.main(["split", ep])
    draft = json.load(open(os.path.join(ep, "shots.draft.json"), encoding="utf-8"))
    assert [s["id"] for s in draft] == ["E01-S01", "E01-S02"]
    assert code == 2                                     # the 30-second shot is reported for fixing
    chat = [json.loads(d) for m, u, d in api["calls"] if u.endswith("/chat/completions")][0]
    assert chat["model"] == "gpt-5.6-luna" and "林夏" in chat["messages"][1]["content"]


def test_stitch_builds_an_ffmpeg_concat(ep, api, monkeypatch):
    drama.main(["approve", ep, "--all"])
    drama.main(["render", ep])
    seen = {}
    monkeypatch.setattr(drama.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    args = drama.argparse.Namespace(reencode=False)
    drama.cmd_stitch(ep, args, run=lambda cmd, check: seen.setdefault("cmd", cmd))
    assert seen["cmd"][:6] == ["ffmpeg", "-y", "-f", "concat", "-safe", "0"]
    assert seen["cmd"][-3:-1] == ["-c", "copy"] and seen["cmd"][-1].endswith("E01-rain-letter.mp4")
    listing = open(os.path.join(ep, "renders", "concat.txt"), encoding="utf-8").read().splitlines()
    assert len(listing) == 8 and listing[0].endswith("E01-S01.mp4'")
