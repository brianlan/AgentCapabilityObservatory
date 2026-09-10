"""Versioned TargetProfile and Trial fingerprint (#36).

Covers: schema + defaults + unknown-field rejection on both the v1 and
legacy paths, secret-free registration, fingerprint stability and
per-field sensitivity, assistance-mode comparability split, and the
results series carrying the fingerprint.
"""

import sqlite3

import pytest

from aco.app import trial_fingerprint
from aco.models import TargetProfile, parse_config_content

TASK_CONTENT = {"prompt": "What is 2+2?", "expected_answer": "4"}

PI_PROFILE = {
    "schema_version": 1,
    "harness": "pi",
    "harness_version": "0.84.1",
    "model": "glm-5.3-flash",
    "thinking": "max",
    "provider": "ark-agent-plan",
    "provider_api_style": "agent-plan",
    "adapter_version": "aco-pi-1",
    "assistance_mode": "none",
    "prompt_digest": "sha256:" + "0" * 64,
    "environment": "registry.local/aco-pi@sha256:" + "1" * 64,
    "resources": {"cpus": 2, "memory_mb": 4096, "timeout_sec": 600, "network": "offline"},
    "skills": [{"name": "pdf", "version": "v2"}, {"name": "search", "version": "v1"}],
    "credentials": ["ark-main"],
}


def register_task(register, name="arith"):
    register("task", name, "v1", dict(TASK_CONTENT))


def create_trial(client, config_name="t1", version="v1", allow_paid_run=False):
    experiment = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": config_name, "version": version}],
        "allow_paid_run": allow_paid_run,
    }).json()
    return experiment["trials"][0]


class TestTargetProfileSchema:
    def test_legacy_fake_config_still_registers_and_materializes(self, client, register):
        register_task(register)
        register("config", "t1", "v1", {"harness": "fake", "model": "none"})
        trial = create_trial(client)
        # the request snapshot is the NORMALIZED profile: defaults materialized,
        # legacy skill names become unversioned refs, assistance_mode none
        assert trial["requested"]["config"] == {
            "schema_version": 1, "harness": "fake", "harness_version": None,
            "model": "none", "thinking": None, "provider": None,
            "provider_api_style": None, "adapter_version": None,
            "assistance_mode": "none", "prompt_digest": None, "environment": None,
            "resources": None, "skills": [], "credentials": [],
        }
        assert len(trial["fingerprint"]) == 64

    def test_v1_profile_registers_with_pinned_pi_fields(self, client, register):
        register_task(register)
        register("config", "pi-main", "v1", dict(PI_PROFILE))
        # the pinned-field flow is exercised through the paid gate (#38)
        trial = create_trial(client, "pi-main", allow_paid_run=True)
        cfg = trial["requested"]["config"]
        assert cfg["harness"] == "pi" and cfg["harness_version"] == "0.84.1"
        assert cfg["model"] == "glm-5.3-flash" and cfg["thinking"] == "max"
        assert cfg["provider"] == "ark-agent-plan"
        assert [s["name"] for s in cfg["skills"]] == ["pdf", "search"]  # ordered
        assert cfg["credentials"] == ["ark-main"]  # logical ref, never a value

    def test_unknown_field_fails_on_v1_path(self, register):
        bad = dict(PI_PROFILE, temperature=0.7)
        resp = register("config", "bad", "v1", bad, expect=(422,))
        assert resp.json()["error"]["code"] == "invalid_content"

    def test_unknown_field_fails_on_legacy_path(self, register):
        resp = register("config", "bad", "v1",
                        {"harness": "fake", "model": "none", "unknown_key": "x"},
                        expect=(422,))
        assert resp.json()["error"]["code"] == "invalid_content"

    def test_secret_value_cannot_enter_registry_or_trial(self, client, register):
        register_task(register)
        # there is no field a credential VALUE could occupy: any extra key fails
        resp = register("config", "leaky", "v1",
                        dict(PI_PROFILE, api_key="sk-secret-value"), expect=(422,))
        assert resp.json()["error"]["code"] == "invalid_content"
        # and a legitimately registered profile's trial snapshot carries only
        # the declared ref fields — no value-shaped key ever appears
        register("config", "t1", "v1", {"harness": "fake", "model": "none",
                                        "credentials": ["ark-main"]})
        trial = create_trial(client)
        assert "sk-secret-value" not in str(trial["requested"])
        assert set(trial["requested"]["config"]) <= set(TargetProfile.model_fields)

    def test_assistance_mode_values(self, register):
        ok = register("config", "ok", "v1",
                      dict(PI_PROFILE, assistance_mode="human"))
        assert ok.status_code in (200, 201)
        register("config", "bad", "v1", dict(PI_PROFILE, assistance_mode="auto"),
                 expect=(422,))

    def test_skills_default_empty_not_inferred(self, client, register):
        register_task(register)
        register("config", "t1", "v1", {"harness": "fake", "model": "none"})
        trial = create_trial(client)
        assert trial["requested"]["config"]["skills"] == []

    def test_assistance_mode_never_shares_a_result_series(self, client, register):
        register_task(register)
        register("config", "bare", "v1", {"harness": "fake", "model": "none"})
        register("config", "assisted", "v1",
                 {"schema_version": 1, "harness": "fake", "model": "none",
                  "assistance_mode": "human"})
        for name in ("bare", "assisted"):
            resp = client.post("/v1/experiments", json={
                "task": {"name": "arith", "version": "v1"},
                "targets": [{"name": name, "version": "v1"}]})
            assert resp.status_code == 202, resp.text
        data = client.get("/v1/results").json()
        keys = [(s["key"]["config"], s["key"]["fingerprint"]) for s in data["series"]]
        assert len(keys) == 2, keys
        assert len({fp for _, fp in keys}) == 2  # different series, always


BASE = parse_config_content({"harness": "fake", "model": "none"})


class TestTrialFingerprint:
    def test_fingerprint_stable_across_legacy_and_v1_shapes(self):
        # a bare fake profile means the same conditions in both shapes: the
        # normalization makes legacy and v1 registrations comparable
        legacy = parse_config_content({"harness": "fake", "model": "none"})
        v1 = parse_config_content({"schema_version": 1, "harness": "fake",
                                   "model": "none"})
        assert legacy.model_dump() == v1.model_dump()
        assert trial_fingerprint(legacy) == trial_fingerprint(v1)

    def test_same_profile_same_fingerprint(self):
        assert trial_fingerprint(BASE) == trial_fingerprint(
            parse_config_content({"harness": "fake", "model": "none"}))

    @pytest.mark.parametrize("mutation", [
        {"harness": "pi"},
        {"harness_version": "0.84.1"},
        {"model": "glm-5.3-flash"},
        {"thinking": "max"},
        {"provider": "ark-agent-plan"},
        {"provider_api_style": "agent-plan"},
        {"adapter_version": "aco-pi-1"},
        {"assistance_mode": "human"},
        {"prompt_digest": "sha256:" + "0" * 64},
        {"environment": "registry.local/x@sha256:" + "1" * 64},
        {"resources": {"network": "online"}},
        {"resources": {"timeout_sec": 60}},
        {"skills": [{"name": "search", "version": "v1"}]},
        {"credentials": ["ark-main"]},
    ], ids=lambda m: next(iter(m)))
    def test_every_controlled_field_change_changes_fingerprint(self, mutation):
        changed = parse_config_content({"harness": "fake", "model": "none"})
        merged = changed.model_copy(update=mutation)
        assert trial_fingerprint(merged) != trial_fingerprint(BASE)

    def test_skill_order_changes_fingerprint(self):
        ab = BASE.model_copy(update={
            "skills": [{"name": "a", "version": "v1"}, {"name": "b", "version": "v1"}]})
        ba = BASE.model_copy(update={
            "skills": [{"name": "b", "version": "v1"}, {"name": "a", "version": "v1"}]})
        assert trial_fingerprint(ab) != trial_fingerprint(ba)

    def test_pre_0011_rows_backfill_deterministically(self, client, register, tmp_path):
        register_task(register)
        register("config", "t1", "v1", {"harness": "fake", "model": "none"})
        trial = create_trial(client)
        # simulate a pre-0011 row: fingerprint column cleared after creation
        conn = sqlite3.connect(tmp_path / "aco.db")
        conn.execute("UPDATE trials SET fingerprint = NULL")
        conn.commit()
        conn.close()
        fetched = client.get(f"/v1/trials/{trial['id']}").json()
        assert fetched["fingerprint"] == trial["fingerprint"]
        # the backfill was persisted, not recomputed per read
        conn = sqlite3.connect(tmp_path / "aco.db")
        stored = conn.execute("SELECT fingerprint FROM trials").fetchone()[0]
        conn.close()
        assert stored == trial["fingerprint"]
