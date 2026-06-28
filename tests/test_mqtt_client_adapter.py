"""Tests for the MQTT client adapter wrapper and callback handling."""

import time

import cpynodus_ii.core.mqtt_client as mqtt_client_module
from cpynodus_ii.core import (
    build_mqtt_client_adapter,
    close_mqtt_client,
    connect_mqtt_client,
    disconnect_mqtt_client,
    poll_mqtt_client,
    preflight_mqtt_broker,
    preflight_mqtt_broker_connect,
    preflight_mqtt_broker_tcp,
    sync_transport_to_client,
)
from cpynodus_ii.core.config import (
    DetectedSensor,
    MQTTConfig,
    RuntimeConfig,
    SwitchChannelConfig,
    SwitchConfig,
)
from cpynodus_ii.core.mqtt import MQTTTransport


def _run_direct_tests():
    tests = (
        test_build_mqtt_client_adapter_uses_runtime_target_and_credentials,
        test_build_mqtt_client_adapter_drops_optional_connect_kwargs_when_unsupported,
        test_build_mqtt_client_adapter_sets_runtime_client_id,
        test_connect_sync_poll_and_disconnect_flow,
        test_sync_transport_to_client_records_last_publish_state,
        test_sync_transport_to_client_records_startup_publish_state,
        test_sync_transport_to_client_collects_after_retained_device_meta,
        test_sync_transport_to_client_publishes_qos0_packet_on_socket,
        test_sync_transport_to_client_chunks_large_qos0_publish_on_socket,
        test_sync_transport_to_client_keeps_startup_meta_qos0_by_default,
        test_sync_transport_to_client_can_verify_large_raw_publish_when_debug_enabled,
        test_sync_transport_to_client_keeps_large_non_startup_publish_qos0,
        test_sync_transport_to_client_ignores_puback_timeout_by_default,
        test_sync_transport_to_client_reports_optional_puback_timeout_when_enabled,
        test_sync_transport_to_client_retries_partial_qos0_socket_sends,
        test_sync_transport_to_client_chunks_send_nbytes_only_socket,
        test_sync_transport_to_client_subscribes_qos0_packet_on_socket,
        test_sync_transport_to_client_reports_raw_subscribe_failure_diagnostics,
        test_poll_mqtt_client_receives_qos0_publish_on_socket_without_loop,
        test_poll_mqtt_client_receives_qos0_publish_on_recv_into_only_socket,
        test_poll_mqtt_client_treats_socket_timeout_as_empty_poll,
        test_poll_mqtt_client_treats_etimedout_as_empty_poll,
        test_poll_mqtt_client_reports_raw_socket_pystack_source,
        test_poll_mqtt_client_reports_remaining_length_pystack_stage,
        test_poll_mqtt_client_reports_minimqtt_loop_pystack_source,
        test_poll_mqtt_client_records_last_loop_diagnostics,
        test_build_mqtt_client_adapter_is_unavailable_without_socket_pool,
        test_connect_mqtt_client_uses_broker_ip_when_hostname_is_configured,
        test_connect_mqtt_client_falls_back_to_broker_ip_alt,
        test_connect_mqtt_client_does_not_resolve_hostname_when_broker_ip_exists,
        test_build_mqtt_client_adapter_requires_ip_target,
        test_preflight_mqtt_broker_can_resolve_explicit_hostname,
        test_preflight_mqtt_broker_tcp_uses_adapter_socket_pool,
        test_preflight_mqtt_broker_tcp_reports_socket_connect_errors,
        test_preflight_mqtt_broker_connect_reads_connack_and_disconnects,
        test_preflight_mqtt_broker_connect_reports_connack_refusal,
        test_connect_mqtt_client_uses_raw_socket_when_available,
        test_connect_mqtt_client_reports_raw_connack_refusal,
        test_poll_mqtt_client_uses_timeout_compatible_with_socket_timeout,
        test_sync_transport_to_client_marks_transport_disconnected_on_publish_oserror,
        test_sync_transport_to_client_subscribes_before_low_priority_publish,
        test_sync_transport_to_client_publishes_priority_status_before_subscribe,
        test_sync_transport_to_client_defers_subscribe_until_clean_poll,
        test_sync_transport_to_client_disconnects_after_slow_publish,
        test_sync_transport_to_client_disconnects_on_subscribe_failure,
        test_poll_mqtt_client_marks_transport_disconnected_on_oserror,
        test_close_mqtt_client_disconnects_without_shutdown_publish,
        test_close_mqtt_client_keeps_pending_publish_queue,
        test_close_mqtt_client_force_closes_socket_when_client_is_not_connected,
        test_disconnect_mqtt_client_swallow_shutdown_publish_oserror,
        test_disconnect_mqtt_client_reports_disconnect_oserror,
        test_poll_mqtt_client_tolerates_disconnect_callback_with_two_args,
        test_poll_mqtt_client_does_not_retry_internal_typeerror,
        test_poll_mqtt_client_marks_transport_disconnected_on_callback_arity_typeerror,
        test_poll_mqtt_client_adapts_socket_recv_into_without_nbytes,
        test_poll_mqtt_client_adapts_connected_minimqtt_socket_recv_into,
        test_poll_mqtt_client_falls_back_to_recv_when_recv_into_arity_persists,
        test_poll_mqtt_client_uses_raw_socket_when_wrapped_recv_still_has_arity_error,
        test_poll_mqtt_client_uses_raw_socket_after_exact_wrapped_arity_error,
        test_poll_mqtt_client_adapts_connected_minimqtt_socket_send_without_nbytes,
        test_poll_mqtt_client_labels_minimqtt_wrapped_socket_typeerror,
        test_poll_mqtt_client_labels_wrapped_socket_typeerror_client_state,
        test_poll_mqtt_client_includes_last_publish_diagnostics_on_wrapped_socket_error,
        test_poll_mqtt_client_includes_last_loop_diagnostics_on_wrapped_socket_error,
        test_poll_mqtt_client_marks_disconnected_when_client_reports_disconnected,
    )
    for test in tests:
        test()
    print("test_mqtt_client_adapter: {} tests passed".format(len(tests)))


class _FakeMQTTClient:
    fail_connect_for = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connected = False
        self.disconnected = False
        self.subscribed = []
        self.published = []
        self.pending_incoming = []
        self.on_message = None

    def connect(self):
        if self.kwargs.get("broker") in self.fail_connect_for:
            raise RuntimeError(
                "connect failed for {}".format(self.kwargs.get("broker"))
            )
        self.connected = True

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def loop(self, timeout=0.0):
        while self.pending_incoming:
            topic, payload = self.pending_incoming.pop(0)
            if callable(self.on_message):
                self.on_message(self, topic, payload)

    def disconnect(self):
        self.disconnected = True


class _SocketTrackingConnectionManager:
    def __init__(self):
        self.closed = []

    def close_socket(self, sock):
        self.closed.append(sock)
        close = getattr(sock, "close", None)
        if callable(close):
            close()


class _TrackingSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _UnconnectedSocketMQTTClient(_FakeMQTTClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sock = _TrackingSocket()
        self._is_connected = False
        self._connection_manager = _SocketTrackingConnectionManager()

    def disconnect(self):
        raise RuntimeError("MiniMQTT is not connected")


class _RejectOptionalMQTTKwargsClient(_FakeMQTTClient):
    def __init__(self, **kwargs):
        if "socket_timeout" in kwargs or "connect_retries" in kwargs:
            raise TypeError("unexpected keyword argument 'socket_timeout'")
        super().__init__(**kwargs)


class _TimeoutSensitiveMQTTClient(_FakeMQTTClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.socket_timeout = kwargs.get("socket_timeout", 1)
        self.loop_timeouts = []

    def loop(self, timeout=0.0):
        self.loop_timeouts.append(timeout)
        if timeout < self.socket_timeout:
            raise ValueError(
                "loop timeout ({}) must be >= socket timeout ({})".format(
                    timeout, self.socket_timeout
                )
            )
        super().loop(timeout=timeout)


class _PublishFailMQTTClient(_FakeMQTTClient):
    def publish(self, topic, payload, retain=False):
        raise OSError(9)


class _PublishFailSecondMQTTClient(_FakeMQTTClient):
    def publish(self, topic, payload, retain=False):
        if self.published:
            raise OSError(5)
        super().publish(topic, payload, retain=retain)


class _SubscribeFailMQTTClient(_FakeMQTTClient):
    def subscribe(self, topic):
        raise RuntimeError("No data received from broker for 10 seconds.")


class _SubscribeFailSecondMQTTClient(_FakeMQTTClient):
    def subscribe(self, topic):
        if self.subscribed:
            raise RuntimeError("No data received from broker for 10 seconds.")
        super().subscribe(topic)


class _SlowPublishMQTTClient(_FakeMQTTClient):
    def publish(self, topic, payload, retain=False):
        time.sleep(0.01)
        super().publish(topic, payload, retain=retain)


class _SettleBeforeSubscribeFailMQTTClient(_SubscribeFailMQTTClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.loop_count = 0

    def loop(self, timeout=0.0):
        self.loop_count += 1
        super().loop(timeout=timeout)


class _PollFailMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        raise OSError(9)


class _LoopPystackFailMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        raise RuntimeError("pystack exhausted")


class _DisconnectFailMQTTClient(_FakeMQTTClient):
    def disconnect(self):
        raise OSError(9)


class _CallbackDisconnectMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        callback = getattr(self, "on_disconnect", None)
        if callable(callback):
            callback(self, 7)


class _TwoArgMessageCallbackMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        while self.pending_incoming:
            topic, payload = self.pending_incoming.pop(0)
            if callable(self.on_message):
                self.on_message(topic, payload)


class _InternalTypeErrorMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        raise TypeError("simulated internal mqtt failure")


class _CallbackArityFailMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        raise TypeError("missing 1 required positional argument")


class _MiniMQTTWrappedSocketFailMQTTClient(_FakeMQTTClient):
    def loop(self, timeout=0.0):
        raise TypeError("function takes 3 positional arguments but 2 were given")


class _MiniMQTTConnectedWrappedSocketFailMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _RecvIntoNeedsNbytesSocket()
        self._backwards_compatible_sock = True

    def loop(self, timeout=0.0):
        raise TypeError("function takes 3 positional arguments but 2 were given")


class _MiniMQTTConnectedWrappedSocketFailAfterCleanLoopMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _RecvIntoNeedsNbytesSocket()
        self._backwards_compatible_sock = True
        self.loop_count = 0

    def loop(self, timeout=0.0):
        self.loop_count += 1
        if self.loop_count > 1:
            raise TypeError("function takes 3 positional arguments but 2 were given")


class _MiniMQTTReportsDisconnectedClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self.connected = False
        self.loop_called = False

    def loop(self, timeout=0.0):
        self.loop_called = True


class _RecvIntoNeedsNbytesSocket:
    def __init__(self):
        self.recv_into_calls = []
        self.sent = bytearray()

    def recv_into(self, buffer, nbytes):
        self.recv_into_calls.append(nbytes)
        if nbytes:
            buffer[0] = 1
        return nbytes

    def send(self, buffer):
        self.sent.extend(buffer)
        return len(buffer)


class _SendNeedsNbytesSocket:
    def __init__(self):
        self.send_calls = []

    def send(self, buffer, nbytes):
        self.send_calls.append(nbytes)
        return nbytes


class _SendingSocket:
    def __init__(self):
        self.sent = bytearray()

    def send(self, buffer):
        self.sent.extend(buffer)
        return len(buffer)


class _NoneReturningSendingSocket:
    def __init__(self):
        self.chunks = []

    @property
    def sent(self):
        return b"".join(self.chunks)

    def send(self, buffer):
        self.chunks.append(bytes(buffer))
        return None


class _PartialSendingSocket:
    def __init__(self, max_send=257):
        self.max_send = max_send
        self.sent = bytearray()
        self.calls = []

    def send(self, buffer):
        count = min(self.max_send, len(buffer))
        self.calls.append(len(buffer))
        self.sent.extend(bytes(buffer[:count]))
        return count


class _SendNbytesOnlyChunkedSocket:
    def __init__(self):
        self.sent = bytearray()
        self.calls = []

    def send(self, buffer, nbytes):
        self.calls.append(nbytes)
        self.sent.extend(bytes(buffer[:nbytes]))
        return None


class _PubackSocket:
    def __init__(self, incoming=b"\x40\x02\x00\x01"):
        self.chunks = []
        self.incoming = bytearray(incoming)
        self.timeouts = []

    @property
    def sent(self):
        return b"".join(self.chunks)

    def send(self, buffer):
        self.chunks.append(bytes(buffer))
        return None

    def recv(self, nbytes):
        if not self.incoming:
            raise OSError(116, "ETIMEDOUT")
        count = min(int(nbytes or 0), len(self.incoming))
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class _SocketPublishMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SendingSocket()

    def publish(self, topic, payload, retain=False):
        raise AssertionError("MiniMQTT publish should not be called")


class _NoneReturningSocketPublishMQTTClient(_SocketPublishMQTTClient):
    def connect(self):
        _FakeMQTTClient.connect(self)
        self._sock = _NoneReturningSendingSocket()


class _PartialSocketPublishMQTTClient(_SocketPublishMQTTClient):
    def connect(self):
        _FakeMQTTClient.connect(self)
        self._sock = _PartialSendingSocket()


class _SendNbytesOnlySocketPublishMQTTClient(_SocketPublishMQTTClient):
    def connect(self):
        _FakeMQTTClient.connect(self)
        self._sock = _SendNbytesOnlyChunkedSocket()


class _PubackSocketPublishMQTTClient(_SocketPublishMQTTClient):
    def connect(self):
        _FakeMQTTClient.connect(self)
        self._sock = _PubackSocket()


class _PubackTimeoutSocketPublishMQTTClient(_SocketPublishMQTTClient):
    def connect(self):
        _FakeMQTTClient.connect(self)
        self._sock = _PubackSocket(incoming=b"")


class _PubackThenEbadfSocket(_PubackSocket):
    def recv(self, nbytes):
        if not self.incoming:
            raise OSError(9, "EBADF")
        return super().recv(nbytes)


class _PubackThenEbadfSocketPublishMQTTClient(_SocketPublishMQTTClient):
    def connect(self):
        _FakeMQTTClient.connect(self)
        self._sock = _PubackThenEbadfSocket()


class _SubscribeSocket:
    def __init__(self):
        self.sent = bytearray()
        self.incoming = bytearray(b"\x90\x03\x00\x01\x00")
        self.timeouts = []

    def send(self, buffer):
        self.sent.extend(buffer)
        return len(buffer)

    def recv(self, nbytes):
        count = min(int(nbytes or 0), len(self.incoming))
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class _SocketSubscribeMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SubscribeSocket()

    def subscribe(self, topic):
        raise AssertionError("MiniMQTT subscribe should not be called")


class _SubscribeTimeoutSocket:
    def __init__(self):
        self.sent = bytearray()
        self.timeouts = []

    def send(self, buffer):
        self.sent.extend(buffer)
        return len(buffer)

    def recv(self, nbytes):
        raise OSError(116, "ETIMEDOUT")

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class _SocketSubscribeTimeoutMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SubscribeTimeoutSocket()

    def subscribe(self, topic):
        raise AssertionError("MiniMQTT subscribe should not be called")


def _mqtt_publish_packet(topic, payload, qos=0, packet_id=1):
    topic_bytes = topic.encode("utf-8")
    payload_bytes = payload.encode("utf-8")
    qos_bytes = b""
    if qos:
        qos_bytes = bytes(((packet_id >> 8) & 0xFF, packet_id & 0xFF))
    remaining = 2 + len(topic_bytes) + len(qos_bytes) + len(payload_bytes)
    header = 0x30 | ((qos & 0x03) << 1)
    return (
        bytes((header, remaining, 0, len(topic_bytes)))
        + topic_bytes
        + qos_bytes
        + payload_bytes
    )


class _SocketPoll:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def recv(self, nbytes):
        if not self.incoming:
            raise OSError("timed out")
        count = min(int(nbytes or 0), len(self.incoming))
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data

    def send(self, buffer):
        self.sent.extend(buffer)
        return len(buffer)


class _SocketPollMQTTClient(_FakeMQTTClient):
    topic = "nodus/S1-x943fm/config/set"
    payload = "ON"

    def connect(self):
        super().connect()
        self._sock = _SocketPoll(_mqtt_publish_packet(self.topic, self.payload))

    def loop(self, timeout=0.0):
        raise AssertionError("MiniMQTT loop should not be called")


class _RecvIntoSocketPoll:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.recv_into_calls = []

    def recv_into(self, buffer, nbytes):
        if not self.incoming:
            raise OSError("timed out")
        count = min(int(nbytes or 0), len(buffer), len(self.incoming))
        index = 0
        while index < count:
            buffer[index] = self.incoming[index]
            index += 1
        del self.incoming[:count]
        self.recv_into_calls.append(nbytes)
        return count

    def send(self, buffer):
        self.sent.extend(buffer)
        return len(buffer)


class _RecvIntoSocketPollMQTTClient(_FakeMQTTClient):
    topic = "nodus/S1-x943fm/config/set"
    payload = "ON"

    def connect(self):
        super().connect()
        self._sock = _RecvIntoSocketPoll(
            _mqtt_publish_packet(self.topic, self.payload)
        )

    def loop(self, timeout=0.0):
        raise AssertionError("MiniMQTT loop should not be called")


class _SocketEmptyPollMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SocketPoll()

    def loop(self, timeout=0.0):
        raise AssertionError("MiniMQTT loop should not be called")


class _SocketErrnoTimeoutPoll(_SocketPoll):
    def recv(self, nbytes):
        raise OSError(116, "ETIMEDOUT")


class _SocketErrnoTimeoutPollMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SocketErrnoTimeoutPoll()

    def loop(self, timeout=0.0):
        raise AssertionError("MiniMQTT loop should not be called")


class _SocketPystackPoll(_SocketPoll):
    def recv(self, nbytes):
        raise RuntimeError("pystack exhausted")


class _SocketPystackPollMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SocketPystackPoll()

    def loop(self, timeout=0.0):
        raise AssertionError("MiniMQTT loop should not be called")


class _RecvIntoRemainingPystackPoll:
    def __init__(self):
        self.calls = 0

    def recv_into(self, buffer, nbytes):
        self.calls += 1
        if self.calls == 1:
            buffer[0] = 0x30
            return 1
        raise RuntimeError("pystack exhausted")

    def send(self, buffer):
        return len(buffer)


class _RemainingPystackPollMQTTClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _RecvIntoRemainingPystackPoll()

    def loop(self, timeout=0.0):
        raise AssertionError("MiniMQTT loop should not be called")


class _RecvIntoAlwaysAritySocket:
    def __init__(self):
        self.recv_calls = []

    def recv_into(self, *args):
        raise TypeError("function takes 3 positional arguments but 2 were given")

    def recv(self, nbytes):
        self.recv_calls.append(nbytes)
        return b"\x01"[:nbytes]


class _WrappedSocketWithRawRecvInto:
    def __init__(self):
        self._socket = _RecvIntoNeedsNbytesSocket()

    def recv_into(self, buffer):
        return self._socket.recv_into(buffer)

    def recv(self, nbytes):
        buffer = bytearray(nbytes)
        count = self.recv_into(buffer)
        return bytes(buffer[:count])


class _WrappedSocketExactArityWithRawRecvInto:
    def __init__(self):
        self._socket = _RecvIntoNeedsNbytesSocket()
        self.recv_into_calls = []

    def recv_into(self, *args):
        self.recv_into_calls.append(len(args))
        raise TypeError("function takes 3 positional arguments but 2 were given")


class _RecvIntoNeedsNbytesPool:
    def __init__(self):
        self.socket_obj = _RecvIntoNeedsNbytesSocket()

    def socket(self, *args, **kwargs):
        return self.socket_obj


class _MiniMQTTSocketRecvIntoClient(_FakeMQTTClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.socket_obj = kwargs["socket_pool"].socket()

    def loop(self, timeout=0.0):
        buffer = bytearray(1)
        self.socket_obj.recv_into(buffer)


class _MiniMQTTConnectedSocketRecvIntoClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _RecvIntoNeedsNbytesSocket()
        self._backwards_compatible_sock = True

    def loop(self, timeout=0.0):
        buffer = bytearray(1)
        self._sock.recv_into(buffer)


class _MiniMQTTConnectedSocketRecvFallbackClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _RecvIntoAlwaysAritySocket()
        self._backwards_compatible_sock = True

    def loop(self, timeout=0.0):
        buffer = bytearray(1)
        self._sock.recv_into(buffer, 1)


class _MiniMQTTConnectedWrappedRawSocketClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _WrappedSocketWithRawRecvInto()
        self._backwards_compatible_sock = True

    def loop(self, timeout=0.0):
        buffer = bytearray(1)
        self._sock.recv_into(buffer, 1)


class _ProbeSocket:
    def __init__(self, *, fail_connect=False):
        self.fail_connect = fail_connect
        self.timeout = None
        self.connected_to = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        self.connected_to = address
        if self.fail_connect:
            raise RuntimeError("probe refused")

    def close(self):
        self.closed = True


class _ProbeSocketPool:
    def __init__(self, *, fail_connect=False):
        self.socket_obj = _ProbeSocket(fail_connect=fail_connect)
        self.socket_calls = 0

    def socket(self):
        self.socket_calls += 1
        return self.socket_obj


class _ConnectProbeSocket(_ProbeSocket):
    def __init__(self, connack=b"\x20\x02\x00\x00"):
        super().__init__()
        self.incoming = bytearray(connack)
        self.sent = bytearray()

    def send(self, data):
        self.sent.extend(bytes(data))
        return len(data)

    def recv(self, nbytes):
        if not self.incoming:
            return b""
        count = min(int(nbytes or 0), len(self.incoming))
        data = bytes(self.incoming[:count])
        del self.incoming[:count]
        return data


class _ConnectProbeSocketPool:
    def __init__(self, connack=b"\x20\x02\x00\x00"):
        self.socket_obj = _ConnectProbeSocket(connack)
        self.socket_calls = 0

    def socket(self):
        self.socket_calls += 1
        return self.socket_obj


class _MiniMQTTConnectShouldNotRunClient(_FakeMQTTClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        raise AssertionError("MiniMQTT connect should not run")


class _MiniMQTTConnectedExactArityWrappedRawSocketClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _WrappedSocketExactArityWithRawRecvInto()
        self._backwards_compatible_sock = True

    def loop(self, timeout=0.0):
        buffer = bytearray(1)
        self._sock.recv_into(buffer, 1)


class _MiniMQTTConnectedSocketSendClient(_FakeMQTTClient):
    def connect(self):
        super().connect()
        self._sock = _SendNeedsNbytesSocket()
        self._backwards_compatible_sock = True

    def loop(self, timeout=0.0):
        self._sock.send(memoryview(bytearray(b"\xc0\0")))


def _runtime_config():
    return RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="broker.local",
            broker_ip="10.0.0.9",
            port=1883,
        ),
        sensor=DetectedSensor(
            family="i2c",
            interface="i2c",
            device="aqi",
            sensor_id="aqi-x943fm",
        ),
        switch=SwitchConfig(
            present=True,
            device_id="switch-x943fm",
            channel_count=1,
            channels=(
                SwitchChannelConfig(
                    key="SWITCH_1",
                    channel_id="S1-x943fm",
                    label="Fan",
                    enable_pin="GP5",
                    control_pin="GP28",
                ),
            ),
        ),
    )


def test_build_mqtt_client_adapter_uses_runtime_target_and_credentials():
    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=8883,
            use_tls=True,
            username="user1",
            password="pass1",
        ),
    )

    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        ssl_context=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    assert adapter.phase == "ready"
    assert adapter.broker == "10.0.0.4"
    assert adapter.port == 8883
    assert adapter.broker_targets == ("10.0.0.4",)
    assert adapter.active_broker == "10.0.0.4"
    assert adapter.client.kwargs["broker"] == "10.0.0.4"
    assert adapter.client.kwargs["ssl_context"] is not None
    assert adapter.client.kwargs["username"] == "user1"
    assert adapter.client.kwargs["password"] == "pass1"
    assert adapter.client.kwargs["socket_timeout"] == 3
    assert adapter.client.kwargs["connect_retries"] == 1


def test_build_mqtt_client_adapter_drops_optional_connect_kwargs_when_unsupported():
    runtime_config = _runtime_config()

    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _RejectOptionalMQTTKwargsClient},
    )

    assert adapter.phase == "ready"
    assert "socket_timeout" not in adapter.client.kwargs
    assert "connect_retries" not in adapter.client.kwargs
    assert "client_id" not in adapter.client.kwargs
    assert adapter.client.kwargs["broker"] == "10.0.0.9"


def test_build_mqtt_client_adapter_sets_runtime_client_id():
    adapter = build_mqtt_client_adapter(
        _runtime_config(),
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    assert adapter.phase == "ready"
    assert adapter.client.kwargs["client_id"] == "aqi-x943fm"


def test_connect_sync_poll_and_disconnect_flow():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    assert connect_result.phase == "connected"
    assert transport.connected is True
    assert connect_result.adapter.active_broker == "10.0.0.9"

    transport.subscribe("nodus/S1-x943fm/config/set")
    transport.publish(
        "nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"}, retain=False
    )
    sync_result = sync_transport_to_client(connect_result.adapter, transport)
    assert sync_result.phase == "synced"
    assert sync_result.subscribed_count == 1
    assert sync_result.published_count == 0
    assert sync_result.operation == "subscribe"
    assert sync_result.topic == "nodus/S1-x943fm/config/set"
    assert sync_result.pending_count == 1
    assert sync_result.elapsed_ms >= 0
    assert sync_result.adapter.client.subscribed == ["nodus/S1-x943fm/config/set"]

    sync_result = sync_transport_to_client(sync_result.adapter, transport)
    assert sync_result.phase == "synced"
    assert sync_result.subscribed_count == 0
    assert sync_result.published_count == 1
    assert sync_result.operation == "publish"
    assert sync_result.topic == "nodus/aqi-x943fm/data"
    assert sync_result.payload_bytes > 0
    assert sync_result.pending_count == 1
    assert sync_result.elapsed_ms >= 0
    assert sync_result.adapter.client.published[0][0] == "nodus/aqi-x943fm/data"

    sync_result.adapter.client.pending_incoming.append(
        ("nodus/S1-x943fm/config/set", b"ON")
    )
    poll_result = poll_mqtt_client(sync_result.adapter, transport)
    assert poll_result.phase == "polled"
    assert poll_result.received_count == 1
    assert transport.received_messages[-1].topic == "nodus/S1-x943fm/config/set"
    assert transport.received_messages[-1].payload_text == "ON"

    disconnect_result = disconnect_mqtt_client(
        sync_result.adapter, transport, runtime_config
    )
    assert disconnect_result.phase == "disconnected"
    assert transport.connected is False
    assert sync_result.adapter.client.disconnected is True
    assert disconnect_result.published_count == 1
    assert (
        sync_result.adapter.client.published[-1][0]
        == "nodus/aqi-x943fm/status/heartbeat"
    )


def test_sync_transport_to_client_records_last_publish_state():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"}, retain=False
    )
    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert transport.last_success_at >= 0.0
    assert transport.last_publish_topic == "nodus/aqi-x943fm/data"
    assert transport.last_publish_bytes == len('{"schema":"nodus-sensor/v1"}')
    assert transport.last_publish_retain == 0
    assert "last_pub_topic=nodus/aqi-x943fm/data" in transport.publish_diagnostic()
    assert sync_result.diagnostic == ""


def test_sync_transport_to_client_records_startup_publish_state():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/status/heartbeat",
        {"schema": "nodus-heartbeat/v1", "status": "online"},
        retain=True,
    )
    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert sync_result.operation == "publish"
    assert sync_result.retain is True
    assert sync_result.diagnostic == ""


def test_sync_transport_to_client_collects_after_retained_device_meta():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    collect_calls = []
    original_collect = mqtt_client_module.gc.collect

    def _collect():
        collect_calls.append(1)
        return 0

    try:
        mqtt_client_module.gc.collect = _collect
        connect_result = connect_mqtt_client(adapter, transport)
        transport.publish(
            "nodus/aqi-x943fm/meta",
            {"schema": "nodus-meta/v1"},
            retain=True,
        )

        sync_result = sync_transport_to_client(connect_result.adapter, transport)
    finally:
        mqtt_client_module.gc.collect = original_collect

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert sync_result.topic == "nodus/aqi-x943fm/meta"
    assert collect_calls == [1]
    assert transport.published_messages == []
    assert sync_result.diagnostic == ""


def test_sync_transport_to_client_publishes_qos0_packet_on_socket():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/aqi-x943fm/data"
    payload = "ON"
    transport.publish(topic, payload, retain=False)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    packet = bytes(connect_result.adapter.client._sock.sent)
    topic_bytes = topic.encode("utf-8")
    assert packet[0] == 0x30
    assert packet[1] == len(packet) - 2
    assert packet[2:4] == bytes((0, len(topic_bytes)))
    assert packet[4 : 4 + len(topic_bytes)] == topic_bytes
    assert packet[-2:] == b"ON"


def test_sync_transport_to_client_chunks_large_qos0_publish_on_socket():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _NoneReturningSocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/meta"
    payload = "x" * 1435
    transport.publish(topic, payload, retain=True)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    expected = bytes(mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, True))
    sock = connect_result.adapter.client._sock
    assert len(expected) == 1462
    assert bytes(sock.sent) == expected
    chunk_size = mqtt_client_module.MQTT_RAW_SEND_CHUNK_BYTES
    assert [len(chunk) for chunk in sock.chunks] == [
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        182,
    ]


def test_sync_transport_to_client_keeps_startup_meta_qos0_by_default():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PubackSocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/meta"
    payload = "x" * 1435
    transport.publish(topic, payload, retain=True)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert transport.published_messages == []
    expected = bytes(mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, True))
    sock = connect_result.adapter.client._sock
    assert len(expected) == 1462
    assert bytes(sock.sent) == expected
    chunk_size = mqtt_client_module.MQTT_RAW_SEND_CHUNK_BYTES
    assert [len(chunk) for chunk in sock.chunks] == [
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        182,
    ]
    assert sock.incoming == bytearray(b"\x40\x02\x00\x01")
    assert transport.ack_diagnostic() == ""
    assert sync_result.diagnostic == ""


def test_sync_transport_to_client_can_verify_large_raw_publish_when_debug_enabled():
    original_threshold = mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES
    mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES = 512
    try:
        runtime_config = _runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        transport.mark_connect_requested()
        adapter = build_mqtt_client_adapter(
            runtime_config,
            socket_pool=object(),
            modules={"mqtt_cls": _PubackSocketPublishMQTTClient},
        )

        connect_result = connect_mqtt_client(adapter, transport)
        topic = "nodus/avpd-0kl7sx/meta"
        payload = "x" * 1435
        transport.publish(topic, payload, retain=True)

        sync_result = sync_transport_to_client(connect_result.adapter, transport)
    finally:
        mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES = original_threshold

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert transport.published_messages == []
    expected = bytes(
        mqtt_client_module._mqtt_qos1_publish_packet(topic, payload, True, 1)
    )
    sock = connect_result.adapter.client._sock
    assert len(expected) == 1464
    assert bytes(sock.sent) == expected
    chunk_size = mqtt_client_module.MQTT_RAW_SEND_CHUNK_BYTES
    assert [len(chunk) for chunk in sock.chunks] == [
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        184,
    ]
    assert sock.incoming == bytearray()
    ack_diagnostic = transport.ack_diagnostic()
    assert "last_ack_kind=puback" in ack_diagnostic
    assert "last_ack_topic={}".format(topic) in ack_diagnostic
    assert "last_ack_stage=complete" in ack_diagnostic
    assert "last_ack_packet_id=1" in ack_diagnostic
    assert sync_result.diagnostic == ""


def test_sync_transport_to_client_keeps_large_non_startup_publish_qos0():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PubackSocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/data"
    payload = "x" * 700
    transport.publish(topic, payload, retain=False)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    expected = bytes(
        mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, False)
    )
    sock = connect_result.adapter.client._sock
    assert len(expected) > mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES
    assert bytes(sock.sent) == expected
    assert [len(chunk) for chunk in sock.chunks] == [len(expected)]
    assert sock.incoming == bytearray(b"\x40\x02\x00\x01")
    assert transport.ack_diagnostic() == ""


def test_sync_transport_to_client_keeps_mid_size_raw_publish_qos0():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PubackSocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/data"
    payload = "x" * 320
    transport.publish(topic, payload, retain=False)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    expected = bytes(
        mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, False)
    )
    sock = connect_result.adapter.client._sock
    assert mqtt_client_module.MQTT_RAW_SEND_CHUNK_BYTES < len(expected)
    assert mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES == 0
    assert bytes(sock.sent) == expected
    assert [len(chunk) for chunk in sock.chunks] == [len(expected)]
    assert sock.incoming == bytearray(b"\x40\x02\x00\x01")


def test_sync_transport_to_client_ignores_puback_timeout_by_default():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PubackTimeoutSocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/meta"
    payload = "x" * 1435
    transport.publish(topic, payload, retain=True)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert transport.connected is True
    assert transport.published_messages == []
    expected = bytes(mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, True))
    sock = connect_result.adapter.client._sock
    assert bytes(sock.sent) == expected
    assert transport.ack_diagnostic() == ""


def test_sync_transport_to_client_reports_optional_puback_timeout_when_enabled():
    original_threshold = mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES
    mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES = 512
    try:
        runtime_config = _runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        transport.mark_connect_requested()
        adapter = build_mqtt_client_adapter(
            runtime_config,
            socket_pool=object(),
            modules={"mqtt_cls": _PubackTimeoutSocketPublishMQTTClient},
        )

        connect_result = connect_mqtt_client(adapter, transport)
        topic = "nodus/avpd-0kl7sx/meta"
        payload = "x" * 1435
        transport.publish(topic, payload, retain=True)

        sync_result = sync_transport_to_client(connect_result.adapter, transport)
    finally:
        mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES = original_threshold

    assert sync_result.phase == "error"
    assert sync_result.published_count == 0
    assert sync_result.operation == "publish"
    assert sync_result.topic == topic
    assert transport.connected is False
    assert len(transport.published_messages) == 1
    assert transport.published_messages[0].topic == topic
    error = sync_result.errors[0]
    assert error.startswith("mqtt_publish_failed:{}:bytes=1435:".format(topic))
    assert "raw_publish_diag qos=1 pkt_id=1" in error
    assert "pkt_bytes=1464" in error
    assert "topic_len={}".format(len(topic.encode("utf-8"))) in error
    assert "puback_ms=" in error
    assert "puback_timeout_s=1" in error
    assert "sock=raw" in error
    assert "caps=_PubackSocket:send1 recv1 recv_into0" in error
    assert "error=mqtt_puback_stage=header:[Errno 116] ETIMEDOUT" in error
    assert "raw_publish_diag qos=1 pkt_id=1" in transport.last_disconnect_reason


def test_sync_transport_to_client_retries_partial_qos0_socket_sends():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PartialSocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/meta"
    payload = "x" * 1435
    transport.publish(topic, payload, retain=True)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    expected = bytes(mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, True))
    sock = connect_result.adapter.client._sock
    assert bytes(sock.sent) == expected
    assert max(sock.calls) <= mqtt_client_module.MQTT_RAW_SEND_CHUNK_BYTES
    assert len(sock.calls) > 3


def test_sync_transport_to_client_chunks_send_nbytes_only_socket():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SendNbytesOnlySocketPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/meta"
    payload = "x" * 1435
    transport.publish(topic, payload, retain=True)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    expected = bytes(mqtt_client_module._mqtt_qos0_publish_packet(topic, payload, True))
    sock = connect_result.adapter.client._sock
    assert bytes(sock.sent) == expected
    chunk_size = mqtt_client_module.MQTT_RAW_SEND_CHUNK_BYTES
    assert sock.calls == [
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        chunk_size,
        182,
    ]


def test_sync_transport_to_client_subscribes_qos0_packet_on_socket():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketSubscribeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/S1-x943fm/config/set"
    transport.subscribe(topic)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.subscribed_count == 1
    packet = bytes(connect_result.adapter.client._sock.sent)
    topic_bytes = topic.encode("utf-8")
    assert packet[0] == 0x82
    assert packet[1] == len(packet) - 2
    assert packet[2:4] == b"\x00\x01"
    assert packet[4:6] == bytes((0, len(topic_bytes)))
    assert packet[6 : 6 + len(topic_bytes)] == topic_bytes
    assert packet[-1] == 0
    assert connect_result.adapter.client._sock.incoming == bytearray()
    assert connect_result.adapter.client._sock.timeouts == [1]
    ack_diagnostic = transport.ack_diagnostic()
    assert "last_ack_kind=suback" in ack_diagnostic
    assert "last_ack_topic={}".format(topic) in ack_diagnostic
    assert "last_ack_stage=complete" in ack_diagnostic
    assert "last_ack_packet_id=1" in ack_diagnostic


def test_sync_transport_to_client_reports_raw_subscribe_failure_diagnostics():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketSubscribeTimeoutMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    topic = "nodus/avpd-0kl7sx/config/set"
    transport.subscribe(topic)

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "error"
    assert sync_result.operation == "subscribe"
    assert sync_result.topic == topic
    assert transport.connected is False
    error = sync_result.errors[0]
    assert error.startswith("mqtt_subscribe_failed:topic={}:index=0/1:".format(topic))
    assert "raw_subscribe_diag pkt_id=1" in error
    packet_len = len(connect_result.adapter.client._sock.sent)
    assert "pkt_bytes={}".format(packet_len) in error
    assert "topic_len={}".format(len(topic.encode("utf-8"))) in error
    assert "send_ms=" in error
    assert "suback_ms=" in error
    assert "runtime_timeout_s=1" in error
    assert "suback_timeout_s=1" in error
    assert "suback_timeout_set=0" in error
    assert "sock=raw" in error
    assert "caps=_SubscribeTimeoutSocket:send1 recv1 recv_into0" in error
    assert "before=free_mem=" in error
    assert "after=free_mem=" in error
    assert "error=mqtt_suback_stage=header:[Errno 116] ETIMEDOUT" in error
    assert "raw_subscribe_diag pkt_id=1" in transport.last_disconnect_reason
    assert connect_result.adapter.client._sock.timeouts == [1]


def test_poll_mqtt_client_receives_qos0_publish_on_socket_without_loop():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketPollMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert poll_result.received_count == 1
    assert transport.connected is True
    assert transport.received_messages[-1].topic == _SocketPollMQTTClient.topic
    assert transport.received_messages[-1].payload_text == _SocketPollMQTTClient.payload
    assert transport.last_loop_socket_state == "raw"
    assert connect_result.adapter.client._sock.incoming == bytearray()


def test_poll_mqtt_client_receives_qos0_publish_on_recv_into_only_socket():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _RecvIntoSocketPollMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert poll_result.received_count == 1
    assert transport.connected is True
    assert transport.received_messages[-1].topic == _RecvIntoSocketPollMQTTClient.topic
    assert (
        transport.received_messages[-1].payload_text
        == _RecvIntoSocketPollMQTTClient.payload
    )
    assert transport.last_loop_socket_state == "raw"
    assert connect_result.adapter.client._sock.recv_into_calls == [
        1,
        1,
        len(_mqtt_publish_packet("nodus/S1-x943fm/config/set", "ON")) - 2,
    ]
    assert connect_result.adapter.client._sock.incoming == bytearray()


def test_poll_mqtt_client_treats_socket_timeout_as_empty_poll():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketEmptyPollMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert poll_result.received_count == 0
    assert transport.connected is True
    assert transport.received_messages == []
    assert transport.last_loop_socket_state == "raw"


def test_poll_mqtt_client_treats_etimedout_as_empty_poll():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketErrnoTimeoutPollMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert poll_result.received_count == 0
    assert transport.connected is True
    assert transport.last_disconnect_reason == ""
    assert transport.received_messages == []


def test_poll_mqtt_client_reports_ebadf_context_after_raw_puback():
    original_threshold = mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES
    mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES = 512
    try:
        runtime_config = _runtime_config()
        transport = MQTTTransport("broker.local", 1883)
        transport.mark_connect_requested()
        adapter = build_mqtt_client_adapter(
            runtime_config,
            socket_pool=object(),
            modules={"mqtt_cls": _PubackThenEbadfSocketPublishMQTTClient},
        )

        connect_result = connect_mqtt_client(adapter, transport)
        topic = "nodus/avpd-0kl7sx/meta"
        payload = "x" * 1435
        transport.publish(topic, payload, retain=True)
        sync_result = sync_transport_to_client(connect_result.adapter, transport)

        assert sync_result.phase == "synced"
        poll_result = poll_mqtt_client(sync_result.adapter, transport)
    finally:
        mqtt_client_module.MQTT_RAW_VERIFY_PUBLISH_BYTES = original_threshold

    assert poll_result.phase == "error"
    error = poll_result.errors[0]
    assert error.startswith("mqtt_poll_failed:[Errno 9] EBADF")
    assert "poll_sock=raw" in error
    assert "poll_caps=_PubackThenEbadfSocket:send1 recv1 recv_into0" in error
    assert "poll_timeout_s=1" in error
    assert "last_pub_topic={}".format(topic) in error
    assert "last_pub_retain=1" in error
    assert "last_ack_kind=puback" in error
    assert "last_ack_topic={}".format(topic) in error
    assert "last_ack_stage=complete" in error
    assert "last_ack_packet_id=1" in error
    assert "last_ack_timeout_s=1" in error


def test_poll_mqtt_client_reports_raw_socket_pystack_source():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SocketPystackPollMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert "mqtt_poll_failed:pystack source=raw_socket" in poll_result.errors[0]
    assert "caps=_SocketPystackPoll:send1 recv1 recv_into0" in poll_result.errors[0]
    assert "raw_stage=header:pystack exhausted" in poll_result.errors[0]


def test_poll_mqtt_client_reports_remaining_length_pystack_stage():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _RemainingPystackPollMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert "mqtt_poll_failed:pystack source=raw_socket" in poll_result.errors[0]
    assert "caps=_RecvIntoRemainingPystackPoll:send1 recv0 recv_into1" in (
        poll_result.errors[0]
    )
    assert "raw_stage=remaining_length:pystack exhausted" in poll_result.errors[0]


def test_poll_mqtt_client_reports_minimqtt_loop_pystack_source():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _LoopPystackFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert "mqtt_poll_failed:pystack source=minimqtt_loop" in poll_result.errors[0]
    assert "caps=none" in poll_result.errors[0]


def test_poll_mqtt_client_records_last_loop_diagnostics():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.last_loop_received == 0
    assert transport.last_loop_timeout == 1.0
    assert transport.last_loop_socket_state == "none"
    assert transport.last_loop_client_connected == "1"
    assert "last_loop_received=0" in transport.loop_diagnostic()


def test_build_mqtt_client_adapter_is_unavailable_without_socket_pool():
    adapter = build_mqtt_client_adapter(
        _runtime_config(),
        socket_pool=None,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    assert adapter.phase == "unavailable"
    assert "socket_pool_unavailable" in adapter.errors


def test_connect_mqtt_client_uses_broker_ip_when_hostname_is_configured():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )
    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "connected"
    assert transport.connected is True
    assert connect_result.adapter.active_broker == "10.0.0.4"
    assert connect_result.adapter.client.kwargs["broker"] == "10.0.0.4"


def test_connect_mqtt_client_falls_back_to_broker_ip_alt():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            broker_ip_alt="10.0.0.5",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )
    original_failures = set(_FakeMQTTClient.fail_connect_for)
    _FakeMQTTClient.fail_connect_for = {"10.0.0.4"}
    try:
        connect_result = connect_mqtt_client(adapter, transport)
    finally:
        _FakeMQTTClient.fail_connect_for = original_failures

    assert adapter.broker_targets == ("10.0.0.4", "10.0.0.5")
    assert connect_result.phase == "connected"
    assert transport.connected is True
    assert connect_result.adapter.active_broker == "10.0.0.5"
    assert connect_result.adapter.client.kwargs["broker"] == "10.0.0.5"


def test_connect_mqtt_client_does_not_resolve_hostname_when_broker_ip_exists():
    class _NoResolvePool:
        def getaddrinfo(self, host, port):
            raise AssertionError("hostname resolution should not run for MQTT connect")

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_NoResolvePool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "connected"
    assert connect_result.adapter.active_broker == "10.0.0.4"
    assert connect_result.adapter.client.kwargs["broker"] == "10.0.0.4"


def test_build_mqtt_client_adapter_requires_ip_target():
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    assert adapter.phase == "unavailable"
    assert adapter.broker_targets == ()
    assert "mqtt_broker_ip_unavailable" in adapter.errors


def test_preflight_mqtt_broker_can_resolve_explicit_hostname():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "ha.local"
            return [(None, None, None, None, ("10.0.0.4", port))]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveOKPool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, resolved_ip = preflight_mqtt_broker(adapter, "ha.local")

    assert error == ""
    assert resolved_ip == "10.0.0.4"


def test_preflight_mqtt_broker_tcp_uses_adapter_socket_pool():
    socket_pool = _ProbeSocketPool()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, target = preflight_mqtt_broker_tcp(adapter)

    assert error == ""
    assert target == "10.0.0.4"
    assert socket_pool.socket_calls == 1
    assert socket_pool.socket_obj.timeout == 3
    assert socket_pool.socket_obj.connected_to == ("10.0.0.4", 1883)
    assert socket_pool.socket_obj.closed is True


def test_preflight_mqtt_broker_tcp_reports_socket_connect_errors():
    socket_pool = _ProbeSocketPool(fail_connect=True)
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, target = preflight_mqtt_broker_tcp(adapter)

    assert target == "10.0.0.4"
    assert error.startswith("mqtt_tcp_preflight_failed:10.0.0.4:RuntimeError")
    assert socket_pool.socket_obj.closed is True


def test_preflight_mqtt_broker_connect_reads_connack_and_disconnects():
    socket_pool = _ConnectProbeSocketPool()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
            username="user",
            password="secret",
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, target, client_id, connack = preflight_mqtt_broker_connect(adapter)

    assert error == ""
    assert target == "10.0.0.4"
    assert client_id == "cpynodus-probe"
    assert connack == 0
    assert socket_pool.socket_obj.connected_to == ("10.0.0.4", 1883)
    assert socket_pool.socket_obj.closed is True
    packet = bytes(socket_pool.socket_obj.sent)
    assert packet[0] == 0x10
    assert b"\x00\x04MQTT" in packet
    assert b"cpynodus-probe" in packet
    assert b"user" in packet
    assert b"secret" in packet
    assert packet[-2:] == b"\xe0\x00"


def test_preflight_mqtt_broker_connect_accepts_probe_client_id_override():
    socket_pool = _ConnectProbeSocketPool()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, _target, client_id, connack = preflight_mqtt_broker_connect(
        adapter,
        client_id="warmup-aqi-wfcp7p",
    )

    assert error == ""
    assert client_id == "warmup-aqi-wfcp7p"
    assert connack == 0
    packet = bytes(socket_pool.socket_obj.sent)
    assert b"warmup-aqi-wfcp7p" in packet
    assert b"cpynodus-probe" not in packet


def test_preflight_mqtt_broker_connect_reports_connack_refusal():
    socket_pool = _ConnectProbeSocketPool(b"\x20\x02\x00\x02")
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, target, _client_id, connack = preflight_mqtt_broker_connect(adapter)

    assert target == "10.0.0.4"
    assert connack == 2
    assert error == "mqtt_connect_probe_failed:10.0.0.4:connack_code=2"
    assert socket_pool.socket_obj.closed is True


def test_connect_mqtt_client_uses_raw_socket_when_available():
    socket_pool = _ConnectProbeSocketPool()
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
        sensor=DetectedSensor(sensor_id="aqi-x943fm"),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "connected"
    assert transport.connected is True
    assert adapter.client.connected is True
    assert socket_pool.socket_calls == 1
    assert bytes(socket_pool.socket_obj.sent).startswith(b"\x10")
    assert connect_result.adapter.client._sock is socket_pool.socket_obj


def test_connect_mqtt_client_reports_raw_connack_refusal():
    socket_pool = _ConnectProbeSocketPool(b"\x20\x02\x00\x02")
    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            broker_ip="10.0.0.4",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "error"
    assert transport.connected is False
    assert adapter.client.connected is False
    assert socket_pool.socket_calls == 1
    assert socket_pool.socket_obj.closed is True
    assert connect_result.errors == (
        "mqtt_connect_failed:10.0.0.4:raw:OSError:"
        "mqtt_connack_code:10.0.0.4:2",
    )


def test_poll_mqtt_client_uses_timeout_compatible_with_socket_timeout():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _TimeoutSensitiveMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    connect_result.adapter.client.pending_incoming.append(
        ("nodus/S1-x943fm/config/set", b"ON")
    )

    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert poll_result.received_count == 1
    assert connect_result.adapter.client.socket_timeout == 1
    assert connect_result.adapter.client.loop_timeouts == [1.0]


def test_sync_transport_to_client_marks_transport_disconnected_on_publish_oserror():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PublishFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"}, retain=False
    )

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "error"
    assert transport.connected is False
    assert "mqtt_publish_failed:nodus/aqi-x943fm/data" in sync_result.errors[0]
    assert ":bytes=" in sync_result.errors[0]
    assert transport.published_messages == []


def test_sync_transport_to_client_keeps_failed_retained_publish_for_retry():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PublishFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/availability",
        {"status": "online"},
        retain=True,
    )

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "error"
    assert len(transport.published_messages) == 1
    assert transport.published_messages[0].topic == "nodus/aqi-x943fm/availability"


def test_sync_transport_to_client_compacts_successes_before_failed_publish():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PublishFailSecondMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/availability", {"status": "online"}, retain=True
    )
    transport.publish(
        "nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"}, retain=False
    )

    sync_result = sync_transport_to_client(connect_result.adapter, transport)
    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1

    sync_result = sync_transport_to_client(sync_result.adapter, transport)

    assert sync_result.phase == "error"
    assert sync_result.published_count == 0
    assert transport.published_messages == []


def test_sync_transport_to_client_disconnects_on_subscribe_failure():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SubscribeFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.subscribe("nodus/S1-x943fm/config/set")

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "error"
    assert transport.connected is False
    assert transport.subscription_retry_after == 0.0
    assert sync_result.operation == "subscribe"
    assert sync_result.topic == "nodus/S1-x943fm/config/set"
    assert sync_result.pending_count == 1
    assert sync_result.elapsed_ms >= 0
    assert sync_result.errors == (
        (
            "mqtt_subscribe_failed:topic=nodus/S1-x943fm/config/set:index=0/1:"
            "No data received from broker for 10 seconds."
        ),
    )
    assert transport.last_disconnect_reason == (
        "mqtt_subscribe_failed:nodus/S1-x943fm/config/set:"
        "No data received from broker for 10 seconds."
    )


def test_sync_transport_to_client_compacts_successful_subscriptions_before_failure():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SubscribeFailSecondMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.subscribe("nodus/one")
    transport.subscribe("nodus/two")

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "error"
    assert sync_result.subscribed_count == 1
    assert transport.connected is False
    assert transport.subscriptions == ["nodus/two"]
    assert transport.subscription_retry_after == 0.0
    assert sync_result.errors == (
        (
            "mqtt_subscribe_failed:topic=nodus/two:index=1/2:"
            "No data received from broker for 10 seconds."
        ),
    )


def test_sync_transport_to_client_subscribes_before_low_priority_publish():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish("nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"})
    transport.subscribe("nodus/S1-x943fm/config/set")

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 0
    assert sync_result.subscribed_count == 1
    assert sync_result.operation == "subscribe"
    assert sync_result.adapter.client.subscribed == ["nodus/S1-x943fm/config/set"]
    assert transport.published_messages[0].topic == "nodus/aqi-x943fm/data"
    assert transport.subscriptions == []


def test_sync_transport_to_client_publishes_priority_status_before_subscribe():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish("nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"})
    transport.publish(
        "nodus/aqi-x943fm/status/heartbeat",
        {"online": True},
        retain=True,
    )
    transport.subscribe("nodus/S1-x943fm/config/set")

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert sync_result.subscribed_count == 0
    assert sync_result.operation == "publish"
    assert sync_result.topic == "nodus/aqi-x943fm/status/heartbeat"
    assert sync_result.adapter.client.published[0][0] == (
        "nodus/aqi-x943fm/status/heartbeat"
    )
    assert [message.topic for message in transport.published_messages] == [
        "nodus/aqi-x943fm/data"
    ]
    assert transport.subscriptions == ["nodus/S1-x943fm/config/set"]


def test_sync_transport_to_client_defers_subscribe_until_clean_poll():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish("nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"})
    transport.publish(
        "nodus/aqi-x943fm/status/heartbeat",
        {"online": True},
        retain=True,
    )
    transport.subscribe("nodus/S1-x943fm/config/set")

    sync_result = sync_transport_to_client(
        connect_result.adapter,
        transport,
        require_clean_poll_before_subscribe=True,
    )
    assert sync_result.phase == "synced"
    assert sync_result.operation == "publish"
    assert sync_result.topic == "nodus/aqi-x943fm/status/heartbeat"

    sync_result = sync_transport_to_client(
        sync_result.adapter,
        transport,
        require_clean_poll_before_subscribe=True,
    )
    assert sync_result.phase == "deferred"
    assert sync_result.operation == "subscribe"
    assert sync_result.pending_count == 1
    assert sync_result.adapter.client.subscribed == []
    assert transport.subscriptions == ["nodus/S1-x943fm/config/set"]

    poll_result = poll_mqtt_client(sync_result.adapter, transport)
    assert poll_result.phase == "polled"

    sync_result = sync_transport_to_client(
        poll_result.adapter,
        transport,
        require_clean_poll_before_subscribe=True,
    )
    assert sync_result.phase == "synced"
    assert sync_result.subscribed_count == 1
    assert sync_result.operation == "subscribe"
    assert sync_result.adapter.client.subscribed == ["nodus/S1-x943fm/config/set"]
    assert transport.published_messages[0].topic == "nodus/aqi-x943fm/data"


def test_sync_transport_to_client_disconnects_after_slow_publish():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SlowPublishMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/availability",
        {"status": "online"},
        retain=True,
    )

    sync_result = sync_transport_to_client(
        connect_result.adapter,
        transport,
        slow_operation_ms=1,
    )

    assert sync_result.phase == "error"
    assert sync_result.published_count == 1
    assert sync_result.operation == "publish"
    assert sync_result.topic == "nodus/aqi-x943fm/availability"
    assert sync_result.errors[0].startswith(
        "mqtt_publish_slow:nodus/aqi-x943fm/availability"
    )
    assert transport.connected is False
    assert transport.published_messages == []


def test_close_mqtt_client_keeps_pending_publish_queue():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )
    connect_result = connect_mqtt_client(adapter, transport)

    close_result = close_mqtt_client(connect_result.adapter, transport)

    assert close_result.phase == "disconnected"
    assert transport.connected is False
    assert close_result.adapter.client.disconnected is True
    assert close_result.adapter.client.published == []


def test_poll_mqtt_client_marks_transport_disconnected_on_oserror():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PollFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert poll_result.errors == ("mqtt_poll_failed:9",)


def test_close_mqtt_client_disconnects_without_shutdown_publish():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )
    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish("nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"})

    close_result = close_mqtt_client(connect_result.adapter, transport)

    assert close_result.phase == "disconnected"
    assert transport.connected is False
    assert transport.last_disconnect_reason == "mqtt_close_requested"
    assert connect_result.adapter.client.disconnected is True
    assert [message.topic for message in transport.published_messages] == [
        "nodus/aqi-x943fm/data"
    ]


def test_close_mqtt_client_force_closes_socket_when_client_is_not_connected():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _UnconnectedSocketMQTTClient},
    )
    client = adapter.client

    close_result = close_mqtt_client(adapter, transport)

    assert close_result.phase == "disconnected"
    assert close_result.errors == ()
    assert transport.connected is False
    assert client._sock is None
    assert client._is_connected is False
    assert client._connection_manager.closed[0].closed is True


def test_disconnect_mqtt_client_swallow_shutdown_publish_oserror():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _PublishFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    disconnect_result = disconnect_mqtt_client(
        connect_result.adapter, transport, runtime_config
    )

    assert disconnect_result.phase == "disconnected"
    assert transport.connected is False
    assert disconnect_result.errors
    assert "mqtt_publish_failed:" in disconnect_result.errors[0]


def test_disconnect_mqtt_client_reports_disconnect_oserror():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _DisconnectFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    disconnect_result = disconnect_mqtt_client(
        connect_result.adapter, transport, runtime_config
    )

    assert disconnect_result.phase == "disconnected"
    assert transport.connected is False
    assert "mqtt_disconnect_failed:9" in disconnect_result.errors


def test_poll_mqtt_client_tolerates_disconnect_callback_with_two_args():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _CallbackDisconnectMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is False


def test_poll_mqtt_client_does_not_retry_internal_typeerror():
    import pytest

    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _InternalTypeErrorMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    with pytest.raises(TypeError, match="simulated internal mqtt failure"):
        poll_mqtt_client(connect_result.adapter, transport)


def test_poll_mqtt_client_marks_transport_disconnected_on_callback_arity_typeerror():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _CallbackArityFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert poll_result.errors == (
        "mqtt_poll_callback_failed:missing 1 required positional argument",
    )


def test_poll_mqtt_client_adapts_socket_recv_into_without_nbytes():
    runtime_config = _runtime_config()
    socket_pool = _RecvIntoNeedsNbytesPool()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=socket_pool,
        modules={
            "mqtt_cls": _MiniMQTTSocketRecvIntoClient,
            "wrap_socket_pool_before_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    assert socket_pool.socket_obj.recv_into_calls == [1]


def test_poll_mqtt_client_adapts_connected_minimqtt_socket_recv_into():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedSocketRecvIntoClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    assert connect_result.adapter.client._sock._socket_obj.recv_into_calls == [1]
    assert connect_result.adapter.client._backwards_compatible_sock is False


def test_poll_mqtt_client_falls_back_to_recv_when_recv_into_arity_persists():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedSocketRecvFallbackClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    assert connect_result.adapter.client._sock._socket_obj.recv_calls == [1]
    assert connect_result.adapter.client._backwards_compatible_sock is False


def test_poll_mqtt_client_uses_raw_socket_when_wrapped_recv_still_has_arity_error():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedWrappedRawSocketClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    raw_socket = connect_result.adapter.client._sock._socket_obj._socket
    assert raw_socket.recv_into_calls == [1]
    assert connect_result.adapter.client._backwards_compatible_sock is False


def test_poll_mqtt_client_uses_raw_socket_after_exact_wrapped_arity_error():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedExactArityWrappedRawSocketClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    wrapped_socket = connect_result.adapter.client._sock._socket_obj
    assert wrapped_socket.recv_into_calls == [2, 1]
    assert wrapped_socket._socket.recv_into_calls == [1]
    assert connect_result.adapter.client._backwards_compatible_sock is False


def test_poll_mqtt_client_adapts_connected_minimqtt_socket_send_without_nbytes():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedSocketSendClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    assert connect_result.adapter.client._sock._socket_obj.send_calls == [2]
    assert connect_result.adapter.client._backwards_compatible_sock is False


def test_poll_mqtt_client_labels_minimqtt_wrapped_socket_typeerror():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _MiniMQTTWrappedSocketFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert poll_result.errors == (
        "mqtt_poll_failed:minimqtt_socket:sock=none client_connected=1 backcompat=0 "
        "error=function takes 3 positional arguments but 2 were given",
    )


def test_poll_mqtt_client_labels_wrapped_socket_typeerror_client_state():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedWrappedSocketFailMQTTClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert poll_result.errors == (
        "mqtt_poll_failed:minimqtt_socket:sock=wrapped client_connected=1 "
        "backcompat=0 error=function takes 3 positional arguments but 2 were given",
    )


def test_poll_mqtt_client_includes_last_publish_diagnostics_on_wrapped_socket_error():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedWrappedSocketFailMQTTClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish(
        "nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"}, retain=False
    )
    sync_transport_to_client(connect_result.adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert "last_pub_topic=nodus/aqi-x943fm/data" in poll_result.errors[0]
    assert "last_pub_sock=wrapped" in poll_result.errors[0]


def test_poll_mqtt_client_includes_last_loop_diagnostics_on_wrapped_socket_error():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _MiniMQTTConnectedWrappedSocketFailAfterCleanLoopMQTTClient,
            "wrap_socket_after_connect": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    clean_poll_result = poll_mqtt_client(connect_result.adapter, transport)
    assert clean_poll_result.phase == "polled"

    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert "last_loop_received=0" in poll_result.errors[0]
    assert "last_loop_sock=wrapped" in poll_result.errors[0]
    assert "last_loop_client_connected=1" in poll_result.errors[0]


def test_poll_mqtt_client_marks_disconnected_when_client_reports_disconnected():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _MiniMQTTReportsDisconnectedClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "error"
    assert transport.connected is False
    assert poll_result.errors == ("mqtt_poll_failed:client_disconnected:sock=none",)
    assert connect_result.adapter.client.loop_called is False


def test_poll_mqtt_client_tolerates_two_arg_message_callback():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={
            "mqtt_cls": _TwoArgMessageCallbackMQTTClient,
            "flexible_callback": True,
        },
    )

    connect_result = connect_mqtt_client(adapter, transport)
    connect_result.adapter.client.pending_incoming.append(
        ("nodus/S1-x943fm/config/set", b"OFF")
    )
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert poll_result.received_count == 1
    assert transport.received_messages[-1].payload_text == "OFF"


if __name__ == "__main__":
    _run_direct_tests()
