"""Static smoke coverage for the dependency-free hosted administration UI."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "src" / "gryphon"
_TEMPLATE = _ROOT / "templates" / "admin.html"
_SCRIPT = _ROOT / "static" / "admin.js"
_STYLE = _ROOT / "static" / "admin.css"


class _Document(HTMLParser):
    """Collect HTML elements without executing scripts or resolving resources."""

    def __init__(self) -> None:
        """Initialize a per-test element collection."""
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Retain attributes for CSP and accessible-name checks."""
        self.elements.append((tag, dict(attrs)))


def _document() -> _Document:
    """Parse the shipped template using the standard library only."""
    document = _Document()
    document.feed(_TEMPLATE.read_text())
    return document


def test_admin_template_assets_same_origin_and_external() -> None:
    """Scripts and styles require no CDN or inline CSP exception."""
    elements = _document().elements
    scripts = [attrs for tag, attrs in elements if tag == "script"]
    styles = [attrs for tag, attrs in elements if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert scripts == [{"src": "/static/admin.js", "defer": None}]
    assert styles == [{"rel": "stylesheet", "href": "/static/admin.css"}]
    assert not any(tag == "style" for tag, _ in elements)
    assert all(not key.startswith("on") and key != "style" for _, attrs in elements for key in attrs)
    assert all(not body.strip() for body in re.findall(r"<script[^>]*>(.*?)</script>", _TEMPLATE.read_text(), re.S))


def test_admin_template_favicon_self_contained_no_missing_request() -> None:
    """The static SVG icon avoids a failing implicit favicon request without new assets."""
    icons = [attrs for tag, attrs in _document().elements if tag == "link" and attrs.get("rel") == "icon"]
    assert len(icons) == 1
    href = icons[0].get("href") or ""
    assert href.startswith("data:image/svg+xml,%3Csvg ")
    assert href.endswith("%3C/svg%3E")
    assert "script" not in href and "style" not in href


def test_admin_template_ids_unique_and_script_targets_present() -> None:
    """Every literal JavaScript target resolves to one shipped element."""
    identifiers = [attrs["id"] for _, attrs in _document().elements if "id" in attrs]
    assert len(identifiers) == len(set(identifiers))
    script_targets = set(re.findall(r'\$\("([a-z-]+)"\)', _SCRIPT.read_text()))
    assert script_targets <= set(identifiers)


def test_admin_template_fields_and_dialogs_accessibly_named() -> None:
    """Form fields and native keyboard-trapping dialogs have labels."""
    elements = _document().elements
    labels = {attrs["for"] for tag, attrs in elements if tag == "label" and "for" in attrs}
    ids = {attrs["id"] for _, attrs in elements if "id" in attrs}
    for tag, attrs in elements:
        if tag in {"input", "select"} and attrs.get("type") != "checkbox":
            assert attrs.get("id") in labels
        if tag == "dialog":
            assert attrs.get("aria-labelledby") in ids
        for reference in ("aria-describedby", "aria-labelledby"):
            if reference in attrs:
                assert set((attrs[reference] or "").split()) <= ids
    assert any(attrs.get("aria-live") == "polite" for _, attrs in elements)


def test_admin_template_login_no_get_credential_submission() -> None:
    """Even without JavaScript, a form must not place credentials in a URL."""
    forms = [attrs for tag, attrs in _document().elements if tag == "form"]
    assert forms
    assert all(attrs.get("method") == "post" for attrs in forms)
    passwords = [attrs for tag, attrs in _document().elements if tag == "input" and attrs.get("type") == "password"]
    assert {attrs["id"] for attrs in passwords} == {"login-token", "channel-token"}
    assert all(attrs.get("autocomplete") == "off" for attrs in passwords)


@pytest.mark.parametrize(
    "forbidden", ["innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage", "sessionStorage"]
)
def test_admin_script_unsafe_rendering_and_persistence_absent(forbidden: str) -> None:
    """Untrusted tenant/specification data and keys never use unsafe DOM or browser stores."""
    assert forbidden not in _SCRIPT.read_text()


def test_admin_script_api_session_and_csrf_contract() -> None:
    """One bounded same-origin request helper carries session cookies and CSRF headers."""
    script = _SCRIPT.read_text()
    assert script.count("await fetch(") == 1
    assert 'credentials: "same-origin"' in script
    assert 'cache: "no-store"' in script
    assert 'redirect: "error"' in script
    assert 'headers["X-CSRF-Token"] = state.csrf' in script
    assert "response.status === 401" in script
    assert 'if (path !== "/api/login") signedOut()' in script
    assert 'api("/api/login", "POST", { token })' in script
    assert 'api("/api/logout", "POST", {})' in script
    assert 'api("/api/session")' in script


def test_admin_script_restricted_mode_clears_library_authority() -> None:
    """Both the controls and serialized request remove imports in restricted mode."""
    script = _SCRIPT.read_text()
    assert '$("library-fieldset").disabled = !docker' in script
    assert 'if (!docker) document.querySelectorAll("#library-options input")' in script
    assert 'allowed_imports: $("sandbox-mode").value === "docker" ? selected("library-options") : []' in script
    assert "state.settings.allowed_imports.map" in script
    assert "!state.settings.docker_enabled" in script
    assert "no network, API access, broker, or credentials" in script


def test_admin_script_key_cleanup_and_secret_free_client_configuration() -> None:
    """Rotation exposes a transient field; close/navigation clears it and snippets omit it."""
    script = _SCRIPT.read_text()
    assert '$("channel-token").value = result.token' in script
    assert '$("channel-token").value = ""' in script
    assert '$("key-dialog").addEventListener("close", clearKey)' in script
    assert 'window.addEventListener("pagehide", clearKey)' in script
    assert '"Bearer <YOUR_CHANNEL_TOKEN>"' in script
    assert "headers: { Authorization: authorization }" in script
    assert "`${location.origin}/mcp/${segment(channel.id)}`" in script
    connect = script.split("function connect(channel)")[1].split("async function copy(")[0]
    assert "YOUR_CHANNEL_TOKEN" in connect and "channel-token" not in connect


def test_admin_script_upload_limits_and_blob_cleanup() -> None:
    """The browser reads bounded local documents and revokes temporary download URLs."""
    script = _SCRIPT.read_text()
    file_input = next(attrs for _, attrs in _document().elements if attrs.get("id") == "spec-file")
    assert file_input["accept"] == ".json,.yaml,.yml"
    assert "file.size > state.settings.max_spec_bytes" in script
    assert "await file.text()" in script
    assert "new TextEncoder().encode(content).length > state.settings.max_spec_bytes" in script
    assert '"POST", { name, content }' in script
    assert 'new Blob([state.source.text], { type: "application/json" })' in script
    assert "URL.revokeObjectURL(url)" in script


@pytest.mark.parametrize("path", [_TEMPLATE, _SCRIPT, _STYLE])
def test_admin_assets_size_limits_respected(path: Path) -> None:
    """All shipped assets remain within the repository file-size constraint."""
    assert len(path.read_text().splitlines()) <= 400


def test_admin_style_responsive_and_keyboard_focus_rules() -> None:
    """Responsive navigation and visible focus do not need script-injected styles."""
    style = _STYLE.read_text()
    assert "@media (max-width: 640px)" in style
    assert "@media (max-width: 900px)" in style
    assert ":focus-visible" in style
    assert "prefers-reduced-motion" in style
    assert "dialog::backdrop" in style
    assert "url(" not in style
