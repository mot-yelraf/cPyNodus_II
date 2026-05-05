"""Tests for the MQTT client adapter wrapper and callback handling."""

from cpynodus_ii.core import (
    build_mqtt_client_adapter,
    connect_mqtt_client,
    disconnect_mqtt_client,
    poll_mqtt_client,
    preflight_mqtt_broker,
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
        test_connect_sync_poll_and_disconnect_flow,
        test_build_mqtt_client_adapter_is_unavailable_without_socket_pool,
        test_connect_mqtt_client_falls_back_to_broker_ip_when_mdns_target_fails,
        test_connect_mqtt_client_falls_back_when_hostname_resolution_fails_preflight,
        test_connect_mqtt_client_can_skip_preflight_for_hostname_only_targets,
        test_connect_mqtt_client_learns_hostname_ip_without_required_preflight,
        test_poll_mqtt_client_uses_timeout_compatible_with_socket_timeout,
        test_sync_transport_to_client_marks_transport_disconnected_on_publish_oserror,
        test_poll_mqtt_client_marks_transport_disconnected_on_oserror,
        test_disconnect_mqtt_client_swallow_shutdown_publish_oserror,
        test_disconnect_mqtt_client_reports_disconnect_oserror,
        test_poll_mqtt_client_tolerates_disconnect_callback_with_two_args,
        test_poll_mqtt_client_does_not_retry_internal_typeerror,
        test_poll_mqtt_client_marks_transport_disconnected_on_callback_arity_typeerror,
        test_poll_mqtt_client_adapts_socket_recv_into_without_nbytes,
        test_poll_mqtt_client_adapts_connected_minimqtt_socket_recv_into,
        test_poll_mqtt_client_falls_back_to_recv_when_recv_into_arity_persists,
        test_poll_mqtt_client_uses_raw_socket_when_wrapped_recv_still_has_arity_error,
        test_poll_mqtt_client_adapts_connected_minimqtt_socket_send_without_nbytes,
        test_poll_mqtt_client_labels_minimqtt_wrapped_socket_typeerror,
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


class _TimeoutSensitiveMQTTClient(_FakeMQTTClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.socket_timeout = 1
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


class _RecvIntoNeedsNbytesSocket:
    def __init__(self):
        self.recv_into_calls = []

    def recv_into(self, buffer, nbytes):
        self.recv_into_calls.append(nbytes)
        if nbytes:
            buffer[0] = 1
        return nbytes


class _SendNeedsNbytesSocket:
    def __init__(self):
        self.send_calls = []

    def send(self, buffer, nbytes):
        self.send_calls.append(nbytes)
        return nbytes


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
        mqtt=MQTTConfig(broker="broker.local", port=1883),
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
    assert adapter.broker == "ha.local"
    assert adapter.port == 8883
    assert adapter.broker_targets == ("ha.local", "10.0.0.4")
    assert adapter.active_broker == "ha.local"
    assert adapter.client.kwargs["broker"] == "ha.local"
    assert adapter.client.kwargs["ssl_context"] is not None
    assert adapter.client.kwargs["username"] == "user1"
    assert adapter.client.kwargs["password"] == "pass1"


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
    assert connect_result.adapter.active_broker == "broker.local"

    transport.subscribe("nodus/S1-x943fm/config/set")
    transport.publish(
        "nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"}, retain=False
    )
    sync_result = sync_transport_to_client(connect_result.adapter, transport)
    assert sync_result.phase == "synced"
    assert sync_result.subscribed_count == 0
    assert sync_result.published_count == 1
    assert sync_result.adapter.client.published[0][0] == "nodus/aqi-x943fm/data"

    sync_result = sync_transport_to_client(sync_result.adapter, transport)
    assert sync_result.phase == "synced"
    assert sync_result.subscribed_count == 1
    assert sync_result.published_count == 0
    assert sync_result.adapter.client.subscribed == ["nodus/S1-x943fm/config/set"]

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


def test_build_mqtt_client_adapter_is_unavailable_without_socket_pool():
    adapter = build_mqtt_client_adapter(
        _runtime_config(),
        socket_pool=None,
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    assert adapter.phase == "unavailable"
    assert "socket_pool_unavailable" in adapter.errors


def test_connect_mqtt_client_falls_back_to_broker_ip_when_mdns_target_fails():
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
    _FakeMQTTClient.fail_connect_for = {"ha.local"}
    try:
        adapter = build_mqtt_client_adapter(
            runtime_config,
            socket_pool=object(),
            modules={"mqtt_cls": _FakeMQTTClient},
        )
        connect_result = connect_mqtt_client(adapter, transport)
    finally:
        _FakeMQTTClient.fail_connect_for = set()

    assert connect_result.phase == "connected"
    assert transport.connected is True
    assert connect_result.adapter.active_broker == "10.0.0.4"
    assert connect_result.adapter.client.kwargs["broker"] == "10.0.0.4"


def test_connect_mqtt_client_falls_back_when_hostname_resolution_fails_preflight():
    class _ResolveFailPool:
        def getaddrinfo(self, host, port):
            if host == "ha.local":
                raise OSError(-2)
            return [(None, None, None, None, ("10.0.0.4", port))]

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
        socket_pool=_ResolveFailPool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "connected"
    assert connect_result.adapter.active_broker == "10.0.0.4"
    assert connect_result.adapter.client.kwargs["broker"] == "10.0.0.4"


def test_connect_mqtt_client_can_skip_preflight_for_hostname_only_targets():
    class _ResolveFailConnectOKPool:
        def getaddrinfo(self, host, port):
            raise OSError(-2)

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveFailConnectOKPool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport, preflight=False)

    assert connect_result.phase == "connected"
    assert transport.connected is True
    assert connect_result.adapter.active_broker == "ha.local"


def test_connect_mqtt_client_learns_hostname_ip_without_required_preflight():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "ha.local"
            return [(None, None, None, None, ("10.0.0.4", port))]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveOKPool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport, preflight=False)

    assert connect_result.phase == "connected"
    assert connect_result.adapter.active_broker == "ha.local"
    assert connect_result.adapter.resolved_broker_ip == "10.0.0.4"


def test_connect_mqtt_client_keeps_hostname_after_non_tls_preflight():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "ha.local"
            return [(None, None, None, None, ("10.0.0.4", port))]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            port=1883,
        ),
    )
    transport = MQTTTransport("ha.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveOKPool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "connected"
    assert connect_result.adapter.active_broker == "ha.local"
    assert connect_result.adapter.resolved_broker_ip == "10.0.0.4"
    assert connect_result.adapter.client.kwargs["broker"] == "ha.local"


def test_connect_mqtt_client_keeps_tls_hostname_after_preflight():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "ha.local"
            return [(None, None, None, None, ("10.0.0.4", port))]

    runtime_config = RuntimeConfig(
        active_profile="homeassistant",
        mqtt=MQTTConfig(
            broker="ha.local",
            port=8883,
            use_tls=True,
        ),
    )
    transport = MQTTTransport("ha.local", 8883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveOKPool(),
        ssl_context=object(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)

    assert connect_result.phase == "connected"
    assert connect_result.adapter.active_broker == "ha.local"
    assert connect_result.adapter.resolved_broker_ip == "10.0.0.4"
    assert connect_result.adapter.client.kwargs["broker"] == "ha.local"


def test_preflight_mqtt_broker_returns_resolved_ip():
    class _ResolveOKPool:
        def getaddrinfo(self, host, port):
            assert host == "ha.local"
            return [(None, None, None, None, ("10.0.0.4", port))]

    runtime_config = RuntimeConfig(
        active_profile="sensorius",
        mqtt=MQTTConfig(
            broker="ha.local",
            port=1883,
        ),
    )
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=_ResolveOKPool(),
        modules={"mqtt_cls": _FakeMQTTClient},
    )

    error, resolved_ip = preflight_mqtt_broker(adapter)

    assert error == ""
    assert resolved_ip == "10.0.0.4"


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


def test_sync_transport_to_client_marks_transport_disconnected_on_subscribe_exception():
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
    assert sync_result.errors == (
        (
            "mqtt_subscribe_failed:topic=nodus/S1-x943fm/config/set:index=0/1:"
            "No data received from broker for 10 seconds."
        ),
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
    assert sync_result.errors == (
        (
            "mqtt_subscribe_failed:topic=nodus/two:index=1/2:"
            "No data received from broker for 10 seconds."
        ),
    )


def test_sync_transport_to_client_publishes_before_subscribe_exception():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _SettleBeforeSubscribeFailMQTTClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    transport.publish("nodus/aqi-x943fm/data", {"schema": "nodus-sensor/v1"})
    transport.subscribe("nodus/S1-x943fm/config/set")

    sync_result = sync_transport_to_client(connect_result.adapter, transport)

    assert sync_result.phase == "synced"
    assert sync_result.published_count == 1
    assert sync_result.subscribed_count == 0
    assert sync_result.adapter.client.published[0][0] == "nodus/aqi-x943fm/data"
    assert sync_result.adapter.client.loop_count == 0
    assert transport.published_messages == []
    assert transport.subscriptions == ["nodus/S1-x943fm/config/set"]


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
        modules={"mqtt_cls": _MiniMQTTSocketRecvIntoClient},
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
        modules={"mqtt_cls": _MiniMQTTConnectedSocketRecvIntoClient},
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
        modules={"mqtt_cls": _MiniMQTTConnectedSocketRecvFallbackClient},
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
        modules={"mqtt_cls": _MiniMQTTConnectedWrappedRawSocketClient},
    )

    connect_result = connect_mqtt_client(adapter, transport)
    poll_result = poll_mqtt_client(connect_result.adapter, transport)

    assert poll_result.phase == "polled"
    assert transport.connected is True
    raw_socket = connect_result.adapter.client._sock._socket_obj._socket
    assert raw_socket.recv_into_calls == [1]
    assert connect_result.adapter.client._backwards_compatible_sock is False


def test_poll_mqtt_client_adapts_connected_minimqtt_socket_send_without_nbytes():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _MiniMQTTConnectedSocketSendClient},
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
        "mqtt_poll_failed:minimqtt_socket:sock=raw backcompat=0 "
        "error=function takes 3 positional arguments but 2 were given",
    )


def test_poll_mqtt_client_tolerates_two_arg_message_callback():
    runtime_config = _runtime_config()
    transport = MQTTTransport("broker.local", 1883)
    transport.mark_connect_requested()
    adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=object(),
        modules={"mqtt_cls": _TwoArgMessageCallbackMQTTClient},
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
