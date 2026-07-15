"""
Tests for WSDiscoveryServer (WS-Discovery) lifecycle, probe filtering, and
malformed-datagram handling.

All sockets are mocked - these tests never open a real socket, join a
multicast group, or touch the network.
"""

import socket
from unittest.mock import MagicMock, patch

import pytest

from ipycam.discovery import WSDiscoveryServer


# A genuine WS-Discovery Probe request.
PROBE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
    '<soap:Header>'
    '<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>'
    '<wsa:MessageID>urn:uuid:aaaaaaaa-0000-0000-0000-000000000001</wsa:MessageID>'
    '</soap:Header>'
    '<soap:Body><d:Probe/></soap:Body>'
    '</soap:Envelope>'
)

# A ProbeMatch *response* as broadcast by some other camera on the segment.
# It contains the substring "Probe" and must NOT be treated as a request.
PROBE_MATCH_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
    '<soap:Header>'
    '<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatch</wsa:Action>'
    '<wsa:MessageID>urn:uuid:bbbbbbbb-0000-0000-0000-000000000002</wsa:MessageID>'
    '</soap:Header>'
    '<soap:Body><d:ProbeMatch>other device</d:ProbeMatch></soap:Body>'
    '</soap:Envelope>'
)


def make_mock_onvif():
    """MagicMock standing in for ONVIFService, exposing only what
    WSDiscoveryServer touches."""
    onvif = MagicMock()
    onvif.config.local_ip = "192.168.1.50"
    onvif.config.name = "Test Camera"
    onvif.config.onvif_url = "http://192.168.1.50:8080/onvif/device_service"
    onvif.device_uuid = "urn:uuid:12345678-1234-1234-1234-123456789abc"
    onvif.create_probe_match.return_value = "<d:ProbeMatch>response</d:ProbeMatch>"
    return onvif


@pytest.fixture
def mock_sock():
    """A MagicMock standing in for the UDP socket. Defaults to raising
    socket.timeout on every recvfrom() call so tests that don't care about
    the run loop (e.g. init/stop tests) never block or loop forever."""
    sock = MagicMock()
    sock.recvfrom.side_effect = socket.timeout()
    return sock


@pytest.fixture
def server(mock_sock):
    """A WSDiscoveryServer built with socket.socket() patched out so no real
    socket or multicast group is ever created."""
    with patch('ipycam.discovery.socket.socket', return_value=mock_sock):
        srv = WSDiscoveryServer(make_mock_onvif())
    return srv


class TestInitialization:
    """The constructor should configure the (mocked) socket but never touch
    the real network."""

    def test_socket_is_bound_and_given_a_timeout(self, server, mock_sock):
        mock_sock.bind.assert_called_once_with(('', WSDiscoveryServer.MULTICAST_PORT))
        mock_sock.settimeout.assert_called_once_with(1.0)

    def test_sock_attribute_is_the_mock(self, server, mock_sock):
        assert server.sock is mock_sock


class TestStopLifecycle:
    """stop() must close the socket, be idempotent, and cause run() to
    return promptly instead of blocking forever."""

    def test_stop_closes_socket_and_clears_reference(self, server, mock_sock):
        server.stop()
        mock_sock.close.assert_called_once()
        assert server.sock is None

    def test_stop_is_idempotent_no_double_close(self, server, mock_sock):
        server.stop()
        server.stop()
        mock_sock.close.assert_called_once()

    def test_run_loop_exits_once_running_is_cleared(self, server, mock_sock):
        """Drive the loop with a socket whose recvfrom() always times out
        (as it would while idle); the loop must still exit as soon as
        `running` is flipped, without needing to touch the socket again."""
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] >= 3:
                server.running = False
            raise socket.timeout()

        mock_sock.recvfrom.side_effect = fake_recvfrom

        server.run()  # must return on its own; a hang would fail via timeout

        assert calls["n"] >= 3

    def test_stop_without_ever_calling_run_does_not_raise(self, server):
        # stop() should be safe to call even if the thread's run() never
        # started (e.g. construction failed elsewhere in the app).
        server.stop()


class TestIsProbeRequest:
    """Unit-level checks of the narrowed probe-matching logic."""

    def test_real_probe_is_detected(self, server):
        assert server._is_probe_request(PROBE_XML) is True

    def test_probe_match_response_is_not_detected(self, server):
        assert server._is_probe_request(PROBE_MATCH_XML) is False

    def test_unrelated_payload_is_not_detected(self, server):
        assert server._is_probe_request("just some noise, not xml at all") is False

    def test_probe_element_without_action_uri_is_detected(self, server):
        msg = '<soap:Body><wsd:Probe xmlns:wsd="urn:example"/></soap:Body>'
        assert server._is_probe_request(msg) is True

    def test_payload_merely_containing_the_word_probe_is_not_detected(self, server):
        # No "<Probe" element and no Probe action URI - just the substring.
        assert server._is_probe_request("Probe this string contains the word") is False


class TestRunLoopProbeHandling:
    """End-to-end (but hermetic) check that only a genuine Probe elicits a
    ProbeMatch reply."""

    def test_probe_match_ignored_but_probe_answered(self, server, mock_sock):
        addr = ('192.168.1.99', 51234)
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] == 1:
                return PROBE_MATCH_XML.encode('utf-8'), addr
            if calls["n"] == 2:
                return PROBE_XML.encode('utf-8'), addr
            server.running = False
            raise socket.timeout()

        mock_sock.recvfrom.side_effect = fake_recvfrom

        server.run()

        # Only replies sent back to `addr` count as probe responses (the
        # startup Hello announcement also calls sendto, to the multicast
        # group address, and must not be confused with a probe reply).
        replies_to_requester = [
            c for c in mock_sock.sendto.call_args_list if c.args[1] == addr
        ]
        assert len(replies_to_requester) == 1
        assert replies_to_requester[0].args[0] == b"<d:ProbeMatch>response</d:ProbeMatch>"


class TestMalformedDatagram:
    """A non-UTF-8 / spoofed datagram must never escape the loop as an
    exception."""

    def test_non_utf8_bytes_do_not_raise(self, server, mock_sock):
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"\xff\xfe\x00garbage-not-utf8\xd8", ('10.0.0.5', 12345)
            server.running = False
            raise socket.timeout()

        mock_sock.recvfrom.side_effect = fake_recvfrom

        # Must complete without raising UnicodeDecodeError (or anything else).
        server.run()
        assert calls["n"] >= 2

    def test_malformed_datagram_does_not_trigger_a_reply(self, server, mock_sock):
        addr = ('10.0.0.5', 12345)
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"\xff\xfe\x00garbage-not-utf8\xd8", addr
            server.running = False
            raise socket.timeout()

        mock_sock.recvfrom.side_effect = fake_recvfrom
        server.run()

        replies_to_sender = [
            c for c in mock_sock.sendto.call_args_list if c.args[1] == addr
        ]
        assert len(replies_to_sender) == 0


class TestHelloByeAnnouncements:
    """Optional passive-discovery feature: announce Hello on start, Bye on
    stop, reusing the same endpoint/scope values as ProbeMatch."""

    def test_run_sends_hello_to_multicast_group_on_start(self, server, mock_sock):
        def fake_recvfrom(bufsize):
            server.running = False
            raise socket.timeout()

        mock_sock.recvfrom.side_effect = fake_recvfrom

        server.run()

        hello_calls = [
            c for c in mock_sock.sendto.call_args_list if b'Hello' in c.args[0]
        ]
        assert len(hello_calls) == 1
        assert hello_calls[0].args[1] == (
            WSDiscoveryServer.MULTICAST_ADDR, WSDiscoveryServer.MULTICAST_PORT
        )

    def test_stop_sends_bye_to_multicast_group(self, server, mock_sock):
        server.stop()

        bye_calls = [
            c for c in mock_sock.sendto.call_args_list if b'Bye' in c.args[0]
        ]
        assert len(bye_calls) == 1
        assert bye_calls[0].args[1] == (
            WSDiscoveryServer.MULTICAST_ADDR, WSDiscoveryServer.MULTICAST_PORT
        )

    def test_stop_does_not_send_bye_twice(self, server, mock_sock):
        server.stop()
        server.stop()

        bye_calls = [
            c for c in mock_sock.sendto.call_args_list if b'Bye' in c.args[0]
        ]
        assert len(bye_calls) == 1


# ---------------------------------------------------------------------------
# Step 3.5 additions: the IP_ADD_MEMBERSHIP fallback in __init__, best-effort
# announcement failures, the generic (non-timeout) recv error path, an
# exception raised while handling a genuine probe, and stop()'s swallowed
# socket.close() error.
# ---------------------------------------------------------------------------


class TestMulticastJoinFallback:
    def test_falls_back_to_inaddr_any_when_first_setsockopt_fails(self):
        """The primary IP_ADD_MEMBERSHIP (bound to the configured local_ip)
        can fail (e.g. no route for that interface); __init__ must retry with
        INADDR_ANY instead of propagating the exception."""
        sock = MagicMock()
        sock.recvfrom.side_effect = socket.timeout()
        # Call order in __init__: (1) SO_REUSEADDR -- succeeds, (2) the
        # primary IP_ADD_MEMBERSHIP bound to local_ip -- fails, (3) the
        # INADDR_ANY fallback -- succeeds.
        sock.setsockopt.side_effect = [None, OSError("no such interface"), None]

        with patch('ipycam.discovery.socket.socket', return_value=sock):
            srv = WSDiscoveryServer(make_mock_onvif())

        assert srv.sock is sock
        assert sock.setsockopt.call_count == 3


class TestAnnouncementFailureIsBestEffort:
    def test_send_announcement_exception_does_not_propagate(self, server, mock_sock):
        mock_sock.sendto.side_effect = OSError("network unreachable")
        server._send_announcement('Hello')  # must not raise

    def test_send_announcement_noop_when_socket_already_cleared(self, server):
        server.sock = None
        server._send_announcement('Hello')  # must not raise (early return)


class TestRunLoopGenericSocketError:
    def test_generic_exception_while_running_is_logged_and_breaks(self, server, mock_sock):
        mock_sock.recvfrom.side_effect = OSError("socket unavailable")
        server.run()  # must return, not raise or loop forever

    def test_exception_while_not_running_is_silent(self, server, mock_sock):
        server.running = False
        mock_sock.recvfrom.side_effect = OSError("socket unavailable")
        server.run()  # must return without logging/raising


class TestProbeHandlingException:
    def test_exception_while_building_probe_match_is_swallowed(self, server, mock_sock):
        server.onvif.create_probe_match.side_effect = RuntimeError("boom")
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] == 1:
                return PROBE_XML.encode('utf-8'), ('10.0.0.5', 1)
            server.running = False
            raise socket.timeout()
        mock_sock.recvfrom.side_effect = fake_recvfrom

        server.run()  # must not raise despite create_probe_match blowing up
        assert calls["n"] >= 2


class TestStopSocketCloseException:
    def test_close_exception_is_swallowed(self, server, mock_sock):
        mock_sock.close.side_effect = OSError("already closed")
        server.stop()  # must not raise
        assert server.sock is None
