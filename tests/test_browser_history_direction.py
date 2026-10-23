from types import SimpleNamespace

import pytest

from easychange.core.browser_service import BrowserService


class FakeCDP:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, timeout=5.0):
        self.calls.append((method, params))
        if method == "Page.getNavigationHistory":
            return {"entries": [{"id": 10}, {"id": 20}, {"id": 30}], "currentIndex": 1}
        return {}


@pytest.mark.parametrize("invalid_direction", [0, True, False, 0.0, None])
def test_history_rejects_invalid_direction_without_navigating(monkeypatch, invalid_direction):
    browser = BrowserService.__new__(BrowserService)
    cdp = FakeCDP()
    monkeypatch.setattr(browser, "_session", lambda _session: SimpleNamespace(cdp=cdp))
    monkeypatch.setattr(browser, "wait_ready", lambda *_args: {"ready": True})
    with pytest.raises(ValueError, match="INVALID_BROWSER_HISTORY_DIRECTION"):
        browser.history("fake", invalid_direction)
    assert cdp.calls == []


@pytest.mark.parametrize("direction,entry_id,label", [(-1, 10, "back"), (1, 30, "forward")])
def test_history_valid_direction_uses_expected_entry(monkeypatch, direction, entry_id, label):
    browser = BrowserService.__new__(BrowserService)
    cdp = FakeCDP()
    monkeypatch.setattr(browser, "_session", lambda _session: SimpleNamespace(cdp=cdp))
    monkeypatch.setattr(browser, "wait_ready", lambda *_args: {"ready": True})
    result = browser.history("fake", direction)
    assert result["moved"] is True
    assert result["direction"] == label
    assert cdp.calls[1] == ("Page.navigateToHistoryEntry", {"entryId": entry_id})
