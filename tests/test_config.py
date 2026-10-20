import pytest

from discord_codex_bot.config import load_config, local_params, parse_id_set


def test_parses_ids_without_duplicates() -> None:
    assert parse_id_set("111111111111111111, 111111111111111111", "TEST") == frozenset(
        {111111111111111111}
    )


def test_rejects_missing_allowlist_and_malformed_ids() -> None:
    base = {
        "DISCORD_TOKEN": "test-token",
        "DISCORD_APPLICATION_ID": "123456789012345678",
    }
    with pytest.raises(ValueError, match="at least one"):
        load_config({**base, "ALLOWED_GUILD_IDS": ""})
    with pytest.raises(ValueError, match="invalid"):
        load_config({**base, "ALLOWED_GUILD_IDS": "not-an-id"})


def test_loads_safe_operational_defaults() -> None:
    config = load_config(
        {
            "DISCORD_TOKEN": "test-token",
            "DISCORD_APPLICATION_ID": "123456789012345678",
            "ALLOWED_GUILD_IDS": "111111111111111111",
        }
    )
    assert config.codex_model == "gpt-6-luna"
    assert config.codex_reasoning_effort == "medium"
    assert config.max_queued_jobs == 10
    assert not config.tracking_enabled
    assert config.tracking_interval_seconds == 900
    # 0 = no gate: a spent subscription falls back instead of failing, so batch work no longer
    # has to hold quota back.
    assert config.tracking_min_remaining_percent == 0
    assert config.consolidate_min_remaining_percent == 0
    assert config.tracking_reasoning_effort == "high"
    assert config.codex_fallback_model == "agy:gemini-3.8-flash|medium"


def test_tracking_config_validates_switch_and_usage_gate() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(
        {
            **base,
            "TRACKING_ENABLED": "true",
            "TRACKING_INTERVAL_MINUTES": "5",
            "TRACKING_MIN_REMAINING_PERCENT": "65",
        }
    )
    assert config.tracking_enabled
    assert config.tracking_interval_seconds == 300
    assert config.tracking_min_remaining_percent == 65
    with pytest.raises(ValueError, match="TRACKING_ENABLED"):
        load_config({**base, "TRACKING_ENABLED": "sometimes"})


def test_effort_accepts_verified_values_and_rejects_unknown_ones() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    override = {**base, "CODEX_REASONING_EFFORT": "xhigh"}
    assert load_config(override).codex_reasoning_effort == "xhigh"
    with pytest.raises(ValueError, match="CODEX_REASONING_EFFORT"):
        load_config({**base, "CODEX_REASONING_EFFORT": "extra_high"})


def test_link_render_settings_default_and_reject_non_positive_values() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(base)
    assert config.link_render_timeout_seconds == 40
    assert config.link_screenshot_max_height == 4000
    with pytest.raises(ValueError, match="LINK_RENDER_TIMEOUT_SECONDS"):
        load_config({**base, "LINK_RENDER_TIMEOUT_SECONDS": "0"})
    with pytest.raises(ValueError, match="LINK_SCREENSHOT_MAX_HEIGHT"):
        load_config({**base, "LINK_SCREENSHOT_MAX_HEIGHT": "-1"})


def test_command_prefix_defaults_and_validates() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    assert load_config(base).command_prefix == "codex"
    assert load_config({**base, "COMMAND_PREFIX": "inmu-king"}).command_prefix == "inmu-king"
    with pytest.raises(ValueError, match="COMMAND_PREFIX"):
        load_config({**base, "COMMAND_PREFIX": "Inmu King"})


def test_linkclean_admin_ids_default_empty_and_validate() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    assert load_config(base).linkclean_admin_ids == frozenset()
    ids = load_config({**base, "LINKCLEAN_ADMIN_IDS": "152035364461084672"}).linkclean_admin_ids
    assert ids == frozenset({152035364461084672})
    with pytest.raises(ValueError, match="LINKCLEAN_ADMIN_IDS"):
        load_config({**base, "LINKCLEAN_ADMIN_IDS": "not-an-id"})


def test_link_allow_nets_default_empty_and_never_open_a_lan() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    assert load_config(base).link_allow_nets == ()
    config = load_config({**base, "LINK_ALLOW_NETS": " 198.18.0.0/15 , "})
    assert [str(net) for net in config.link_allow_nets] == ["198.18.0.0/15"]
    for bad in (
        "10.0.0.0/8",
        "172.19.0.0/16",
        "127.0.0.1/32",
        "0.0.0.0/0",
        "169.254.169.254",
        "100.64.0.0/10",
        "192.168.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255",
    ):
        with pytest.raises(ValueError, match="may not include"):
            load_config({**base, "LINK_ALLOW_NETS": bad})
    # IPv6 nets could carry any of the above back in as mapped / NAT64 addresses.
    for bad in ("::/0", "::1", "fc00::/7", "::ffff:10.0.0.0/104", "64:ff9b::/96", "2002::/16"):
        with pytest.raises(ValueError, match="IPv4 networks only"):
            load_config({**base, "LINK_ALLOW_NETS": bad})
    with pytest.raises(ValueError, match="invalid network"):
        load_config({**base, "LINK_ALLOW_NETS": "not-a-net"})


def test_env_example_does_not_promise_a_shadow_mode() -> None:
    # Watches are live from the moment they are made; the example told operators each new
    # watch started review-only until a /track live:<id> that does not exist (Codex on PR #4).
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8")
    assert "shadow" not in text.lower() and "live:" not in text


def test_the_sidecar_reservation_fits_in_the_lookup_deadline() -> None:
    # A lookup keeps XSEARCH_SIDECAR_SECONDS of its deadline for the request; a reservation
    # larger than the whole deadline would send requests the Bot gives up on while the sidecar
    # runs on (Codex on PR #29).
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(base)
    assert (config.xsearch_timeout_seconds, config.xsearch_sidecar_seconds) == (240, 210)
    on = {**base, "XSEARCH_URL": "http://xsearch:8090"}
    assert load_config({**on, "XSEARCH_SIDECAR_SECONDS": "240"}).xsearch_sidecar_seconds == 240
    with pytest.raises(ValueError, match="XSEARCH_SIDECAR_SECONDS"):
        load_config({**on, "XSEARCH_SIDECAR_SECONDS": "241"})
    # Without the sidecar the two settings mean nothing: a short timeout must not stop the Bot.
    assert load_config({**base, "XSEARCH_TIMEOUT_SECONDS": "60"}).xsearch_timeout_seconds == 60


def test_agy_reserve_settings_default_and_stay_in_bounds() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(base)
    assert config.agy_reserve_percent == 20 and config.agy_usage_minutes == 10
    config = load_config({**base, "AGY_RESERVE_PERCENT": "0", "AGY_USAGE_MINUTES": "0"})
    assert config.agy_reserve_percent == 0 and config.agy_usage_minutes == 0
    for name, bad in (("AGY_RESERVE_PERCENT", "101"), ("AGY_USAGE_MINUTES", "-1")):
        with pytest.raises(ValueError, match=name):
            load_config({**base, name: bad})


def test_local_llm_url_is_optional_and_must_be_http() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(base)
    assert (config.local_llm_url, config.local_llm_api_key) == ("", "")
    config = load_config(
        {**base, "LOCAL_LLM_URL": " http://host.orb.internal:8080/v1 ", "LOCAL_LLM_API_KEY": "k"}
    )
    assert (config.local_llm_url, config.local_llm_api_key) == (
        "http://host.orb.internal:8080/v1",
        "k",
    )
    assert load_config({**base, "LOCAL_LLM_URL": "https://llm.lan/v1"}).local_llm_url
    for bad in (
        "ftp://host/v1",
        "host.orb.internal:8080/v1",
        "file:///etc/passwd",
        "http://",
        "http://user:pw@host/v1",
        "http://host /v1",
        "http://host/v1?x=1",
        "http://host/v1#frag",
        "http://host:99999/v1",
    ):
        with pytest.raises(ValueError, match="LOCAL_LLM_URL"):
            load_config({**base, "LOCAL_LLM_URL": bad})


def test_local_llm_params_must_be_a_json_object_without_request_keys() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(base)
    assert config.local_llm_max_tokens == 8192 and local_params(config) == {}
    config = load_config({**base, "LOCAL_LLM_PARAMS": '{"temperature": 1.0, "top_k": 20}'})
    assert local_params(config) == {"temperature": 1.0, "top_k": 20}
    for bad, why in (
        ("{temperature: 1}", "not valid JSON"),
        ("[1]", "must be a JSON object"),
        ('{"model": "x", "max_tokens": 9}', "may not set max_tokens, model"),
    ):
        with pytest.raises(ValueError, match=why):
            load_config({**base, "LOCAL_LLM_PARAMS": bad})
