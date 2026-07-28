"""Tests for API client policy enforcement.

No network access: these test the guardrails, not the endpoints. Endpoint
behaviour is documented in docs/api-notes.md from live probes.
"""

from __future__ import annotations

import pytest

from osrs_index.client import (
    V1_BASE,
    V2_BASE,
    ApiError,
    ClientConfig,
    PricesClient,
    UserAgentError,
    validate_user_agent,
)

GOOD_UA = "osrs-assets/0.1 - @tester on Discord"


def test_accepts_a_descriptive_user_agent():
    assert validate_user_agent(GOOD_UA) == GOOD_UA
    assert validate_user_agent("osrs-assets - https://github.com/x/y")


def test_rejects_empty_user_agent():
    with pytest.raises(UserAgentError):
        validate_user_agent("")
    with pytest.raises(UserAgentError):
        validate_user_agent("   ")


@pytest.mark.parametrize(
    "ua",
    [
        "python-requests/2.31.0",
        "Python-urllib/3.12",
        "curl/8.5.0",
        "Java/17.0.1",
        "okhttp/4.9.0",
    ],
)
def test_rejects_agents_the_api_asks_us_not_to_send(ua):
    """Not technically enforced upstream, enforced here anyway.

    A live probe on 2026-07-27 returned HTTP 200 for both a missing UA and
    `python-requests/2.31.0`. The policy is real even though the control is
    not, and the cost of being blocked is the whole product.
    """
    with pytest.raises(UserAgentError):
        validate_user_agent(ua)


def test_rejects_a_user_agent_with_no_contact_route():
    with pytest.raises(UserAgentError):
        validate_user_agent("osrs-assets/0.1")


def test_client_validates_at_construction():
    with pytest.raises(UserAgentError):
        PricesClient(ClientConfig(user_agent="curl/8.5.0"))


def test_unsupported_aggregate_step_is_rejected():
    """/4h, /1d and /7d return 404 upstream; fail before the request."""
    client = PricesClient(ClientConfig(user_agent=GOOD_UA))
    with pytest.raises(ValueError):
        client.aggregate("4h")  # type: ignore[arg-type]


def test_v1_timeseries_refuses_to_run_against_a_v2_base():
    """The v1/v2 divergence trap.

    v1 /timeseries takes `timestep=`; v2 takes `lookback=` and rejects
    `timestep` outright. Silently pointing v1 code at a v2 base is the most
    common way to break a collector during an "upgrade".
    """
    client = PricesClient(ClientConfig(user_agent=GOOD_UA, base=V2_BASE))
    with pytest.raises(ApiError, match="v1 contract"):
        client.timeseries(4151)


def test_default_base_is_pinned_to_v1():
    assert ClientConfig(user_agent=GOOD_UA).base == V1_BASE


# ------------------------------------------------- offline commands are offline


def test_settings_do_not_demand_a_user_agent(monkeypatch):
    """Only `collect` and `backfill` reach the network.

    Requiring a contact route to parse committed NDJSON is a gratuitous
    failure, and it is the one people hit in CI: the natural pipeline opens
    with an offline `restore` step, which used to abort before reading a
    single file.
    """
    from osrs_index.config import Settings

    monkeypatch.delenv("OSRS_INDEX_USER_AGENT", raising=False)
    settings = Settings.from_env()
    assert settings.user_agent == ""
    assert settings.db_path  # usable for every offline command


def test_require_user_agent_raises_with_an_actionable_message(monkeypatch):
    from osrs_index.config import Settings

    monkeypatch.delenv("OSRS_INDEX_USER_AGENT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        Settings.from_env().require_user_agent()
    message = str(excinfo.value)
    assert "not a secret" in message
    assert "env:" in message


def test_require_user_agent_returns_a_configured_value(monkeypatch):
    from osrs_index.config import Settings

    monkeypatch.setenv("OSRS_INDEX_USER_AGENT", GOOD_UA)
    assert Settings.from_env().require_user_agent() == GOOD_UA


def test_blank_user_agent_is_treated_as_unset(monkeypatch):
    from osrs_index.config import Settings

    monkeypatch.setenv("OSRS_INDEX_USER_AGENT", "   ")
    with pytest.raises(SystemExit):
        Settings.from_env().require_user_agent()
