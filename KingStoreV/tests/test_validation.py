from utils import validate_button_url


def test_validate_http_url():
    ok, norm = validate_button_url("https://example.com/path")
    assert ok and norm.startswith("https://example.com")


def test_validate_telegram_url():
    ok, norm = validate_button_url("https://t.me/mychan/123")
    assert ok and "t.me" in norm


def test_invalid_url():
    ok, norm = validate_button_url("ftp://example.com")
    assert not ok