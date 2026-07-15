# Contributing to IPyCam

Thanks for your interest in improving IPyCam. This is a small, pure-Python
project, so the bar is mostly "keep it simple, keep it tested."

## Development setup

Clone the repo and install it in editable mode with the dev tooling. Include
the `webrtc` extra too if you're touching anything under `ipycam/webrtc.py`
or the native WebRTC signaling path:

```bash
git clone https://github.com/olkham/IPyCam.git
cd IPyCam
pip install -e ".[dev,webrtc]"
```

If you don't need WebRTC, `pip install -e ".[dev]"` is enough and avoids
pulling in `aiortc`/`aiohttp` (which have native build dependencies).

## Running the tests

```bash
# Run the full suite
pytest

# Verbose output
pytest -v

# With coverage
pytest --cov=ipycam --cov-report=term-missing

# A single file
pytest tests/test_config.py
```

The suite is hermetic: tests that depend on `aiortc` are skipped
automatically when the `webrtc` extra isn't installed, so `pip install -e
".[dev]"` alone is sufficient for most changes.

## Linting and type checking

```bash
ruff check ipycam
mypy ipycam
```

Both run in CI (`.github/workflows/ci.yml`) on every push to `main` and on
every pull request, alongside the test matrix (ubuntu-latest and
windows-latest, Python 3.8 through 3.12, plus a dedicated job that installs
the `webrtc` extra). Ruff and mypy are currently **non-blocking** in CI (the
codebase predates both and hasn't been fully brought in line yet) -- please
still try to keep new code clean, but a pre-existing warning elsewhere is not
your problem to fix in an unrelated PR.

## Branch / PR basics

- Branch off `main`; keep PRs focused on one topic where practical.
- Add or update tests for behavior you change -- this project treats the
  test suite as the source of truth for "does it still work."
- Make sure `pytest` and `ruff check ipycam` are clean (or at least not
  newly broken by your change) before opening a PR.
- Describe *why* a change is needed in the PR description, not just what
  changed -- it's much easier to review.

## Architecture notes for contributors

The core of IPyCam is a single-writer, multi-reader frame pipeline. Every
frame handed to `IPCamera.stream(frame)` goes through, in order:

1. **Capture** -- the caller reads a frame from wherever (webcam, file, RTSP,
   generated) and calls `camera.stream(frame)`.
2. **Transforms** -- digital PTZ (`ptz.apply_ptz`), then display transforms
   (`flip`/`mirror`/`rotation`), then the timestamp overlay, are applied in
   that order on the capture thread.
3. **Outbound copy** -- exactly one defensive copy is made per frame
   (`outbound = frame.copy()`). This copy is treated as **immutable by
   contract**: no downstream consumer may mutate it in place, so everything
   below can share the same object by reference instead of re-copying.
4. **Drop-queue fan-out to workers** -- the capture thread hands `outbound`
   to each active output via a cheap, non-blocking enqueue and moves on
   immediately:
   - MJPEG: enqueued into an encode worker, then fanned out to per-client
     writer threads (a slow client stalls only itself).
   - Native RTSP: enqueued into a bounded `FrameQueue`
     (`ipycam/framequeue.py`) drained by a fan-out worker that does the
     sub-stream resize and per-stream writes.
   - Recorder (`ipycam/recorder.py`): enqueued into its own bounded queue
     only while recording or maintaining the pre-record ring buffer; a
     dedicated worker thread does all disk I/O.
   - go2rtc/native WebRTC: similarly enqueued/stored for their own
     workers/encoders.

The `FrameQueue` primitive (`ipycam/framequeue.py`) is what makes this safe:
`put()` **never blocks** -- if the queue is full it drops the oldest buffered
frame and keeps going. This means a slow encoder, a stalled client socket, or
a slow disk can only ever cause *frame drops* in its own queue; it can never
apply back-pressure to the capture thread or to any other output. If you add
a new consumer to the pipeline, follow the same pattern: enqueue and return
immediately from `stream()`, do the actual work (encoding, I/O) on a
dedicated worker thread, and never mutate the `outbound` frame in place
(copy it first if you must).

## License

By contributing, you agree that your contributions will be licensed under
the project's MIT License.
