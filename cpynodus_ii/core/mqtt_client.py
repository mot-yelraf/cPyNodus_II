"""Wrap the configured MiniMQTT client in a firmware-friendly adapter.

This module centralizes connect, poll, publish, subscribe, and disconnect
behavior so the main application can reason about MQTT state through a small
and testable interface.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MQTTClientAdapter:
    """Describe the current MQTT client binding state."""

    phase: str
    driver_kind: str
    broker: str
    port: int
    broker_targets: tuple = ()
    active_broker: str = ""
    resolved_broker_ip: str = ""
    client: object | None = None
    client_class: object | None = None
    client_kwargs: dict | None = None
    published_index: int = 0
    subscription_index: int = 0
    errors: tuple = ()


@dataclass(frozen=True)
class MQTTClientSyncResult:
    """Describe one adapter operation."""

    phase: str
    adapter: MQTTClientAdapter
    published_count: int = 0
    subscribed_count: int = 0
    received_count: int = 0
    errors: tuple = ()


def build_mqtt_client_adapter(
    runtime_config,
    *,
    socket_pool=None,
    ssl_context=None,
    modules=None,
):
    """Build a clean-room MQTT client adapter when runtime MQTT is enabled."""
    if not runtime_config.mqtt_enabled:
        return MQTTClientAdapter(
            phase="inactive",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=runtime_config.mqtt.connection_targets,
            errors=(),
        )

    if socket_pool is None:
        return MQTTClientAdapter(
            phase="unavailable",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=runtime_config.mqtt.connection_targets,
            errors=("socket_pool_unavailable",),
        )

    client_class = _resolve_mqtt_class(modules)
    if client_class is None:
        return MQTTClientAdapter(
            phase="unavailable",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=runtime_config.mqtt.connection_targets,
            errors=("mqtt_client_module_unavailable",),
        )

    broker_targets = runtime_config.mqtt.connection_targets or (
        runtime_config.mqtt.preferred_host,
    )
    mqtt_socket_pool = _wrap_minimqtt_socket_pool(socket_pool)
    kwargs = {
        "socket_pool": mqtt_socket_pool,
        "port": runtime_config.mqtt.port,
        "keep_alive": 60,
    }
    if runtime_config.mqtt.use_tls or runtime_config.mqtt.port == 8883:
        kwargs["ssl_context"] = ssl_context
    if runtime_config.mqtt.username:
        kwargs["username"] = runtime_config.mqtt.username
    if runtime_config.mqtt.password:
        kwargs["password"] = runtime_config.mqtt.password

    try:
        client = _instantiate_client(client_class, dict(kwargs), broker_targets[0])
    except Exception as exc:
        return MQTTClientAdapter(
            phase="error",
            driver_kind="adafruit_minimqtt",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=broker_targets,
            errors=("mqtt_client_init_failed", str(exc)),
        )

    return MQTTClientAdapter(
        phase="ready",
        driver_kind="adafruit_minimqtt",
        broker=runtime_config.mqtt.preferred_host,
        port=runtime_config.mqtt.port,
        broker_targets=broker_targets,
        active_broker=broker_targets[0],
        client=client,
        client_class=client_class,
        client_kwargs=dict(kwargs),
        errors=(),
    )


def connect_mqtt_client(adapter, transport, *, preflight=True):
    """Connect the bound client and wire inbound message delivery."""
    if adapter.phase != "ready" or adapter.client is None:
        return MQTTClientSyncResult(
            phase=adapter.phase,
            adapter=adapter,
            errors=adapter.errors,
        )

    _bind_on_message(adapter.client, transport)
    errors = []
    active_adapter = adapter
    connected = False
    for index, broker in enumerate(
        adapter.broker_targets or (adapter.active_broker or adapter.broker,)
    ):
        resolve_error, resolved_ip = _resolve_broker_target(active_adapter, broker)
        if preflight and resolve_error:
            errors.append(resolve_error)
            transport.mark_disconnected()
            continue
        connect_broker = _connect_broker_for_target(
            active_adapter, broker, resolved_ip, preflight
        )
        if index > 0 or connect_broker != active_adapter.active_broker:
            try:
                client = _instantiate_client(
                    adapter.client_class,
                    dict(adapter.client_kwargs or {}),
                    connect_broker,
                )
            except Exception as exc:
                errors.append("mqtt_client_init_failed:{}".format(exc))
                continue
            active_adapter = MQTTClientAdapter(
                phase=adapter.phase,
                driver_kind=adapter.driver_kind,
                broker=adapter.broker,
                port=adapter.port,
                broker_targets=adapter.broker_targets,
                active_broker=broker,
                resolved_broker_ip=resolved_ip,
                client=client,
                client_class=adapter.client_class,
                client_kwargs=dict(adapter.client_kwargs or {}),
                published_index=adapter.published_index,
                subscription_index=adapter.subscription_index,
                errors=adapter.errors,
            )
        _bind_on_message(active_adapter.client, transport)
        try:
            active_adapter.client.connect()
            _ensure_minimqtt_socket_compat(active_adapter.client)
            transport.mark_connected()
            connected = True
            break
        except Exception as exc:
            errors.append("mqtt_connect_failed:{}:{}".format(broker, exc))
            transport.mark_disconnected()

    if not connected:
        return MQTTClientSyncResult(
            phase="error",
            adapter=active_adapter,
            errors=tuple(errors) or ("mqtt_connect_failed",),
        )

    return MQTTClientSyncResult(
        phase="connected",
        adapter=MQTTClientAdapter(
            phase=active_adapter.phase,
            driver_kind=active_adapter.driver_kind,
            broker=active_adapter.broker,
            port=active_adapter.port,
            broker_targets=active_adapter.broker_targets,
            active_broker=active_adapter.active_broker,
            resolved_broker_ip=resolved_ip,
            client=active_adapter.client,
            client_class=active_adapter.client_class,
            client_kwargs=active_adapter.client_kwargs,
            published_index=active_adapter.published_index,
            subscription_index=active_adapter.subscription_index,
            errors=active_adapter.errors,
        ),
        errors=(),
    )


def sync_transport_to_client(adapter, transport):
    """Flush newly queued subscriptions and publishes to the bound client."""
    if adapter.phase != "ready" or adapter.client is None or not transport.connected:
        return MQTTClientSyncResult(
            phase="skipped",
            adapter=adapter,
            errors=adapter.errors
            if adapter.phase != "ready"
            else ("transport_not_connected",),
        )

    subscribed_count = 0
    published_count = 0
    client = adapter.client
    for message in transport.published_messages[adapter.published_index :]:
        payload = _serialize_payload(message.payload)
        try:
            client.publish(message.topic, payload, retain=message.retain)
        except Exception as exc:
            transport.mark_disconnected()
            drop_failed = not bool(message.retain)
            transport.compact(
                published_keep_from=adapter.published_index
                + published_count
                + (1 if drop_failed else 0),
                subscriptions_keep_from=adapter.subscription_index + subscribed_count,
            )
            return MQTTClientSyncResult(
                phase="error",
                adapter=MQTTClientAdapter(
                    phase=adapter.phase,
                    driver_kind=adapter.driver_kind,
                    broker=adapter.broker,
                    port=adapter.port,
                    broker_targets=adapter.broker_targets,
                    active_broker=adapter.active_broker,
                    resolved_broker_ip=adapter.resolved_broker_ip,
                    client=adapter.client,
                    client_class=adapter.client_class,
                    client_kwargs=adapter.client_kwargs,
                    published_index=0,
                    subscription_index=0,
                    errors=adapter.errors,
                ),
                published_count=published_count,
                subscribed_count=subscribed_count,
                errors=(
                    "mqtt_publish_failed:{}:bytes={}:{}".format(
                        message.topic,
                        _payload_size(payload),
                        exc,
                    ),
                ),
            )
        published_count += 1

    if subscribed_count or published_count:
        transport.compact(
            published_keep_from=adapter.published_index + published_count,
            subscriptions_keep_from=adapter.subscription_index + subscribed_count,
        )
    if published_count:
        settle_error = _settle_client_after_publish(client)
        if settle_error:
            transport.mark_disconnected()
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                published_count=published_count,
                subscribed_count=subscribed_count,
                errors=(settle_error,),
            )
        return MQTTClientSyncResult(
            phase="synced",
            adapter=MQTTClientAdapter(
                phase=adapter.phase,
                driver_kind=adapter.driver_kind,
                broker=adapter.broker,
                port=adapter.port,
                broker_targets=adapter.broker_targets,
                active_broker=adapter.active_broker,
                resolved_broker_ip=adapter.resolved_broker_ip,
                client=adapter.client,
                client_class=adapter.client_class,
                client_kwargs=adapter.client_kwargs,
                published_index=0,
                subscription_index=0,
                errors=adapter.errors,
            ),
            published_count=published_count,
            subscribed_count=0,
            errors=(),
        )

    pending_subscriptions = transport.subscriptions[adapter.subscription_index :]
    pending_subscription_count = len(pending_subscriptions)
    for topic in pending_subscriptions:
        try:
            client.subscribe(topic)
        except Exception as exc:
            if subscribed_count:
                transport.compact(
                    published_keep_from=0,
                    subscriptions_keep_from=(
                        adapter.subscription_index + subscribed_count
                    ),
                )
            transport.mark_disconnected()
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                published_count=published_count,
                subscribed_count=subscribed_count,
                errors=(
                    "mqtt_subscribe_failed:topic={}:index={}/{}:{}".format(
                        topic,
                        subscribed_count,
                        pending_subscription_count,
                        exc,
                    ),
                ),
            )
        subscribed_count += 1

    if subscribed_count:
        transport.compact(
            published_keep_from=0,
            subscriptions_keep_from=adapter.subscription_index + subscribed_count,
        )

    updated_adapter = MQTTClientAdapter(
        phase=adapter.phase,
        driver_kind=adapter.driver_kind,
        broker=adapter.broker,
        port=adapter.port,
        broker_targets=adapter.broker_targets,
        active_broker=adapter.active_broker,
        resolved_broker_ip=adapter.resolved_broker_ip,
        client=adapter.client,
        client_class=adapter.client_class,
        client_kwargs=adapter.client_kwargs,
        published_index=0,
        subscription_index=0,
        errors=adapter.errors,
    )
    return MQTTClientSyncResult(
        phase="synced",
        adapter=updated_adapter,
        published_count=published_count,
        subscribed_count=subscribed_count,
        errors=(),
    )


def poll_mqtt_client(adapter, transport):
    """Poll the client loop so inbound MQTT messages can reach the transport."""
    if adapter.phase != "ready" or adapter.client is None or not transport.connected:
        return MQTTClientSyncResult(
            phase="skipped",
            adapter=adapter,
            errors=adapter.errors
            if adapter.phase != "ready"
            else ("transport_not_connected",),
        )

    before = len(transport.received_messages)
    loop = getattr(adapter.client, "loop", None)
    if callable(loop):
        _ensure_minimqtt_socket_compat(adapter.client)
        timeout = _poll_timeout_for_client(adapter.client)
        try:
            loop(timeout=timeout)
        except TypeError as exc:
            if not _is_loop_timeout_signature_error(exc):
                if _is_minimqtt_wrapped_socket_error(exc):
                    transport.mark_disconnected()
                    return MQTTClientSyncResult(
                        phase="error",
                        adapter=adapter,
                        received_count=0,
                        errors=(_minimqtt_socket_error(adapter.client, exc),),
                    )
                if _is_callback_arity_error(exc):
                    transport.mark_disconnected()
                    return MQTTClientSyncResult(
                        phase="error",
                        adapter=adapter,
                        received_count=0,
                        errors=("mqtt_poll_callback_failed:{}".format(exc),),
                    )
                raise
            loop()
        except ValueError:
            try:
                loop(timeout=max(1.0, timeout))
            except TypeError as exc:
                if not _is_loop_timeout_signature_error(exc):
                    if _is_minimqtt_wrapped_socket_error(exc):
                        transport.mark_disconnected()
                        return MQTTClientSyncResult(
                            phase="error",
                            adapter=adapter,
                            received_count=0,
                            errors=(_minimqtt_socket_error(adapter.client, exc),),
                        )
                    if _is_callback_arity_error(exc):
                        transport.mark_disconnected()
                        return MQTTClientSyncResult(
                            phase="error",
                            adapter=adapter,
                            received_count=0,
                            errors=("mqtt_poll_callback_failed:{}".format(exc),),
                        )
                    raise
                loop()
        except OSError as exc:
            transport.mark_disconnected()
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=("mqtt_poll_failed:{}".format(exc),),
            )
    received_count = len(transport.received_messages) - before
    return MQTTClientSyncResult(
        phase="polled",
        adapter=adapter,
        received_count=max(0, received_count),
        errors=(),
    )


def _settle_client_after_publish(client):
    loop = getattr(client, "loop", None)
    if not callable(loop):
        return ""
    _ensure_minimqtt_socket_compat(client)
    timeout = _poll_timeout_for_client(client)
    try:
        loop(timeout=timeout)
    except TypeError as exc:
        if not _is_loop_timeout_signature_error(exc):
            if _is_minimqtt_wrapped_socket_error(exc):
                return _minimqtt_socket_error(client, exc)
            if _is_callback_arity_error(exc):
                return "mqtt_publish_settle_callback_failed:{}".format(exc)
            raise
        loop()
    except ValueError:
        try:
            loop(timeout=max(1.0, timeout))
        except TypeError as exc:
            if not _is_loop_timeout_signature_error(exc):
                if _is_minimqtt_wrapped_socket_error(exc):
                    return _minimqtt_socket_error(client, exc)
                if _is_callback_arity_error(exc):
                    return "mqtt_publish_settle_callback_failed:{}".format(exc)
                raise
            loop()
    except OSError as exc:
        return "mqtt_publish_settle_failed:{}".format(exc)
    return ""


def disconnect_mqtt_client(adapter, transport, runtime_config):
    """Publish retained offline status, flush it, then disconnect the client."""
    from cpynodus_ii.features.publish_cycle import publish_shutdown_cycle

    if adapter.phase != "ready" or adapter.client is None:
        transport.mark_disconnected()
        return MQTTClientSyncResult(
            phase=adapter.phase,
            adapter=adapter,
            errors=adapter.errors,
        )

    sync_result = MQTTClientSyncResult(
        phase="skipped",
        adapter=adapter,
        errors=(),
    )
    try:
        publish_shutdown_cycle(transport, runtime_config)
        sync_result = sync_transport_to_client(adapter, transport)
    except OSError as exc:
        transport.mark_disconnected()
        sync_result = MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            errors=("mqtt_disconnect_flush_failed:{}".format(exc),),
        )
    try:
        disconnect = getattr(adapter.client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except OSError as exc:
                sync_result = MQTTClientSyncResult(
                    phase="error",
                    adapter=sync_result.adapter,
                    published_count=sync_result.published_count,
                    subscribed_count=sync_result.subscribed_count,
                    errors=sync_result.errors
                    + ("mqtt_disconnect_failed:{}".format(exc),),
                )
    finally:
        transport.mark_disconnected()
    return MQTTClientSyncResult(
        phase="disconnected",
        adapter=sync_result.adapter,
        published_count=sync_result.published_count,
        subscribed_count=sync_result.subscribed_count,
        errors=sync_result.errors,
    )


def _resolve_mqtt_class(modules):
    if isinstance(modules, dict) and modules.get("mqtt_cls") is not None:
        return modules["mqtt_cls"]
    try:
        from adafruit_minimqtt.adafruit_minimqtt import MQTT  # type: ignore
    except ImportError:
        return None
    return MQTT


def _instantiate_client(client_class, kwargs, broker):
    kwargs["broker"] = broker
    return client_class(**kwargs)


class _MiniMQTTSocketPoolCompat:
    def __init__(self, socket_pool):
        self._socket_pool = socket_pool

    def socket(self, *args, **kwargs):
        socket_obj = self._socket_pool.socket(*args, **kwargs)
        return _MiniMQTTSocketCompat(socket_obj)

    def __getattr__(self, name):
        return getattr(self._socket_pool, name)


class _MiniMQTTSocketCompat:
    def __init__(self, socket_obj):
        self._socket_obj = socket_obj

    def send(self, buffer):
        send = getattr(self._socket_obj, "send")
        try:
            return send(buffer)
        except TypeError as exc:
            if not _is_socket_nbytes_required_error(exc):
                raise
            return send(buffer, len(buffer))

    def recv_into(self, buffer, nbytes=None):
        if nbytes is None:
            return self._recv_into_compatible(
                self._socket_obj,
                buffer,
                len(buffer),
                True,
            )
        return self._recv_into_compatible(self._socket_obj, buffer, nbytes, False)

    def recv(self, nbytes):
        recv = getattr(self._socket_obj, "recv")
        return recv(nbytes)

    def _recv_into_from_recv(self, socket_obj, buffer, nbytes):
        recv = getattr(socket_obj, "recv")
        data = recv(nbytes)
        count = len(data or b"")
        if count:
            buffer[:count] = data
        return count

    def _recv_into_compatible(self, socket_obj, buffer, nbytes, prefer_single_arg):
        recv_into = getattr(socket_obj, "recv_into", None)
        if callable(recv_into):
            if prefer_single_arg:
                try:
                    return recv_into(buffer)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
                try:
                    return recv_into(buffer, nbytes)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
            else:
                try:
                    return recv_into(buffer, nbytes)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
                try:
                    return recv_into(buffer)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise

        raw_socket = _inner_socket_obj(socket_obj)
        if raw_socket is not None and raw_socket is not socket_obj:
            return self._recv_into_compatible(raw_socket, buffer, nbytes, False)

        return self._recv_into_from_recv(socket_obj, buffer, nbytes)

    def __getattr__(self, name):
        return getattr(self._socket_obj, name)


def _wrap_minimqtt_socket_pool(socket_pool):
    if socket_pool is None or isinstance(socket_pool, _MiniMQTTSocketPoolCompat):
        return socket_pool
    if not callable(getattr(socket_pool, "socket", None)):
        return socket_pool
    return _MiniMQTTSocketPoolCompat(socket_pool)


def _ensure_minimqtt_socket_compat(client):
    socket_obj = getattr(client, "_sock", None)
    if socket_obj is None or isinstance(socket_obj, _MiniMQTTSocketCompat):
        return
    try:
        client._sock = _MiniMQTTSocketCompat(socket_obj)
        client._backwards_compatible_sock = False
    except Exception:
        pass


def _inner_socket_obj(socket_obj):
    for attr_name in ("_socket", "_sock", "_socket_obj"):
        try:
            inner = getattr(socket_obj, attr_name, None)
        except Exception:
            inner = None
        if inner is not None and inner is not socket_obj:
            return inner
    return None


def _poll_timeout_for_client(client):
    for attr_name in (
        "socket_timeout",
        "_socket_timeout",
        "recv_timeout",
        "_recv_timeout",
    ):
        value = getattr(client, attr_name, None)
        if _is_positive_number(value):
            return max(0.1, min(1.0, float(value)))
    socket_obj = getattr(client, "_socket", None)
    for attr_name in ("timeout", "_timeout"):
        value = getattr(socket_obj, attr_name, None)
        if _is_positive_number(value):
            return max(0.1, min(1.0, float(value)))
    return 1.0


def _preflight_broker_target(adapter, broker):
    error, _ip_address = _resolve_broker_target(adapter, broker)
    return error


def preflight_mqtt_broker(adapter, broker=None):
    """Resolve an MQTT broker target without opening a client socket."""
    target = (
        broker
        or getattr(adapter, "active_broker", "")
        or getattr(adapter, "broker", "")
    )
    return _resolve_broker_target(adapter, target)


def _connect_broker_for_target(adapter, broker, resolved_ip, preflight):
    """Return the broker address MiniMQTT should open directly."""
    if not preflight or not resolved_ip:
        return broker
    if _looks_like_ip_literal(str(broker or "")):
        return broker
    if isinstance(adapter.client_kwargs, dict) and adapter.client_kwargs.get(
        "ssl_context"
    ):
        return broker
    return resolved_ip


def _resolve_broker_target(adapter, broker):
    broker_text = str(broker or "").strip()
    if not broker_text:
        return "mqtt_connect_failed:empty_broker", ""
    if _looks_like_ip_literal(broker_text):
        return "", broker_text

    socket_pool = None
    if isinstance(adapter.client_kwargs, dict):
        socket_pool = adapter.client_kwargs.get("socket_pool")
    if socket_pool is None:
        return "", ""

    getaddrinfo = getattr(socket_pool, "getaddrinfo", None)
    if not callable(getaddrinfo):
        return "", ""

    try:
        resolved = getaddrinfo(broker_text, adapter.port)
    except Exception as exc:
        return "mqtt_resolve_failed:{}:{}".format(broker_text, exc), ""
    return "", _ip_from_getaddrinfo_result(resolved)


def _ip_from_getaddrinfo_result(resolved):
    try:
        first = resolved[0]
        sockaddr = first[-1]
        return str(sockaddr[0] or "").strip()
    except Exception:
        return ""


def _looks_like_ip_literal(value):
    text = str(value or "").strip()
    if not text:
        return False
    parts = text.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    if ":" in text:
        return True
    return False


def _is_positive_number(value):
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def _is_loop_timeout_signature_error(exc):
    text = str(exc or "").lower()
    if "keyword" in text:
        return True
    if "unexpected" in text and "timeout" in text:
        return True
    if "positional argument" in text and "timeout" in text:
        return True
    return False


def _is_callback_arity_error(exc):
    text = str(exc or "").lower()
    if "positional argument" in text and "given" in text:
        return True
    if "required positional argument" in text:
        return True
    return False


def _is_minimqtt_wrapped_socket_error(exc):
    text = str(exc or "").lower()
    return text == "function takes 3 positional arguments but 2 were given"


def _minimqtt_socket_error(client, exc):
    socket_obj = getattr(client, "_sock", None)
    socket_state = "wrapped" if isinstance(socket_obj, _MiniMQTTSocketCompat) else "raw"
    backwards = 1 if getattr(client, "_backwards_compatible_sock", False) else 0
    return "mqtt_poll_failed:minimqtt_socket:sock={} backcompat={} error={}".format(
        socket_state,
        backwards,
        exc,
    )


def _is_socket_nbytes_required_error(exc):
    text = str(exc or "").lower()
    if ("recv_into" in text or "send" in text) and "argument" in text:
        return True
    if "positional argument" in text and "given" in text and "takes" in text:
        return True
    return False


def _bind_on_message(client, transport):
    def _on_message(*args):
        if len(args) >= 3:
            _, topic, message = args[-3:]
        elif len(args) == 2:
            topic, message = args
        else:
            raise TypeError("mqtt_on_message_callback_args_invalid")
        transport.receive(topic, _coerce_payload_text(message))

    try:
        client.on_message = _on_message
    except Exception:
        pass

    def _on_disconnect(*_args):
        transport.mark_disconnected()

    try:
        client.on_disconnect = _on_disconnect
    except Exception:
        pass


def _serialize_payload(payload):
    import json

    if isinstance(payload, str):
        return payload
    return json.dumps(dict(payload or {}), separators=(",", ":"))


def _payload_size(payload):
    try:
        return len(payload)
    except Exception:
        return 0


def _coerce_payload_text(message):
    if isinstance(message, bytes):
        try:
            return message.decode("utf-8")
        except Exception:
            return message.decode("utf-8", errors="ignore")
    return str(message)
