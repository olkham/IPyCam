"""
Tests for VideoStreamer's decoupled writer thread (go2rtc / FFmpeg path).

After the rework, stream() only enqueues onto a bounded drop-oldest queue; a
dedicated writer thread performs the blocking stdin.write to FFmpeg. These
tests verify that a blocked/back-pressured pipe can NEVER freeze the caller,
that overflow is counted as dropped frames, and that stop() joins the writer.
"""

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from ipycam.streamer import VideoStreamer, StreamConfig, HWAccel


def make_frame(w=320, h=240):
    return np.zeros((h, w, 3), dtype=np.uint8)


def make_mock_process(poll_return=None):
    """A MagicMock standing in for a subprocess.Popen FFmpeg handle.

    ``stderr.readline`` returns b'' immediately (EOF), matching what happens
    once the real process dies, so the background stderr-reader thread
    started by ``_start_ffmpeg`` exits right away instead of spinning.
    """
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stderr = MagicMock()
    proc.stderr.readline.return_value = b''
    proc.poll.return_value = poll_return
    proc.wait.return_value = 0
    return proc


def _fake_running_streamer(write_side_effect=None):
    """Build a VideoStreamer wired to a fake FFmpeg process with a live writer.

    Bypasses the real FFmpeg launch: we inject a MagicMock process whose
    stdin.write can be made to block, then start the writer thread directly.
    """
    cfg = StreamConfig(width=320, height=240, fps=10)
    s = VideoStreamer(cfg)
    proc = MagicMock()
    proc.stdin = MagicMock()
    if write_side_effect is not None:
        proc.stdin.write.side_effect = write_side_effect
    s._ffmpeg_process = proc
    s._is_running = True
    s._start_writer()
    return s, proc


def test_stream_returns_false_when_not_running():
    s = VideoStreamer(StreamConfig(width=320, height=240))
    assert s.stream(make_frame()) is False


def test_stream_returns_immediately_when_write_blocks():
    """stdin.write sleeping 1s must not delay stream() at all."""
    s, _ = _fake_running_streamer(write_side_effect=lambda b: time.sleep(1.0))
    try:
        start = time.time()
        for _ in range(5):
            assert s.stream(make_frame()) is True
        elapsed = time.time() - start
        assert elapsed < 0.1, f"stream() blocked on the writer ({elapsed:.3f}s)"
    finally:
        s._is_running = False
        s._stop_writer()


def test_dropped_frames_increments_on_overflow():
    """With the writer stuck, the bounded queue overflows -> dropped_frames++."""
    s, _ = _fake_running_streamer(write_side_effect=lambda b: time.sleep(1.0))
    try:
        for _ in range(20):
            s.stream(make_frame())
        assert s.stats.dropped_frames > 0
    finally:
        s._is_running = False
        s._stop_writer()


def test_writer_delivers_frames_when_not_blocked():
    """Happy path: queued frames reach FFmpeg stdin and stats advance."""
    s, proc = _fake_running_streamer()
    try:
        for _ in range(5):
            s.stream(make_frame())
        # Writer drains asynchronously; wait for at least one delivery.
        deadline = time.time() + 2.0
        while time.time() < deadline and not proc.stdin.write.called:
            time.sleep(0.01)
        assert proc.stdin.write.called
        assert s.stats.frames_sent >= 1
    finally:
        s._is_running = False
        s._stop_writer()


def test_stop_joins_writer_thread():
    cfg = StreamConfig(width=320, height=240, fps=10)
    s = VideoStreamer(cfg)
    proc = MagicMock()
    proc.stdin = MagicMock()
    s._ffmpeg_process = proc
    s._is_running = True
    s._start_writer()

    writer = s._writer_thread
    assert writer is not None and writer.is_alive()

    s.stop()

    assert not writer.is_alive()
    assert s._writer_thread is None
    assert s._is_running is False


def test_writer_survives_frame_size_mismatch():
    """A wrong-sized frame is resized on the writer thread, not the caller."""
    s, proc = _fake_running_streamer()
    try:
        # 640x480 into a 320x240 stream -> writer resizes before writing.
        s.stream(make_frame(640, 480))
        deadline = time.time() + 2.0
        while time.time() < deadline and not proc.stdin.write.called:
            time.sleep(0.01)
        assert proc.stdin.write.called
        # 320*240*3 bytes written after resize.
        written = proc.stdin.write.call_args[0][0]
        assert len(written) == 320 * 240 * 3
    finally:
        s._is_running = False
        s._stop_writer()


# ---------------------------------------------------------------------------
# FFmpeg subprocess robustness: stdout disposition, stderr-reader lifecycle,
# and the writer thread's bounded reconnect after a broken pipe.
# ---------------------------------------------------------------------------


def test_start_ffmpeg_spawns_with_stdout_devnull(monkeypatch):
    """Nothing ever reads stdout: it must be DEVNULL, not PIPE, or FFmpeg can
    fill the OS pipe buffer and block forever writing to its own stdout."""
    s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return make_mock_process()

    monkeypatch.setattr('ipycam.streamer.subprocess.Popen', fake_popen)

    s._start_ffmpeg("rtmp://127.0.0.1/test", None, HWAccel.CPU)

    assert captured.get('stdout') is subprocess.DEVNULL
    # stderr IS consumed by the reader thread, so PIPE remains correct there.
    assert captured.get('stderr') is subprocess.PIPE
    assert captured.get('stdin') is subprocess.PIPE


def test_stderr_reader_thread_handle_stored_and_joined_by_stop(monkeypatch):
    """The stderr-reader thread's handle must be retrievable and stop() must
    join it (not merely fire-and-forget it as a bare local variable)."""
    s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
    proc = make_mock_process(poll_return=None)
    monkeypatch.setattr('ipycam.streamer.subprocess.Popen', lambda *a, **k: proc)

    s._start_ffmpeg("rtmp://127.0.0.1/test", None, HWAccel.CPU)
    thread = s._stderr_thread
    assert thread is not None, "stderr-reader thread handle was not stored"

    # readline() returning b'' means EOF -- the reader loop ends on its own,
    # same as it would once the real FFmpeg process exits.
    deadline = time.time() + 2.0
    while time.time() < deadline and thread.is_alive():
        time.sleep(0.01)
    assert not thread.is_alive(), "stderr reader never exited on EOF"

    s._is_running = True
    s._start_writer()
    s.stop()

    assert s._stderr_thread is None
    assert not thread.is_alive()


def test_broken_pipe_triggers_reconnect_and_resumes(monkeypatch):
    """A broken pipe must trigger a bounded reconnect: a NEW FFmpeg process is
    spawned and, once it's up, subsequent frames are written to it."""
    cfg = StreamConfig(width=320, height=240, fps=10)
    s = VideoStreamer(cfg)
    # Tiny/short-circuited reconnect parameters so the test is fast and
    # deterministic (defaults are tuned for real-world FFmpeg restarts).
    s.RECONNECT_INITIAL_BACKOFF = 0.01
    s.RECONNECT_MAX_BACKOFF = 0.01
    s.RECONNECT_MAX_ATTEMPTS = 3
    s.RECONNECT_CHECK_TIMEOUT = 0.05
    s.RECONNECT_WARMUP_TIMEOUT = 0.2

    proc1 = make_mock_process()
    proc1.stdin.write.side_effect = BrokenPipeError()  # dies on first write

    proc2 = make_mock_process(poll_return=None)  # the reconnect target
    spawned = []

    def fake_popen(cmd, **kwargs):
        spawned.append(kwargs)
        return proc2

    monkeypatch.setattr('ipycam.streamer.subprocess.Popen', fake_popen)

    # Bypass the real FFmpeg launch/hw-detection: inject proc1 directly and
    # start the writer, as the other tests in this file do.
    s._ffmpeg_process = proc1
    s._active_hw_accel = HWAccel.CPU.value
    s._rtmp_url = "rtmp://127.0.0.1/test"
    s._rtmp_url_sub = None
    s._is_running = True
    s._start_writer()

    try:
        assert s.stream(make_frame(320, 240)) is True

        # Writer hits BrokenPipeError and reconnects -> a second Popen call.
        deadline = time.time() + 5.0
        while time.time() < deadline and not spawned:
            time.sleep(0.01)
        assert spawned, "broken pipe never triggered a reconnect spawn"

        # _ffmpeg_process is assigned inside _start_ffmpeg, BEFORE the
        # post-restart check/warm-up run -- so wait on reconnect_count
        # (bumped only once both pass) to know the reconnect fully finished,
        # not just that a new process object was assigned.
        deadline = time.time() + 3.0
        while time.time() < deadline and s.reconnect_count < 1:
            time.sleep(0.01)
        assert s.reconnect_count == 1
        assert s._ffmpeg_process is proc2
        assert s.is_running is True, "a successful reconnect must not report permanent failure"

        # Streaming resumed: a fresh frame must land on the NEW process.
        assert s.stream(make_frame(320, 240)) is True
        deadline = time.time() + 2.0
        while time.time() < deadline and not proc2.stdin.write.called:
            time.sleep(0.01)
        assert proc2.stdin.write.called
        # The old (dead) process only ever saw the one failed write -- the
        # writer must not keep retrying against it after reconnecting.
        assert proc1.stdin.write.call_count == 1
    finally:
        s._is_running = False
        s._stop_writer()


def test_reconnect_exhausted_gives_up_permanently(monkeypatch):
    """When every reconnect attempt fails, the streamer must stop retrying
    (no infinite loop) and report permanent failure via is_running."""
    cfg = StreamConfig(width=320, height=240, fps=10)
    s = VideoStreamer(cfg)
    s.RECONNECT_INITIAL_BACKOFF = 0.01
    s.RECONNECT_MAX_BACKOFF = 0.01
    s.RECONNECT_MAX_ATTEMPTS = 2
    s.RECONNECT_CHECK_TIMEOUT = 0.05
    s.RECONNECT_WARMUP_TIMEOUT = 0.1

    proc1 = make_mock_process()
    proc1.stdin.write.side_effect = BrokenPipeError()

    def fake_popen(cmd, **kwargs):
        # Every reconnect attempt spawns a process that looks already-dead
        # (poll() returns an exit code), so _check_ffmpeg_running fails fast
        # and every attempt is exhausted quickly.
        return make_mock_process(poll_return=1)

    monkeypatch.setattr('ipycam.streamer.subprocess.Popen', fake_popen)

    s._ffmpeg_process = proc1
    s._active_hw_accel = HWAccel.CPU.value
    s._rtmp_url = "rtmp://127.0.0.1/test"
    s._is_running = True
    s._start_writer()

    try:
        assert s.stream(make_frame(320, 240)) is True

        writer = s._writer_thread
        deadline = time.time() + 10.0
        while time.time() < deadline and writer.is_alive():
            time.sleep(0.02)
        assert not writer.is_alive(), "writer thread never gave up (possible infinite retry loop)"
        assert s._is_running is False
        assert s.is_running is False
        assert s.reconnect_count == 0
    finally:
        s._is_running = False
        s._stop_writer()


def test_stop_joins_still_alive_stderr_thread(monkeypatch):
    """When the stderr-reader thread is STILL ALIVE at stop() time (unlike the
    other test, which waits for natural EOF first), stop() must actually
    join() it rather than skipping the join because it already exited."""
    s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
    proc = make_mock_process(poll_return=None)

    def slow_readline():
        time.sleep(0.2)
        return b''
    proc.stderr.readline.side_effect = slow_readline
    monkeypatch.setattr('ipycam.streamer.subprocess.Popen', lambda *a, **k: proc)

    s._start_ffmpeg("rtmp://127.0.0.1/test", None, HWAccel.CPU)
    thread = s._stderr_thread
    assert thread.is_alive(), "test setup assumption: thread must still be running"

    s._is_running = True
    s._start_writer()
    s.stop()

    assert not thread.is_alive()
    assert s._stderr_thread is None


def test_stop_interrupts_reconnect_backoff_promptly(monkeypatch):
    """stop() during a (long) backoff sleep must return promptly, not wait out
    the remaining backoff/attempts."""
    cfg = StreamConfig(width=320, height=240, fps=10)
    s = VideoStreamer(cfg)
    # Deliberately long backoff -- if stop() had to wait it out, this test
    # would hang/timeout instead of completing in well under a second.
    s.RECONNECT_INITIAL_BACKOFF = 30.0
    s.RECONNECT_MAX_BACKOFF = 30.0
    s.RECONNECT_MAX_ATTEMPTS = 5

    proc1 = make_mock_process()
    proc1.stdin.write.side_effect = BrokenPipeError()

    popen_mock = MagicMock(side_effect=AssertionError("Popen must not be reached during backoff"))
    monkeypatch.setattr('ipycam.streamer.subprocess.Popen', popen_mock)

    s._ffmpeg_process = proc1
    s._active_hw_accel = HWAccel.CPU.value
    s._rtmp_url = "rtmp://127.0.0.1/test"
    s._is_running = True
    s._start_writer()

    assert s.stream(make_frame(320, 240)) is True

    # Give the writer a moment to hit BrokenPipeError and enter the 30s
    # backoff wait.
    deadline = time.time() + 2.0
    while time.time() < deadline and not proc1.stdin.write.called:
        time.sleep(0.01)
    assert proc1.stdin.write.called
    time.sleep(0.1)  # let it settle into Event.wait(30.0)

    start = time.time()
    s.stop()
    elapsed = time.time() - start

    assert elapsed < 3.0, f"stop() waited out the reconnect backoff ({elapsed:.2f}s)"
    assert s._writer_thread is None
    assert s._is_running is False
    popen_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Step 3.5 additions: StreamStats math, the hw-accel selection ladder,
# _check_hw_encoder_available, FFmpeg command building (rtsp/flv, substream),
# _warm_up_encoder / _check_ffmpeg_running error-pattern detection, error
# dumping, and _cleanup_ffmpeg's termination fallbacks.
# ---------------------------------------------------------------------------

import subprocess as subprocess_module

from ipycam.streamer import StreamStats


class TestStreamStatsMath:
    def test_actual_fps_zero_with_fewer_than_two_samples(self):
        stats = StreamStats()
        assert stats.actual_fps == 0

    def test_actual_fps_positive_with_recent_samples(self):
        stats = StreamStats()
        now = time.time()
        for i in reversed(range(5)):
            stats.record_frame(now - 0.1 * i)
        assert stats.actual_fps > 0

    def test_actual_fps_zero_when_samples_outside_window(self):
        stats = StreamStats()
        old = time.time() - 30
        stats.record_frame(old)
        stats.record_frame(old + 0.1)
        assert stats.actual_fps == 0

    def test_actual_fps_zero_when_time_span_is_exactly_zero(self, monkeypatch):
        stats = StreamStats()
        frozen = 1_700_000_000.0
        monkeypatch.setattr('ipycam.streamer.time.time', lambda: frozen)
        stats.record_frame(frozen)
        stats.record_frame(frozen)
        assert stats.actual_fps == 0

    def test_bitrate_mbps_positive_with_elapsed_time_and_bytes(self):
        stats = StreamStats()
        stats.bytes_sent = 1_000_000
        assert stats.bitrate_mbps >= 0  # elapsed_time > 0 immediately after construction

    def test_bitrate_mbps_zero_when_elapsed_time_not_positive(self):
        stats = StreamStats()
        stats.start_time = time.time() + 100  # future -> elapsed_time negative
        assert stats.bitrate_mbps == 0

    def test_frame_size_and_expected_frame_bytes(self):
        s = VideoStreamer(StreamConfig(width=100, height=50))
        assert s.frame_size == (100, 50)
        assert s.expected_frame_bytes == 100 * 50 * 3


class TestCheckHwEncoderAvailable:
    def test_cpu_is_always_available_without_subprocess_call(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240))
        run_mock = MagicMock()
        monkeypatch.setattr('ipycam.streamer.subprocess.run', run_mock)
        assert s._check_hw_encoder_available(HWAccel.CPU) is True
        run_mock.assert_not_called()

    def test_nvenc_available_when_listed_in_encoders(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240))
        result = MagicMock(stdout=b"... h264_nvenc ...")
        monkeypatch.setattr('ipycam.streamer.subprocess.run', lambda *a, **k: result)
        assert s._check_hw_encoder_available(HWAccel.NVENC) is True

    def test_qsv_not_available_when_missing_from_encoders(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240))
        result = MagicMock(stdout=b"... libx264 only ...")
        monkeypatch.setattr('ipycam.streamer.subprocess.run', lambda *a, **k: result)
        assert s._check_hw_encoder_available(HWAccel.QSV) is False

    def test_exception_returns_false(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240))

        def raise_err(*a, **k):
            raise FileNotFoundError("ffmpeg not found")
        monkeypatch.setattr('ipycam.streamer.subprocess.run', raise_err)
        assert s._check_hw_encoder_available(HWAccel.NVENC) is False

    def test_unmapped_hw_type_is_available_without_subprocess_call(self, monkeypatch):
        """HWAccel.AUTO isn't in the NVENC/QSV encoder-name map, so the
        'unknown encoder name' branch short-circuits to True."""
        s = VideoStreamer(StreamConfig(width=320, height=240))
        run_mock = MagicMock()
        monkeypatch.setattr('ipycam.streamer.subprocess.run', run_mock)
        assert s._check_hw_encoder_available(HWAccel.AUTO) is True
        run_mock.assert_not_called()


class TestStartFfmpegCommandBuilding:
    def _captured_cmd(self, monkeypatch, s, rtmp_url, rtmp_url_sub, hw_type):
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured['cmd'] = cmd
            captured['kwargs'] = kwargs
            return make_mock_process()
        monkeypatch.setattr('ipycam.streamer.subprocess.Popen', fake_popen)
        s._start_ffmpeg(rtmp_url, rtmp_url_sub, hw_type)
        return captured['cmd']

    def test_cpu_uses_libx264_and_expected_encode_args(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
        cmd = self._captured_cmd(monkeypatch, s, "rtmp://127.0.0.1/test", None, HWAccel.CPU)
        assert "libx264" in cmd
        assert "-tune" in cmd and "zerolatency" in cmd

    def test_nvenc_uses_h264_nvenc_and_gpu_args(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
        cmd = self._captured_cmd(monkeypatch, s, "rtmp://127.0.0.1/test", None, HWAccel.NVENC)
        assert "h264_nvenc" in cmd
        assert "-gpu" in cmd
        assert "cbr" in cmd

    def test_qsv_uses_h264_qsv_and_global_quality(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
        cmd = self._captured_cmd(monkeypatch, s, "rtmp://127.0.0.1/test", None, HWAccel.QSV)
        assert "h264_qsv" in cmd
        assert "-global_quality" in cmd

    def test_rtsp_primary_url_uses_rtsp_transport(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
        cmd = self._captured_cmd(monkeypatch, s, "rtsp://127.0.0.1/main", None, HWAccel.CPU)
        assert "rtsp" in cmd
        assert "-rtsp_transport" in cmd
        assert "rtsp://127.0.0.1/main" in cmd

    def test_flv_primary_url_uses_flv_muxer(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10))
        cmd = self._captured_cmd(monkeypatch, s, "rtmp://127.0.0.1/main", None, HWAccel.CPU)
        assert "flv" in cmd
        assert "+global_header" in cmd

    def test_substream_rtsp_url_adds_second_output(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10, sub_width=160, sub_height=120))
        cmd = self._captured_cmd(
            monkeypatch, s, "rtmp://127.0.0.1/main", "rtsp://127.0.0.1/sub", HWAccel.CPU,
        )
        assert "rtsp://127.0.0.1/sub" in cmd
        assert "160x120" in cmd

    def test_substream_flv_url_adds_second_output(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=320, height=240, fps=10, sub_width=160, sub_height=120))
        cmd = self._captured_cmd(
            monkeypatch, s, "rtmp://127.0.0.1/main", "rtmp://127.0.0.1/sub", HWAccel.CPU,
        )
        assert "rtmp://127.0.0.1/sub" in cmd
        assert cmd.count("+global_header") == 2  # once per FLV output


class TestWarmUpEncoder:
    def test_success_sends_three_frames(self):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=1000))
        proc = make_mock_process(poll_return=None)
        s._ffmpeg_process = proc
        assert s._warm_up_encoder(timeout=5.0) is True
        assert proc.stdin.write.call_count == 3

    def test_broken_pipe_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=1000))
        proc = make_mock_process(poll_return=None)
        proc.stdin.write.side_effect = BrokenPipeError()
        s._ffmpeg_process = proc
        assert s._warm_up_encoder(timeout=1.0) is False

    def test_process_died_mid_warmup_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=1000))
        proc = make_mock_process()
        proc.poll.return_value = 1  # already dead by the first check
        s._ffmpeg_process = proc
        assert s._warm_up_encoder(timeout=1.0) is False

    def test_stderr_error_pattern_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=1000))
        proc = make_mock_process(poll_return=None)
        s._ffmpeg_process = proc
        s._ffmpeg_stderr_buffer = [b"Error: could not open encoder for stream"]
        assert s._warm_up_encoder(timeout=1.0) is False

    def test_no_stdin_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=1000))
        proc = make_mock_process(poll_return=None)
        proc.stdin = None
        s._ffmpeg_process = proc
        assert s._warm_up_encoder(timeout=1.0) is False

    def test_timeout_before_three_frames_returns_false(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=10))
        proc = make_mock_process(poll_return=None)
        s._ffmpeg_process = proc

        # Fake time.sleep to advance a fake clock past the timeout after the
        # first frame, so the loop exits having sent fewer than 3 frames --
        # deterministic, no real sleeping needed.
        state = {"t": 1000.0}

        def fake_time():
            return state["t"]

        def fake_sleep(secs):
            state["t"] += 10.0  # jump well past any reasonable timeout

        monkeypatch.setattr('ipycam.streamer.time.time', fake_time)
        monkeypatch.setattr('ipycam.streamer.time.sleep', fake_sleep)

        assert s._warm_up_encoder(timeout=0.2) is False
        assert proc.stdin.write.call_count == 1

    def test_unexpected_exception_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=1000))
        s._ffmpeg_process = None  # config.height access still fine, but force a TypeError path
        # Force an exception path inside the try: patch np.zeros to raise.
        with patch('ipycam.streamer.np.zeros', side_effect=ValueError("boom")):
            assert s._warm_up_encoder(timeout=1.0) is False


class TestCheckFfmpegRunning:
    def test_none_process_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._ffmpeg_process = None
        assert s._check_ffmpeg_running(timeout=0.1) is False

    def test_process_died_immediately_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process(poll_return=1)
        s._ffmpeg_process = proc
        assert s._check_ffmpeg_running(timeout=0.1) is False

    def test_stderr_error_pattern_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process(poll_return=None)
        s._ffmpeg_process = proc
        s._ffmpeg_stderr_buffer = [b"no NVENC capable devices found"]
        assert s._check_ffmpeg_running(timeout=0.3) is False

    def test_running_cleanly_returns_true(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process(poll_return=None)
        s._ffmpeg_process = proc
        assert s._check_ffmpeg_running(timeout=0.2) is True

    def test_dies_right_after_timeout_final_check_returns_false(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process()
        # check_interval is a fixed 0.2s, so with timeout=0.05 the loop body
        # runs exactly once (poll() -> None, alive) before the elapsed time
        # exceeds the timeout; the second poll() call is the final post-loop
        # check, which reports the process has since died.
        proc.poll.side_effect = [None, 1]
        s._ffmpeg_process = proc
        assert s._check_ffmpeg_running(timeout=0.05) is False


class TestDumpFfmpegError:
    def test_dump_with_buffer_logs_content(self, caplog):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._ffmpeg_stderr_buffer = [b"encoder init failed\n"]
        with caplog.at_level("ERROR"):
            s._dump_ffmpeg_error()
        assert any("encoder init failed" in r.message for r in caplog.records)

    def test_dump_with_whitespace_only_buffer_does_not_log(self, caplog):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._ffmpeg_stderr_buffer = [b"   \n  "]
        with caplog.at_level("ERROR"):
            s._dump_ffmpeg_error()
        assert not any("FFmpeg error output" in r.message for r in caplog.records)

    def test_dump_without_buffer_reads_process_stderr_directly(self, caplog):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._ffmpeg_stderr_buffer = []
        proc = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = b"late stderr output"
        s._ffmpeg_process = proc
        with caplog.at_level("ERROR"):
            s._dump_ffmpeg_error()
        assert any("late stderr output" in r.message for r in caplog.records)

    def test_dump_stderr_read_exception_is_logged(self, caplog):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._ffmpeg_stderr_buffer = []
        proc = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read.side_effect = OSError("closed")
        s._ffmpeg_process = proc
        with caplog.at_level("ERROR"):
            s._dump_ffmpeg_error()
        assert any("Could not read FFmpeg stderr" in r.message for r in caplog.records)


class TestCleanupFfmpeg:
    def test_normal_cleanup_terminates_and_waits(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process()
        s._ffmpeg_process = proc
        s._cleanup_ffmpeg()
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()
        assert s._ffmpeg_process is None

    def test_timeout_expired_falls_back_to_kill(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process()
        proc.wait.side_effect = subprocess_module.TimeoutExpired(cmd="ffmpeg", timeout=3)
        s._ffmpeg_process = proc
        s._cleanup_ffmpeg()
        proc.kill.assert_called_once()
        assert s._ffmpeg_process is None

    def test_generic_exception_is_swallowed(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        proc = make_mock_process()
        proc.terminate.side_effect = RuntimeError("boom")
        s._ffmpeg_process = proc
        s._cleanup_ffmpeg()  # must not raise
        assert s._ffmpeg_process is None

    def test_no_process_is_noop(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._ffmpeg_process = None
        s._cleanup_ffmpeg()  # must not raise


class TestStartHwAccelLadder:
    """VideoStreamer.start()'s hardware-acceleration selection ladder, with
    _check_hw_encoder_available / _start_ffmpeg / _check_ffmpeg_running /
    _warm_up_encoder mocked out at the method level (their own internals are
    covered by the dedicated test classes above)."""

    def _patch(self, s, monkeypatch, *, hw_available=None, ffmpeg_running=True,
               warm_up=True, start_ffmpeg_side_effect=None):
        hw_available = hw_available if hw_available is not None else (lambda hw: True)
        monkeypatch.setattr(s, '_check_hw_encoder_available', MagicMock(side_effect=hw_available))
        start_mock = MagicMock(side_effect=start_ffmpeg_side_effect)
        monkeypatch.setattr(s, '_start_ffmpeg', start_mock)
        monkeypatch.setattr(s, '_check_ffmpeg_running', MagicMock(return_value=ffmpeg_running))
        monkeypatch.setattr(s, '_warm_up_encoder', MagicMock(return_value=warm_up))
        monkeypatch.setattr(s, '_start_writer', MagicMock())
        return start_mock

    def test_already_running_returns_false(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.CPU))
        s._is_running = True
        assert s.start("rtmp://x") is False

    def test_specific_hw_accel_success(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.CPU))
        self._patch(s, monkeypatch)
        assert s.start("rtmp://x") is True
        assert s._active_hw_accel == "cpu"
        assert s.is_running is True
        s._start_writer.assert_called_once()

    def test_auto_tries_nvenc_then_qsv_then_succeeds_on_cpu(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.AUTO))
        attempted = []

        def hw_available(hw):
            attempted.append(hw)
            return hw == HWAccel.CPU
        self._patch(s, monkeypatch, hw_available=hw_available)

        assert s.start("rtmp://x") is True
        assert attempted == [HWAccel.NVENC, HWAccel.QSV, HWAccel.CPU]
        assert s._active_hw_accel == "cpu"

    def test_ffmpeg_check_failure_tries_next_hw_type(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.AUTO))
        # NVENC "starts" but never comes up; QSV succeeds.
        results = {"NVENC_tried": False}

        def ffmpeg_running():
            if not results["NVENC_tried"]:
                results["NVENC_tried"] = True
                return False
            return True
        monkeypatch.setattr(s, '_check_hw_encoder_available', MagicMock(return_value=True))
        monkeypatch.setattr(s, '_start_ffmpeg', MagicMock())
        monkeypatch.setattr(s, '_check_ffmpeg_running', MagicMock(side_effect=ffmpeg_running))
        monkeypatch.setattr(s, '_warm_up_encoder', MagicMock(return_value=True))
        monkeypatch.setattr(s, '_start_writer', MagicMock())
        monkeypatch.setattr(s, '_cleanup_ffmpeg', MagicMock())

        assert s.start("rtmp://x") is True
        assert s._active_hw_accel == "qsv"
        assert s._cleanup_ffmpeg.call_count >= 1

    def test_warm_up_failure_tries_next_hw_type(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.AUTO))
        warm_up_results = iter([False, True, True])
        monkeypatch.setattr(s, '_check_hw_encoder_available', MagicMock(return_value=True))
        monkeypatch.setattr(s, '_start_ffmpeg', MagicMock())
        monkeypatch.setattr(s, '_check_ffmpeg_running', MagicMock(return_value=True))
        monkeypatch.setattr(s, '_warm_up_encoder', MagicMock(side_effect=lambda: next(warm_up_results)))
        monkeypatch.setattr(s, '_start_writer', MagicMock())
        monkeypatch.setattr(s, '_cleanup_ffmpeg', MagicMock())

        assert s.start("rtmp://x") is True
        assert s._active_hw_accel == "qsv"  # NVENC's warm-up failed first

    def test_exception_during_attempt_is_handled_and_continues(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.AUTO))

        def start_ffmpeg_side_effect(*a, **k):
            if not getattr(start_ffmpeg_side_effect, "called", False):
                start_ffmpeg_side_effect.called = True
                raise RuntimeError("ffmpeg spawn failed")
        self._patch(s, monkeypatch, start_ffmpeg_side_effect=start_ffmpeg_side_effect)

        assert s.start("rtmp://x") is True
        assert s._active_hw_accel == "qsv"  # NVENC raised, QSV succeeded next

    def test_all_hw_types_fail_returns_false(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.AUTO))
        self._patch(s, monkeypatch, hw_available=lambda hw: False)
        assert s.start("rtmp://x") is False
        assert s.is_running is False

    def test_success_with_substream_logs_substream_line(self, monkeypatch, caplog):
        """Covers the `if rtmp_url_sub:` branch inside the success path."""
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.CPU))
        self._patch(s, monkeypatch)
        with caplog.at_level("INFO"):
            assert s.start("rtmp://main", "rtmp://sub") is True
        assert any("Substream: rtmp://sub" in r.message for r in caplog.records)

    # ---- Guaranteed CPU (libx264) fallback for a specific HW request -------

    def test_specific_qsv_request_appends_cpu_fallback(self, monkeypatch):
        """A specific QSV request extends the ladder with a CPU last resort:
        when QSV is entirely unavailable, CPU is still tried (and succeeds)."""
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.QSV))
        attempted = []

        def hw_available(hw):
            attempted.append(hw)
            return hw == HWAccel.CPU  # QSV unavailable, CPU available
        self._patch(s, monkeypatch, hw_available=hw_available)

        assert s.start("rtmp://x") is True
        assert attempted == [HWAccel.QSV, HWAccel.CPU]  # CPU fallback attempted
        assert s._active_hw_accel == "cpu"

    def test_specific_qsv_warmup_failure_falls_back_to_cpu(self, monkeypatch, caplog):
        """QSV is available and starts, but its warm-up (encoder init) fails.
        start() must fall back to the appended CPU (libx264) last resort and
        succeed with _active_hw_accel == 'cpu', logging a software-fallback
        warning -- a failing QSV warm-up must NOT kill the whole push path."""
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.QSV))
        attempted = []

        monkeypatch.setattr(s, '_check_hw_encoder_available', MagicMock(return_value=True))
        monkeypatch.setattr(
            s, '_start_ffmpeg',
            MagicMock(side_effect=lambda rtmp, sub, hw: attempted.append(hw)),
        )
        monkeypatch.setattr(s, '_check_ffmpeg_running', MagicMock(return_value=True))
        # Warm-up fails for QSV, succeeds for the CPU fallback.
        monkeypatch.setattr(
            s, '_warm_up_encoder',
            MagicMock(side_effect=lambda: attempted[-1] == HWAccel.CPU),
        )
        monkeypatch.setattr(s, '_start_writer', MagicMock())
        monkeypatch.setattr(s, '_cleanup_ffmpeg', MagicMock())

        with caplog.at_level("WARNING"):
            assert s.start("rtmp://x") is True
        assert attempted == [HWAccel.QSV, HWAccel.CPU]
        assert s._active_hw_accel == "cpu"
        assert s.is_running is True
        assert any("falling back to CPU" in r.message for r in caplog.records)

    def test_explicit_cpu_request_stays_cpu_only(self, monkeypatch):
        """An explicit CPU request must NOT get a duplicate CPU appended."""
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.CPU))
        attempted = []

        def hw_available(hw):
            attempted.append(hw)
            return True
        self._patch(s, monkeypatch, hw_available=hw_available)

        assert s.start("rtmp://x") is True
        assert attempted == [HWAccel.CPU]  # single CPU, no fallback duplicate

    def test_specific_nvenc_request_appends_cpu_fallback(self, monkeypatch):
        """Same guarantee for NVENC: an unavailable NVENC falls back to CPU."""
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.NVENC))
        attempted = []

        def hw_available(hw):
            attempted.append(hw)
            return hw == HWAccel.CPU
        self._patch(s, monkeypatch, hw_available=hw_available)

        assert s.start("rtmp://x") is True
        assert attempted == [HWAccel.NVENC, HWAccel.CPU]
        assert s._active_hw_accel == "cpu"

    def test_specific_qsv_and_cpu_both_fail_returns_false(self, monkeypatch):
        """When even the CPU last resort fails, start() still returns False."""
        s = VideoStreamer(StreamConfig(width=4, height=4, hw_accel=HWAccel.QSV))
        self._patch(s, monkeypatch, hw_available=lambda hw: False)
        assert s.start("rtmp://x") is False
        assert s.is_running is False


# ---------------------------------------------------------------------------
# _write_loop format-conversion and error-handling branches
# ---------------------------------------------------------------------------


class TestWriteLoopFormatConversion:
    def test_grayscale_frame_is_converted_to_bgr(self):
        s, proc = _fake_running_streamer()
        try:
            gray = np.zeros((240, 320), dtype=np.uint8)  # 2D -- grayscale
            s.stream(gray)
            deadline = time.time() + 2.0
            while time.time() < deadline and not proc.stdin.write.called:
                time.sleep(0.01)
            assert proc.stdin.write.called
            written = proc.stdin.write.call_args[0][0]
            assert len(written) == 320 * 240 * 3  # converted to 3-channel BGR
        finally:
            s._is_running = False
            s._stop_writer()

    def test_bgra_frame_is_trimmed_to_bgr(self):
        s, proc = _fake_running_streamer()
        try:
            bgra = np.zeros((240, 320, 4), dtype=np.uint8)  # 4-channel BGRA
            s.stream(bgra)
            deadline = time.time() + 2.0
            while time.time() < deadline and not proc.stdin.write.called:
                time.sleep(0.01)
            assert proc.stdin.write.called
            written = proc.stdin.write.call_args[0][0]
            assert len(written) == 320 * 240 * 3  # alpha channel dropped
        finally:
            s._is_running = False
            s._stop_writer()

    def test_generic_write_exception_increments_dropped_and_keeps_running(self):
        """A non-pipe exception during write must be swallowed (dropped_frames
        incremented) rather than tearing down the writer loop."""
        s, proc = _fake_running_streamer()
        proc.stdin.write.side_effect = ValueError("unexpected encoder error")
        try:
            s.stream(make_frame(320, 240))
            deadline = time.time() + 2.0
            while time.time() < deadline and s.stats.dropped_frames == 0:
                time.sleep(0.01)
            assert s.stats.dropped_frames >= 1
            assert s._writer_thread is not None and s._writer_thread.is_alive()
        finally:
            s._is_running = False
            s._stop_writer()


# ---------------------------------------------------------------------------
# _reconnect() edge cases not covered by the broken-pipe integration tests
# ---------------------------------------------------------------------------


class TestReconnectEdgeCases:
    def test_abandoned_immediately_when_writer_not_running(self):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._writer_running = False
        s._active_hw_accel = HWAccel.CPU.value
        assert s._reconnect() is False

    def test_abandoned_after_wait_when_writer_stopped_meanwhile(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._writer_running = True
        s._active_hw_accel = HWAccel.CPU.value
        s.RECONNECT_MAX_ATTEMPTS = 3

        def fake_wait(timeout):
            # Simulate the writer being stopped by something other than
            # stop()'s shutdown_event (e.g. a concurrent state change) while
            # the backoff sleep was in progress.
            s._writer_running = False
            return False  # event was NOT set -- distinct from the stop() path
        monkeypatch.setattr(s._shutdown_event, 'wait', fake_wait)

        assert s._reconnect() is False

    def test_exception_during_attempt_is_caught_and_attempt_exhausts(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4))
        s._writer_running = True
        s._active_hw_accel = HWAccel.CPU.value
        s._rtmp_url = "rtmp://x"
        s._rtmp_url_sub = None
        s.RECONNECT_MAX_ATTEMPTS = 1
        s.RECONNECT_INITIAL_BACKOFF = 0.01
        s.RECONNECT_MAX_BACKOFF = 0.01

        monkeypatch.setattr(s, '_start_ffmpeg', MagicMock(side_effect=RuntimeError("spawn failed")))
        monkeypatch.setattr(s, '_cleanup_ffmpeg', MagicMock())

        assert s._reconnect() is False
        assert s.reconnect_count == 0


# ---------------------------------------------------------------------------
# stderr-reader thread (inside _start_ffmpeg's nested read_stderr())
# ---------------------------------------------------------------------------


class TestStderrReaderThread:
    def test_appends_lines_and_trims_buffer_over_100(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=10))
        lines = [f"line{i}\n".encode() for i in range(105)] + [b'']
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline.side_effect = lines
        monkeypatch.setattr('ipycam.streamer.subprocess.Popen', lambda *a, **k: proc)

        s._start_ffmpeg("rtmp://127.0.0.1/test", None, HWAccel.CPU)
        thread = s._stderr_thread
        thread.join(timeout=2.0)

        assert not thread.is_alive()
        assert len(s._ffmpeg_stderr_buffer) == 100  # trimmed to the 100-line cap

    def test_reader_exception_is_swallowed_and_thread_exits(self, monkeypatch):
        s = VideoStreamer(StreamConfig(width=4, height=4, fps=10))
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline.side_effect = RuntimeError("stream closed unexpectedly")
        monkeypatch.setattr('ipycam.streamer.subprocess.Popen', lambda *a, **k: proc)

        s._start_ffmpeg("rtmp://127.0.0.1/test", None, HWAccel.CPU)
        thread = s._stderr_thread
        thread.join(timeout=2.0)

        assert not thread.is_alive()  # exception caught inside read_stderr, thread exits cleanly
