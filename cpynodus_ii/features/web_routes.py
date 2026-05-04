"""Describe the web routes exposed by supported runtime profiles.

The route inventory is kept declarative so profile-specific route enablement
can be reasoned about and tested separately from the HTTP server plumbing.
"""

from dataclasses import dataclass

from cpynodus_ii.features.web_services import bootstrap_routes_enabled


@dataclass(frozen=True)
class WebRoute:
    """Describe one supported web route."""

    path: str
    methods: tuple
    kind: str
    description: str


def build_web_route_table(runtime_config):
    """Return the supported route set for the active runtime."""
    routes = []
    if runtime_config.web_enabled:
        routes.extend(_operational_routes(runtime_config))
    if bootstrap_routes_enabled(runtime_config):
        routes.extend(_bootstrap_routes())
    return tuple(routes)


def route_paths(runtime_config):
    """Return just the route paths for convenience in tests and adapters."""
    return tuple(route.path for route in build_web_route_table(runtime_config))


def _operational_routes(runtime_config):
    profile = str(getattr(runtime_config, "active_profile", "") or "").strip().lower()
    routes = [
        WebRoute(
            path="/",
            methods=("GET",),
            kind="status",
            description="Minimal device status page",
        ),
        WebRoute(
            path="/current-data",
            methods=("GET",),
            kind="status",
            description="Current sensor and switch snapshot",
        ),
        WebRoute(
            path="/setup",
            methods=("GET",),
            kind="ui",
            description="Setup and operational configuration view",
        ),
        WebRoute(
            path="/config",
            methods=("POST",),
            kind="config",
            description="Apply live or restart-required config updates",
        ),
        WebRoute(
            path="/set-switch-state",
            methods=("POST",),
            kind="control",
            description="Apply a live switch override",
        ),
    ]
    if profile in {"nodusweb", "sensorius"}:
        routes.append(
            WebRoute(
                path="/restart",
                methods=("POST",),
                kind="control",
                description=(
                    "Schedule a soft or hard restart for restart-required changes"
                ),
            )
        )
    return tuple(routes)


def _bootstrap_routes():
    return (
        WebRoute(
            path="/itaot-init",
            methods=("POST",),
            kind="bootstrap",
            description="Apply bootstrap provisioning and reboot",
        ),
        WebRoute(
            path="/itaot-meta",
            methods=("GET",),
            kind="bootstrap",
            description="Return compact onboarding metadata",
        ),
    )
