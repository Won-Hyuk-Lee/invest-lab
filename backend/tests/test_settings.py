import os

import pytest

from invest_lab.adapters.settings import KisSettings, SettingsError


def test_standard_dotenv_quotes_comments_export_and_no_interpolation(tmp_path):
    file = tmp_path / "fixture.env"
    file.write_text(
        'export KIS_PAPER_APP_KEY="fixture-key" # comment\n'
        "KIS_PAPER_APP_SECRET='literal $TOKEN ${TOKEN}'\n"
        "KIS_PAPER_ACCOUNT_NUMBER=01234567\n"
        "KIS_PAPER_ACCOUNT_PRODUCT_CODE=01\n",
        encoding="utf-8",
    )
    before = dict(os.environ)
    settings = KisSettings.from_env_file(file, environ={})
    assert settings.app_key == "fixture-key"
    assert settings.app_secret == "literal $TOKEN ${TOKEN}"
    assert settings.account_number == "01234567"
    assert settings.cache_path == tmp_path / ".local/kis-coordination.db"
    assert dict(os.environ) == before
    assert settings.readiness()["configured"]
    assert settings.readiness()["connected"] is False
    assert "fixture-key" not in repr(settings)
    assert "01234567" not in repr(settings)


def test_live_keys_cannot_silently_populate_paper_configuration(tmp_path):
    settings = KisSettings.from_mapping(
        {"KIS_APP_KEY": "live-key", "KIS_APP_SECRET": "live-secret"}
    )
    assert not settings.readiness()["configured"]
    assert "KIS_PAPER_APP_KEY" in settings.readiness()["missing"]
    with pytest.raises(SettingsError, match="Missing configuration"):
        settings.require()


@pytest.mark.parametrize(
    "fields",
    [
        {"base_url": "https://openapi.koreainvestment.com:9443"},
        {"ws_url": "ws://ops.koreainvestment.com:21000"},
        {"account_number": "private-invalid-account"},
        {"app_secret": "secret\nheader"},
        {"request_interval": float("nan")},
        {"timeout": float("inf")},
    ],
)
def test_configuration_rejects_unsafe_values_without_echo(fields):
    with pytest.raises(SettingsError) as error:
        KisSettings(**fields)
    assert "private-invalid-account" not in str(error.value)
    assert "secret\nheader" not in str(error.value)


def test_malformed_dotenv_is_redacted_and_not_logged(tmp_path, caplog):
    file = tmp_path / "fixture.env"
    file.write_text('KIS_PAPER_APP_SECRET="private-unclosed\n', encoding="utf-8")
    with pytest.raises(SettingsError, match="Invalid dotenv syntax") as error:
        KisSettings.from_env_file(file, environ={})
    assert "private-unclosed" not in str(error.value)
    assert not caplog.records


def test_explicit_environment_override_and_order_gate(tmp_path):
    settings = KisSettings.from_env_file(
        tmp_path / "missing.env",
        environ={
            "KIS_PAPER_APP_KEY": "fixture-key",
            "KIS_PAPER_APP_SECRET": "fixture-secret",
            "KIS_PAPER_ORDERS_ENABLED": "false",
        },
    )
    with pytest.raises(SettingsError, match="disabled"):
        settings.require(orders=True)
