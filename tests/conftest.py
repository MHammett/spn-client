import pytest

from spn_client import client


@pytest.fixture(autouse=True)
def no_machine_wide_state(monkeypatch):
    """Every test starts on per-process state, whatever the machine has set.

    ``SPN_CLIENT_SHARED_STATE`` in a developer's environment would otherwise
    put every test on the developer's real shared file, and leave the next
    test reading what the last one tripped.
    """
    monkeypatch.delenv(client.SHARED_STATE_ENV, raising=False)
    client.use_shared_state(None)
    client._shared_configured = False
    yield
    client.use_shared_state(None)
    client._shared_configured = False
