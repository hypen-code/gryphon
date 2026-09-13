"""Static smoke coverage for the dependency-free hosted administration UI."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "src" / "gryphon"
_TEMPLATE = _ROOT / "templates" / "admin.html"
_SCRIPT = _ROOT / "static" / "admin.js"
_ANALYTICS = _ROOT / "static" / "analytics.js"
_SPECIFICATIONS = _ROOT / "static" / "specifications.js"
_LIFECYCLE = _ROOT / "static" / "lifecycle.js"
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
    assert scripts == [
        {"src": "/static/analytics.js", "defer": None},
        {"src": "/static/specifications.js", "defer": None},
        {"src": "/static/lifecycle.js", "defer": None},
        {"src": "/static/admin.js", "defer": None},
    ]
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
    assets = "".join(p.read_text() for p in (_SCRIPT, _ANALYTICS, _SPECIFICATIONS, _LIFECYCLE))
    script_targets = set(re.findall(r'\$\("([a-z-]+)"\)', assets))
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
    """POST-only forms bound UTF-16 fields while permitting all 128-code-point passwords."""
    forms = [attrs for tag, attrs in _document().elements if tag == "form"]
    assert forms
    assert all(attrs.get("method") == "post" for attrs in forms)
    passwords = [attrs for tag, attrs in _document().elements if tag == "input" and attrs.get("type") == "password"]
    expected = {
        "login-token": "off",
        "channel-token": "off",
        "login-password": "current-password",
        "user-password": "new-password",
        "user-confirm": "new-password",
        "current-password": "current-password",
        "new-password": "new-password",
        "confirm-password": "new-password",
    }
    assert {attrs["id"]: attrs.get("autocomplete") for attrs in passwords} == expected
    assert all(not attrs.get("value") for attrs in passwords)
    for attrs in passwords:
        if attrs["id"] not in {"login-token", "channel-token"}:
            assert attrs["maxlength"] == str(128 * 2)
        if attrs["autocomplete"] == "new-password":
            assert attrs["minlength"] == "12"


@pytest.mark.parametrize(
    "forbidden", ["innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage", "sessionStorage"]
)
def test_admin_script_unsafe_rendering_and_persistence_absent(forbidden: str) -> None:
    """Untrusted tenant/specification data and keys never use unsafe DOM or browser stores."""
    assert all(forbidden not in p.read_text() for p in (_SCRIPT, _ANALYTICS, _SPECIFICATIONS, _LIFECYCLE))


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
    script = _SCRIPT.read_text() + _SPECIFICATIONS.read_text()
    file_input = next(attrs for _, attrs in _document().elements if attrs.get("id") == "spec-file")
    assert file_input["accept"] == ".json,.yaml,.yml"
    assert "file.size > state.settings.max_spec_bytes" in script
    assert "await file.text()" in script
    assert "new TextEncoder().encode(content).length > state.settings.max_spec_bytes" in script
    assert '"POST", { name, content, read_only_filter }' in script
    assert 'new Blob([state.source.text], { type: "application/json" })' in script
    assert "URL.revokeObjectURL(url)" in script


@pytest.mark.parametrize("path", [_TEMPLATE, _SCRIPT, _ANALYTICS, _SPECIFICATIONS, _LIFECYCLE, _STYLE])
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
    mobile = style.split("@media (max-width: 640px)")[1]
    assert ".sidebar-bottom > .identity-badge { display: block; }" in mobile


def test_admin_script_named_login_and_verified_scope() -> None:
    """Verified identity gates platform controls and foreign tenant choices, not client claims."""
    script = _SCRIPT.read_text()
    assert 'api("/api/login", "POST", { username, password })' in script
    assert 'state.me = (await api("/api/me")).user' in script
    assert 'state.me?.role === "platform_admin"' in script
    assert "tenants.items.filter((tenant) => tenant.id === state.me.tenant_id)" in script
    assert 'if (key === "users" && !isAdmin())' in script
    assert "if (isAdmin()) await loadUsers()" in script
    assert '["nav-users", "new-tenant", "empty-new-tenant", "toggle-tenant", "tenant-select"]' in script
    assert '$("change-password").hidden = !state.me?.id' in script
    assert "response.status === 403" in script
    assert 'location.hash = "#overview"' in script


def test_admin_script_user_management_pagination_and_fixed_membership() -> None:
    """Account creation includes fixed membership while updates cannot reassign authority."""
    script = _SCRIPT.read_text() + _LIFECYCLE.read_text()
    assert "api(`/api/users?offset=${offset}`)" in script
    assert "state.userNext = result.next_offset" in script
    assert "loadUsers(Math.max(0, state.userOffset - 100))" in script
    assert "loadUsers(state.userNext)" in script
    assert 'const payload = { name: $("user-name").value.trim() }' in script
    assert "if (!state.editingUser) {" in script
    assert 'tenant_id: $("user-role").value === "tenant_user" ? $("user-tenant").value : null' in script
    assert '$("user-create-fields").disabled = !!user' in script
    assert "toggle.disabled = remove.disabled = self || state.busy" in script
    assert "if (!isAdmin() || user.id === state.me?.id)" in script
    assert '"PATCH", { enabled: !user.enabled }' in script
    assert "rotate exposed channel keys too" in script


def test_admin_script_password_change_reset_and_cleanup() -> None:
    """Password material is transient; self-service and administrator reset stay distinct."""
    script = _SCRIPT.read_text()
    assert "password !== $(confirmId).value" in script
    assert "[...password].length < 12 || [...password].length > 128" in script
    assert 'user ? `/api/users/${segment(user.id)}/password` : "/api/password"' in script
    assert "user ? { password } : { current_password: current, new_password: password }" in script
    assert '$("current-password").required = !user' in script
    assert "if (!user || user.id === state.me?.id) { signedOut()" in script
    assert 'clearPasswords($("user-dialog"))' in script
    assert 'clearPasswords($("password-dialog"))' in script
    assert '["user-dialog", "password-dialog"].forEach' in script
    assert 'addEventListener("close", () => { clearPasswords($(id))' in script
    assert 'window.addEventListener("pagehide", () => clearPasswords())' in script
    assert "clearDialog(dialog); dialog.close()" in script
    assert "if (state.busy) event.preventDefault(); else clearDialog(dialog)" in script
    assert 'clearPasswords(dialog); if (dialog.id === "key-dialog") clearKey()' in script
    signout = script.split("function signedOut()")[1].split("async function api(")[0]
    assert "clearKey(); clearPasswords();" in signout
    assert "state.me = null; state.users = []" in signout
    assert 'document.querySelectorAll("form").forEach((form) => form.reset())' in signout
    assert '$("client-config").textContent = ""' in signout


def test_admin_accounts_validation_matches_named_account_forms() -> None:
    """Account fields accept the backend username bound and errors stay safe and relevant."""
    fields = {attrs.get("id"): attrs for tag, attrs in _document().elements if tag == "input"}
    for identifier in ("login-username", "user-username"):
        assert fields[identifier]["maxlength"] == "128"
        assert fields[identifier]["minlength"] == "3"
        assert fields[identifier]["autocapitalize"] == "none"
    assert fields["user-name"]["maxlength"] == "128"
    script = _SCRIPT.read_text()
    assert 'result.error === "validation" ? validationMessage(path)' in script
    assert 'path === "/api/password"' in script
    assert 'path.endsWith("/password"))) return errors.invalid_password' in script
    assert "Object.hasOwn(errors, result.error)" in script
    assert "result.message" not in script


def test_admin_navigation_icons_consistent_outlined_and_decorative() -> None:
    """Every main navigation destination uses the same accessible SVG icon family."""
    template = _TEMPLATE.read_text().split('<nav aria-label="Main navigation">')[1].split("</nav>")[0]
    links = re.findall(r"<a\b[^>]*>(.*?)</a>", template, re.S)
    assert len(links) == 6
    for link in links:
        document = _Document()
        document.feed(link)
        icons = [attrs for tag, attrs in document.elements if tag == "svg"]
        assert len(icons) == 1
        assert icons[0] == {
            "class": "nav-icon",
            "width": "20",
            "height": "20",
            "viewbox": "0 0 24 24",
            "fill": "none",
            "stroke": "currentColor",
            "stroke-width": "1.75",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
            "aria-hidden": "true",
            "focusable": "false",
        }
    assert ".nav-icon { display: block; width: 20px; height: 20px; flex: 0 0 20px; }" in _STYLE.read_text()


def test_admin_specification_source_modes_and_refresh_confirmation() -> None:
    """Uploads remain supported alongside explicit URL kinds and opt-out channel replacement."""
    fields = {attrs.get("id"): attrs for _, attrs in _document().elements if "id" in attrs}
    assert fields["spec-file"]["accept"] == fields["spec-replacement"]["accept"] == ".json,.yaml,.yml"
    assert fields["spec-url"]["type"] == "url"
    assert "checked" in fields["spec-update-channels"]
    template = _TEMPLATE.read_text()
    for text in (
        "Old snapshots are retained",
        "invalidates running work",
        "catalog drift",
        "exact old version",
        "Included POST operations in bound catalogs execute automatically and may have side effects",
        "unsupported tools, transports, and contracts are reported explicitly",
        "https://coolbudget.lk/api/ucp/mcp",
        "call_tool json_body wrapper",
        "never fabricated by Gryphon",
    ):
        assert text in template
    script = _SPECIFICATIONS.read_text()
    assert '"POST", { name, url, kind, read_only_filter }' in script
    assert 'const payload = { update_channels: $("spec-update-channels").checked, read_only_filter:' in script
    assert "fetch(" not in script
    for field in ("available_operations", "filtered_operations", "unsupported_operations", "total_operations"):
        assert field in script
    assert "snapshot.tenant !== state.tenant" in script and "snapshot.epoch !== state.epoch" in script
    assert "const content = await file.text(); current(snapshot)" in script


def test_admin_discovery_checkboxes_defaults_and_automatic_post_explanation() -> None:
    """Read filters explain automatic POST effects without implying all methods are executable."""
    fields = {attrs.get("id"): attrs for _, attrs in _document().elements if "id" in attrs}
    for identifier in ("spec-read-only-filter", "spec-refresh-read-only-filter"):
        assert fields[identifier]["type"] == "checkbox"
        assert "checked" in fields[identifier]
    assert fields["channel-function-summaries"]["type"] == "checkbox"
    assert "checked" not in fields["channel-function-summaries"]
    template = _TEMPLATE.read_text()
    for text in (
        "normally GET, HEAD, and OPTIONS for OpenAPI",
        "Uncheck to include POST operations",
        "POST does not mean read-only",
        "PUT, PATCH, and DELETE remain subject to hosted execution restrictions",
        "UCP supports compatible REST and MCP bindings",
        "Include function names and descriptions in list_servers",
        "follow bounded continuation",
    ):
        assert text in template
    script = _SPECIFICATIONS.read_text()
    assert 'snapshot.filterOnly ? "filter" : "refresh"' in script
    assert "if (!snapshot.filterOnly && !remote(spec))" in script
    assert 'button.setAttribute("aria-pressed", String(enabled))' in script
    assert 'icon("filter",' in script
    assert 'include_function_summaries: $("channel-function-summaries").checked' in _SCRIPT.read_text()


def test_admin_notification_dismiss_is_accessible_and_independent_of_busy() -> None:
    """A named SVG close control never goes through the busy mutation helper."""
    fields = {attrs.get("id"): attrs for _, attrs in _document().elements if "id" in attrs}
    assert fields["dismiss-notice"]["aria-label"] == "Dismiss notification"
    assert fields["dismiss-notice"]["type"] == "button"
    assert "data-close" not in fields["dismiss-notice"]
    assert fields["notice-text"]["role"] == "status"
    assert fields["notice-text"]["aria-live"] == "polite"
    assert fields["notice-text"]["aria-atomic"] == "true"
    notice = _TEMPLATE.read_text().split('id="dismiss-notice"')[1].split("</button>")[0]
    assert "<svg " in notice and 'aria-hidden="true"' in notice and 'focusable="false"' in notice
    script = _SCRIPT.read_text()
    assert '$("dismiss-notice").addEventListener("click", dismissNotice)' in script
    assert "const NOTICE_DURATION_MS = 10000" in script
    assert "if (revision === noticeRevision) dismissNotice()" in script
    assert "clearTimeout(noticeTimer)" in script


def test_admin_logical_specs_history_and_pinned_binding_controls() -> None:
    """Lineage grouping and retained history are explicit, never name-based deduplication."""
    script = _SPECIFICATIONS.read_text()
    assert "spec.tenant_id === state.tenant" in script
    assert "root = byId.get(root.parent_id)" in script
    assert "spec.specification_id || root.parent_id || root.id" in script
    assert "row.dataset.specificationId = group.id" in script
    assert "group.versions.forEach" in script and 'showDialog("spec-history-dialog")' in script
    assert "pinned older version" in script and "pinned unavailable version" in script
    assert "specifications.bindingChoices(channel?.spec_ids || [])" in _SCRIPT.read_text()
    assert "Viewing or downloading a version never changes channel bindings" in _TEMPLATE.read_text()


def test_admin_manual_post_controls_absent_and_actor_column_preserved() -> None:
    """Retired POST review has no controls or requests; actors remain distinct from subjects."""
    assets = _SPECIFICATIONS.read_text() + _SCRIPT.read_text() + _TEMPLATE.read_text() + _STYLE.read_text()
    for stale in ("spec-permissions", "post-reads", "POST read permissions", "POST read status", "attest"):
        assert stale not in assets
    script = _SCRIPT.read_text()
    assert all(category in script for category in ("ucp_discovery", "ucp_transport", "ucp_schema"))
    assert "x-gryphon-ucp" in _SPECIFICATIONS.read_text()
    assert "sourceMetadata(source)" in script
    assert "<th>Actor</th>" in _TEMPLATE.read_text()
    assert "Unknown / legacy actor" in script
    assert "Account subject:" in script
    specs = _SPECIFICATIONS.read_text()
    assert '"DELETE", { confirm_name, confirmation_token }' in specs
    assert "confirm_name !== snapshot.preview.name" in specs
    assert 'document.createElementNS("http://www.w3.org/2000/svg", "svg")' in specs
    assert ".slice(0, 100)" in specs
    assert ".spec-action:focus-visible::after" in _STYLE.read_text()
    assert "min-height: 44px" in _STYLE.read_text()
