"""
Tests for the native WebRTC path (aiortc).

Focus of step 2.2: the redundant per-frame copies in the WebRTC path were
removed. These tests pin down the new contract:

  * SharedFrameBuffer.update stores a REFERENCE (no ~6 MB/frame copy), even
    when nothing is watching.
  * A frame delivered through CameraVideoTrack.recv() is correct (BGR->RGB)
    and fully isolated from later mutation of the source array -- isolation is
    provided by av.VideoFrame.from_ndarray copying pixels into its own buffer,
    NOT by a defensive copy in the buffer.
  * End-to-end, IPCamera.stream() only feeds WebRTC when a peer is connected
    (the single authoritative gate), and the copy it makes isolates a peer from
    later in-place mutation of the caller's input frame.

The whole module is skipped when aiortc/av are not installed.
"""

import asyncio

import numpy as np
import pytest

from ipycam import webrtc
from ipycam.camera import IPCamera
from ipycam.config import CameraConfig

pytestmark = pytest.mark.skipif(
    not webrtc.is_webrtc_available(),
    reason="aiortc/av not installed",
)


def make_camera():
    """Unstarted IPCamera with PTZ + timestamp disabled.

    That makes stream() a pass-through so `outbound` is exactly a copy of the
    input frame, keeping the isolation assertions deterministic. The PTZ thread
    started in __init__ is stopped first.
    """
    camera = IPCamera(CameraConfig(show_timestamp=False))
    camera.ptz.stop()
    camera.ptz = None
    return camera


# ---------------------------------------------------------------------------
# SharedFrameBuffer: no-copy store
# ---------------------------------------------------------------------------


def test_update_stores_reference_not_copy():
    """update() must store the SAME object (no per-frame memcpy)."""
    buf = webrtc.SharedFrameBuffer()
    frame = np.full((120, 160, 3), 7, dtype=np.uint8)

    buf.update(frame)

    # Object identity proves no copy was made on the way in or out.
    assert buf._frame is frame
    assert buf.get() is frame


def test_get_returns_none_before_any_frame():
    """No frame stored yet -> get() returns None (not an empty array)."""
    buf = webrtc.SharedFrameBuffer()
    assert buf.get() is None


class _SpyArray(np.ndarray):
    """ndarray subclass whose .copy() bumps a shared counter, so a test can
    assert that no defensive copy was taken."""

    copies = 0

    def copy(self, *args, **kwargs):  # noqa: D102
        type(self).copies += 1
        return np.asarray(self).copy(*args, **kwargs)


def test_update_with_zero_consumers_does_not_copy():
    """With nothing watching, update() still performs zero copies.

    Uses a spying ndarray subclass: if update() ever calls frame.copy() the
    counter trips. (The buffer has no consumers here; the real "is anyone
    watching?" gate lives in IPCamera.stream.)
    """
    buf = webrtc.SharedFrameBuffer()
    _SpyArray.copies = 0
    frame = np.full((64, 64, 3), 3, dtype=np.uint8).view(_SpyArray)

    buf.update(frame)

    assert _SpyArray.copies == 0
    assert buf.get() is frame


# ---------------------------------------------------------------------------
# CameraVideoTrack.recv(): correctness + isolation via from_ndarray
# ---------------------------------------------------------------------------


def _recv_once(track):
    return asyncio.run(track.recv())


def test_recv_converts_bgr_to_rgb_correctly():
    """recv() must hand av an RGB frame that matches the BGR source channels."""
    buf = webrtc.SharedFrameBuffer()
    # Distinct per-channel BGR values so the channel swap is observable.
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    frame[:, :, 0] = 10  # B
    frame[:, :, 1] = 20  # G
    frame[:, :, 2] = 30  # R
    buf.update(frame)

    track = webrtc.CameraVideoTrack(buf, fps=10, width=16, height=16)
    out = _recv_once(track).to_ndarray(format="rgb24")

    # RGB order: R,G,B == 30,20,10
    assert out[0, 0].tolist() == [30, 20, 10]


def test_recv_frame_isolated_from_source_mutation():
    """Mutating the source AFTER recv() must not change the delivered frame.

    This is the guarantee that lets us drop the defensive copy: av.VideoFrame
    .from_ndarray copies the pixels, so the encoded frame is independent of the
    shared buffer's array.
    """
    buf = webrtc.SharedFrameBuffer()
    frame = np.full((16, 16, 3), 10, dtype=np.uint8)
    buf.update(frame)

    track = webrtc.CameraVideoTrack(buf, fps=10, width=16, height=16)
    vf = _recv_once(track)
    before = vf.to_ndarray(format="rgb24").copy()

    # Mutate the exact array the buffer still references.
    frame[:] = 200

    after = vf.to_ndarray(format="rgb24")
    assert np.array_equal(before, after)
    assert np.all(after == 10)  # original pixels, not the mutated 200


# ---------------------------------------------------------------------------
# End-to-end through IPCamera.stream()
# ---------------------------------------------------------------------------


def _attach_running_webrtc(camera, *, with_peer, width=160, height=120):
    streamer = webrtc.NativeWebRTCStreamer(fps=30, width=width, height=height)
    # Flip the running flag directly so we exercise stream_frame WITHOUT
    # spinning up the asyncio event-loop thread.
    streamer._is_running = True
    if with_peer:
        # connection_count == len(_peer_connections); a sentinel is enough to
        # trip the camera's authoritative gate.
        streamer._peer_connections.add(object())
    camera.webrtc_streamer = streamer
    return streamer


def test_camera_skips_webrtc_when_no_peers():
    """The single authoritative gate: no peer connected -> WebRTC never touched."""
    camera = make_camera()
    streamer = _attach_running_webrtc(camera, with_peer=False)

    camera.stream(np.full((120, 160, 3), 5, dtype=np.uint8))

    # update() was never called, so the shared buffer stayed empty.
    assert streamer._frame_buffer._frame_count == 0
    assert streamer._frame_buffer.get() is None


def test_camera_feeds_webrtc_when_peer_present_and_isolates_input():
    """With a peer, stream() feeds WebRTC; mutating the input can't corrupt it.

    Guards the full contract end-to-end: stream() copies the caller's frame into
    the immutable `outbound` buffer, WebRTC stores a reference to that, and the
    encoder isolates the peer via from_ndarray.
    """
    camera = make_camera()
    streamer = _attach_running_webrtc(camera, with_peer=True, width=160, height=120)

    f1 = np.full((120, 160, 3), 10, dtype=np.uint8)
    camera.stream(f1)

    assert streamer._frame_buffer._frame_count == 1

    track = webrtc.CameraVideoTrack(streamer._frame_buffer, fps=30, width=160, height=120)
    vf = _recv_once(track)
    before = vf.to_ndarray(format="rgb24").copy()

    # Simulate the capture loop reusing/mutating its frame buffer in place.
    f1[:] = 99

    after = vf.to_ndarray(format="rgb24")
    assert np.array_equal(before, after)
    assert np.all(after == 10)  # peer still sees the original frame, not 99
