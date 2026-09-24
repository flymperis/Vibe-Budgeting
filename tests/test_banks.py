import json
import re

import pytest

from banks import detect_bank

from .conftest import csrf_token, register_and_login


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Eurobank Main", "eurobank"),
        ("Πειραιώς μισθοδοσία", "piraeus"),
        ("ΕΘΝΙΚΗ", "nbg"),
        ("NBG savings", "nbg"),
        ("Alpha Bank", "alpha"),
        ("Revolut EUR", "revolut"),
        ("CrediaBank", "attica"),
        ("Μετρητά", "cash"),
    ],
)
def test_detect_bank_from_account_name(name, slug):
    assert detect_bank(name)["slug"] == slug


@pytest.mark.parametrize("name", ["Revolutionary fund", "Holiday fund", "", None])
def test_unknown_names_have_no_bank(name):
    # Only whole words match, so "Revolutionary" is not Revolut.
    assert detect_bank(name) is None


def test_dashboard_shows_bank_badges(client):
    register_and_login(client, "bank-badges-user")
    token = csrf_token(client)
    for name in ("Eurobank Main", "Holiday fund"):
        client.post(
            "/accounts/add",
            data={"name": name, "opening_balance": "10", "_csrf_token": token},
            follow_redirects=True,
        )

    html = client.get("/?panel=home").get_data(as_text=True)
    assert 'title="Eurobank"' in html
    assert "/static/banks/eurobank.png" in html

    badges = json.loads(re.search(r'id="bank-badges">(.*?)</script>', html).group(1))
    assert badges["Eurobank Main"]["slug"] == "eurobank"
    assert "Holiday fund" not in badges


def test_every_shipped_logo_is_served(client):
    import os

    import banks

    for name in sorted(os.listdir(banks._LOGO_DIR)):
        resp = client.get(f"/static/banks/{name}")
        assert resp.status_code == 200, name
        assert resp.mimetype.startswith("image/"), (name, resp.mimetype)
