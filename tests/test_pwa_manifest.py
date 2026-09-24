import json
import os

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def test_manifest_served_without_login(client):
    resp = client.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.content_type == "application/manifest+json"


def test_manifest_is_valid_standalone_manifest_with_icons_on_disk(client):
    resp = client.get("/static/manifest.webmanifest")
    manifest = json.loads(resp.get_data(as_text=True))

    assert manifest["display"] == "standalone"
    assert manifest["name"] == "Vibe Budgeting"
    assert manifest["icons"], "manifest should declare at least one icon"

    for icon in manifest["icons"]:
        icon_path = os.path.join(STATIC_DIR, *icon["src"].split("/"))
        assert os.path.isfile(icon_path), f"missing icon file: {icon_path}"


def test_login_page_links_manifest_and_apple_touch_icon(client):
    resp = client.get("/login")
    html = resp.get_data(as_text=True)

    assert 'rel="manifest"' in html
    assert "manifest.webmanifest" in html
    assert 'rel="apple-touch-icon"' in html
    assert "apple-touch-icon.png" in html
