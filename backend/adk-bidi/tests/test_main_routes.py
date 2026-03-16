from app.main import app


def test_http_routes_are_registered():
    http_paths = {
        route.path
        for route in app.routes
        if hasattr(route, "methods")
    }
    assert "/health" in http_paths
    assert "/debug" in http_paths
    assert "/memory" in http_paths
    assert "/memory/clear" in http_paths
    assert "/memory/profile/{key}" in http_paths


def test_websocket_route_is_registered():
    ws_paths = {
        route.path
        for route in app.routes
        if route.__class__.__name__ == "APIWebSocketRoute"
    }
    assert "/ws" in ws_paths
