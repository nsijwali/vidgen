"""Narration cache key and timing (spec batch1 PR C)."""
import subprocess

import numpy as np
import pytest

from fakes import _fake_voice_server
from yamlreel import render as render_mod
from yamlreel import voice as voice_mod
from yamlreel.config import Config
from yamlreel.render import Runtime, Timeline, render
from yamlreel.specs import validate
from yamlreel.voice import SAMPLE_RATE, VoiceError, VoiceServer

BPM = 72
BEAT = 60.0 / BPM


def plan_durations(*args, **kwargs):
    return render_mod.plan_durations(*args, **kwargs)  # looked up per call so each test fails on its own


def _spec(*seconds, snap=True, narrate=False, **extra):
    scenes = []
    for n, s in enumerate(seconds):
        sc = {"type": "title", "title": f"T{n}", "seconds": s}
        if narrate:
            sc["narration"] = f"Line number {n}"
        scenes.append(sc)
    return validate({"scenes": scenes, "music": {"style": "none", "bpm": BPM, "snap": snap}, **extra}, Config())


# --- plan_durations ------------------------------------------------------------------------------------------

def test_plan_durations_reproduction_two_short_scenes():
    spec = _spec(1.1, 1.1)
    assert plan_durations(spec) == pytest.approx([BEAT, BEAT])
    assert plan_durations(spec, {1: 1.0}) == pytest.approx([BEAT, 2 * BEAT])


def test_plan_durations_rounds_narrated_scenes_up_and_silent_ones_to_nearest():
    spec = _spec(1.1, 1.1, 1.1)
    d = plan_durations(spec, {0: 0.9})
    assert d == pytest.approx([2 * BEAT, BEAT, BEAT])  # only the narrated scene grows


def test_plan_durations_floor_on_a_beat_boundary_does_not_add_a_beat():
    spec = _spec(0.5)
    assert plan_durations(spec, {0: 2 * BEAT}) == pytest.approx([2 * BEAT])
    assert plan_durations(spec, {0: 2 * BEAT + 0.01}) == pytest.approx([3 * BEAT])


def test_plan_durations_without_snap_is_max_of_length_and_floor():
    spec = _spec(1.1, 3.0, snap=False)
    assert plan_durations(spec, {0: 1.5, 1: 1.0}) == pytest.approx([1.5, 3.0])


def test_plan_durations_epsilon_keeps_an_exact_one_beat_floor_at_one_beat():
    # 62370 samples + 0.3 s lead is exactly one beat at 35 bpm; float noise must not add a second beat
    floor = voice_mod.min_scene_seconds(np.zeros(62370, dtype=np.float32), 0.0)
    spec = _spec(0.5)
    spec["music"] = {"style": "none", "bpm": 35, "snap": True}
    assert plan_durations(spec, {0: floor}) == pytest.approx([60 / 35])


def test_plan_durations_snap_needs_a_bpm():
    spec = _spec(1.1, snap=True)
    spec["music"] = {"style": "none", "snap": True}
    assert plan_durations(spec, {0: 2.0}) == pytest.approx([2.0])


# --- the narration lines up with the picture ----------------------------------------------------------------------

@pytest.mark.parametrize("snap", [True, False])
def test_each_clip_starts_at_its_scene_start_plus_lead_and_fits_the_scene(tmp_path, monkeypatch, snap):
    clip_s = 0.4
    srv, calls, url = _fake_voice_server(seconds=clip_s, dc=0.5)
    timelines: list[Timeline] = []
    tracks: list[np.ndarray] = []

    class SpyTimeline(Timeline):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            timelines.append(self)

    real_write = voice_mod.write_wav

    def spy_write(path, samples):
        if path.name == "narration.wav":
            tracks.append(np.array(samples))
        return real_write(path, samples)

    monkeypatch.setattr(render_mod, "Timeline", SpyTimeline)
    monkeypatch.setattr(voice_mod, "write_wav", spy_write)
    try:
        spec = _spec(1.1, 1.1, snap=snap, narrate=True)
        render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
               runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
    finally:
        srv.shutdown()
    assert len(calls) == 2 and len(timelines) == 1 and len(tracks) == 1
    tl, track = timelines[0], tracks[0]
    nz = np.flatnonzero(track)
    run_starts = nz[np.concatenate(([True], np.diff(nz) > 1))]
    assert len(run_starts) == 2
    for i, first in enumerate(run_starts):
        assert abs(int(first) - round((tl.starts[i] + voice_mod.NARRATION_LEAD) * SAMPLE_RATE)) <= 1
        dur = tl.scenes[i].duration
        assert dur >= clip_s + 0.5 - 1e-9  # speech plus the default gap
        end = tl.starts[i] + voice_mod.NARRATION_LEAD + clip_s
        assert end <= tl.starts[i] + dur + 1e-9  # speech ends inside its scene


def test_build_track_places_clips_by_the_given_starts():
    clip = np.full(100, 0.5, dtype=np.float32)
    track = voice_mod.build_track({1: clip}, [0.0, 2.0], 3.0, lead=0.25)
    nz = np.flatnonzero(track)
    assert nz[0] == int(2.25 * SAMPLE_RATE) and len(nz) == 100


def test_min_scene_seconds_uses_the_larger_of_gap_and_lead():
    clip = np.zeros(SAMPLE_RATE, dtype=np.float32)
    assert voice_mod.min_scene_seconds(clip, 0.5) == pytest.approx(1.5)
    assert voice_mod.min_scene_seconds(clip, 0.0) == pytest.approx(1.0 + voice_mod.NARRATION_LEAD)


# --- cache key -----------------------------------------------------------------------------------------------

def _key(url="http://127.0.0.1:3900/mcp/", text="hi", voice=None, language=None, client_id="yamlreel"):
    return voice_mod.narration_cache_key(VoiceServer(url=url, client_id=client_id), text, voice, language)


def test_identity_differs_across_host_port_and_path():
    ids = {VoiceServer(url=u).identity() for u in (
        "http://127.0.0.1:3900/mcp/", "http://127.0.0.2:3900/mcp/", "http://127.0.0.1:3901/mcp/",
        "http://127.0.0.1:3900/other/", "https://127.0.0.1:3900/mcp/")}
    assert len(ids) == 5


def test_identity_ignores_userinfo_query_and_fragment_and_normalises():
    base = VoiceServer(url="http://voice.example/mcp/").identity()
    assert base == "http://voice.example:80/mcp/"
    assert VoiceServer(url="HTTP://user:secret@VOICE.example:80/mcp/?token=abc#frag").identity() == base
    assert VoiceServer(url="https://voice.example").identity() == "https://voice.example:443/"
    assert VoiceServer(url="http://[::1]:3900/mcp/").identity() == "http://[::1]:3900/mcp/"
    ident = VoiceServer(url="http://user:secret@h.example/p?token=abc").identity()
    assert not any(w in ident for w in ("user", "secret", "token"))
    assert _key("http://u:s@voice.example/mcp/?token=1") == _key("http://voice.example/mcp/?token=2")


def test_a_bad_port_is_a_voice_error():
    with pytest.raises(VoiceError):
        VoiceServer(url="http://voice.example:notaport/mcp/")
    with pytest.raises(VoiceError):
        VoiceServer(url="http://voice.example:99999/mcp/")


def test_key_changes_with_server_voice_language_and_text():
    base = _key()
    assert len({base, _key("http://127.0.0.1:3901/mcp/"), _key(voice="warm"), _key(language="en"), _key(text="bye")}) == 5


def test_client_id_only_matters_when_no_voice_is_set():
    assert _key(client_id="a") != _key(client_id="b")
    assert _key(voice="warm", client_id="a") == _key(voice="warm", client_id="b")


def test_cache_key_version_changes_the_key(monkeypatch):
    before = _key()
    monkeypatch.setattr(voice_mod, "CACHE_KEY_VERSION", voice_mod.CACHE_KEY_VERSION + 1)
    assert _key() != before


def test_cache_file_name_is_versioned(tmp_path):
    srv, _, url = _fake_voice_server(seconds=0.3)
    try:
        spec = _spec(1.0, narrate=True)
        render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
               runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
    finally:
        srv.shutdown()
    names = [p.name for p in (tmp_path / "o" / ".cache").glob("*.wav")]
    assert len(names) == 1 and names[0].startswith("narration-v2-")


def test_two_servers_do_not_share_cached_clips(tmp_path):
    spec = _spec(1.0, narrate=True)
    servers = [_fake_voice_server(seconds=0.3) for _ in range(2)]
    try:
        for _, _, url in servers:
            render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
                   runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
    finally:
        for srv, _, _ in servers:
            srv.shutdown()
    assert [len(c) for _, c, _ in servers] == [1, 1]


def test_the_same_server_reuses_the_cache(tmp_path):
    spec = _spec(1.0, narrate=True)
    srv, calls, url = _fake_voice_server(seconds=0.3)
    try:
        for _ in range(2):
            render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
                   runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
    finally:
        srv.shutdown()
    assert len(calls) == 1


def _decode_mono_8k(video):
    """Decode a video's audio to mono 8 kHz float samples with the bundled ffmpeg (ffprobe may be absent)."""
    ffmpeg = render_mod.ffmpeg_exe()
    out = subprocess.run([ffmpeg, "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0


def test_narration_without_music_is_audible_to_the_end(tmp_path):
    srv, calls, url = _fake_voice_server(seconds=1.0)
    try:
        spec = _spec(1.0, 1.0, snap=False, narrate=True)
        res = render(spec, "square", tmp_path / "o", fps=5, scale=0.1, contact=False,
                     runtime=Runtime(roots=[tmp_path], voice=VoiceServer(url=url)))
    finally:
        srv.shutdown()
    assert len(calls) == 2
    audio = _decode_mono_8k(res["video"])
    rate = 8000
    # scenes are 1.5 s each (1.0 s of speech + 0.5 s gap); speech runs from start + lead for 1.0 s
    for start in (0.0, 1.5):
        a, b = start + voice_mod.NARRATION_LEAD + 0.1, start + voice_mod.NARRATION_LEAD + 0.9
        t = a
        while t + 0.1 <= b + 1e-9:
            win = audio[int(t * rate):int((t + 0.1) * rate)]
            assert float(np.sqrt(np.mean(win ** 2))) > 0.05, f"silent at {t:.1f}s"
            t += 0.1


def test_identity_keeps_path_params_and_follows_a_reassigned_url():
    assert VoiceServer(url="http://h/mcp/;a").identity() != VoiceServer(url="http://h/mcp/;b").identity()
    s = VoiceServer(url="http://h:81/mcp/")
    s.url = "http://h/mcp/"
    assert s.identity() == "http://h:80/mcp/"
