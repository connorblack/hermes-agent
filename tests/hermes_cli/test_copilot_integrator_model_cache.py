"""Copilot provider-model cache isolation by integration id."""

import json

import hermes_cli.models as models


def _cache_payload(tmp_path):
    return json.loads((tmp_path / "provider_models_cache.json").read_text())


def test_copilot_model_cache_keeps_distinct_integrator_catalogs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls: list[tuple[str, str | None, bool]] = []

    def fake_provider_model_ids(provider, *, force_refresh=False, integrator=None):
        calls.append((provider, integrator, force_refresh))
        if integrator == "vscode-chat":
            return ["gpt-vscode"]
        if integrator == "jetbrains-chat":
            return ["gpt-jetbrains"]
        return ["unexpected"]

    monkeypatch.setattr(models, "provider_model_ids", fake_provider_model_ids)

    assert models.cached_provider_model_ids("copilot", integrator="vscode-chat") == [
        "gpt-vscode"
    ]
    assert models.cached_provider_model_ids("copilot", integrator="jetbrains-chat") == [
        "gpt-jetbrains"
    ]
    assert calls == [
        ("copilot", "vscode-chat", False),
        ("copilot", "jetbrains-chat", False),
    ]

    payload = _cache_payload(tmp_path)
    assert payload["version"] == 2
    assert payload["entries"]["copilot::vscode-chat"]["models"] == ["gpt-vscode"]
    assert payload["entries"]["copilot::jetbrains-chat"]["models"] == ["gpt-jetbrains"]

    calls.clear()

    def fail_if_live_fetch(provider, *, force_refresh=False, integrator=None):  # pragma: no cover
        raise AssertionError(f"unexpected live fetch for {provider}/{integrator}")

    monkeypatch.setattr(models, "provider_model_ids", fail_if_live_fetch)
    assert models.cached_provider_model_ids("copilot", integrator="vscode-chat") == [
        "gpt-vscode"
    ]
    assert models.cached_provider_model_ids("copilot", integrator="jetbrains-chat") == [
        "gpt-jetbrains"
    ]
    assert calls == []


def test_copilot_refresh_and_evict_are_scoped_to_one_integrator(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    seed_catalogs = {
        "vscode-chat": ["old-vscode"],
        "jetbrains-chat": ["old-jetbrains"],
    }

    def seed_provider_model_ids(provider, *, force_refresh=False, integrator=None):
        return seed_catalogs[str(integrator)]

    monkeypatch.setattr(models, "provider_model_ids", seed_provider_model_ids)
    assert models.cached_provider_model_ids("copilot", integrator="vscode-chat") == [
        "old-vscode"
    ]
    assert models.cached_provider_model_ids("copilot", integrator="jetbrains-chat") == [
        "old-jetbrains"
    ]

    refreshed_calls: list[str | None] = []

    def refresh_one(provider, *, force_refresh=False, integrator=None):
        refreshed_calls.append(integrator)
        if integrator == "vscode-chat":
            assert force_refresh is True
            return ["new-vscode"]
        raise AssertionError(f"refresh leaked to {integrator}")

    monkeypatch.setattr(models, "provider_model_ids", refresh_one)
    assert models.cached_provider_model_ids(
        "copilot",
        integrator="vscode-chat",
        force_refresh=True,
    ) == ["new-vscode"]
    assert refreshed_calls == ["vscode-chat"]

    def fail_if_live_fetch(provider, *, force_refresh=False, integrator=None):  # pragma: no cover
        raise AssertionError(f"unexpected live fetch for {provider}/{integrator}")

    monkeypatch.setattr(models, "provider_model_ids", fail_if_live_fetch)
    assert models.cached_provider_model_ids("copilot", integrator="jetbrains-chat") == [
        "old-jetbrains"
    ]

    models.clear_provider_models_cache("copilot", integrator="vscode-chat")

    def refill_evicted(provider, *, force_refresh=False, integrator=None):
        if integrator == "vscode-chat":
            return ["refilled-vscode"]
        raise AssertionError(f"eviction leaked to {integrator}")

    monkeypatch.setattr(models, "provider_model_ids", refill_evicted)
    assert models.cached_provider_model_ids("copilot", integrator="vscode-chat") == [
        "refilled-vscode"
    ]

    monkeypatch.setattr(models, "provider_model_ids", fail_if_live_fetch)
    assert models.cached_provider_model_ids("copilot", integrator="jetbrains-chat") == [
        "old-jetbrains"
    ]


def test_copilot_default_headers_accepts_integrator_override():
    assert (
        models.copilot_default_headers(integrator="jetbrains-chat")[
            "Copilot-Integration-Id"
        ]
        == "jetbrains-chat"
    )
