TASK_CONTENT = {"prompt": "What is 2+2?", "expected_answer": "4"}

import sqlite3


def make_config(name):
    # a bare executable fake target: plan-expansion tests exercise the
    # experiment flow, not target validation (#36 reopen rejects
    # unenforceable configs at creation)
    return {"harness": "fake", "model": "none"}


def setup_registry(register, tasks, configs):
    for task in tasks:
        register("task", task, "v1", dict(TASK_CONTENT, prompt=f"task {task}"))
    for config in configs:
        register("config", config, "v1", make_config(config))


def test_single_task_experiment_plan_expansion(client, register):
    setup_registry(register, ["arith"], ["cfg-a"])
    resp = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
        "repetitions": 2,
    })
    assert resp.status_code == 202, resp.text
    experiment = resp.json()
    assert experiment["id"] and experiment["status"] == "planned"
    assert [(t["plan_order"], t["repetition"]) for t in experiment["trials"]] == [(1, 1), (2, 2)]

    fetched = client.get(f"/v1/experiments/{experiment['id']}")
    assert fetched.status_code == 200
    assert [t["id"] for t in fetched.json()["trials"]] == [t["id"] for t in experiment["trials"]]


def test_suite_experiment_preserves_suite_order(client, register):
    setup_registry(register, ["t-one", "t-two"], ["cfg-a"])
    register("suite", "pack", "v1",
             {"tasks": [{"name": "t-two", "version": "v1"}, {"name": "t-one", "version": "v1"}]})
    resp = client.post("/v1/experiments", json={
        "suite": {"name": "pack", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
    })
    experiment = resp.json()
    assert [t["task"]["name"] for t in experiment["trials"]] == ["t-two", "t-one"]


def test_multi_target_experiment_cartesian_product(client, register):
    setup_registry(register, ["arith"], ["cfg-a", "cfg-b"])
    resp = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}, {"name": "cfg-b", "version": "v1"}],
    })
    experiment = resp.json()
    assert [t["config"]["name"] for t in experiment["trials"]] == ["cfg-a", "cfg-b"]
    assert all(t["repetition"] == 1 for t in experiment["trials"])


def test_missing_version_fails_without_partial_plan(client, tmp_path, register):
    setup_registry(register, ["arith"], [])
    resp = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "missing-cfg", "version": "v1"}],
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "version_not_found"
    # nothing persisted in the fixture's database
    db_path = tmp_path / "aco.db"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0


def test_invalid_experiment_requests_rejected(client, register):
    setup_registry(register, ["arith"], ["cfg-a"])
    both = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "suite": {"name": "pack", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
    })
    assert both.status_code == 422
    neither = client.post("/v1/experiments", json={
        "targets": [{"name": "cfg-a", "version": "v1"}],
    })
    assert neither.status_code == 422
    zero_reps = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
        "repetitions": 0,
    })
    assert zero_reps.status_code == 422


def test_trial_plan_fields_immutable_no_mutation_api(client, register):
    setup_registry(register, ["arith"], ["cfg-a"])
    experiment = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
    }).json()
    trial_id = experiment["trials"][0]["id"]
    for method in ("PATCH", "PUT", "DELETE"):
        resp = client.request(method, f"/v1/trials/{trial_id}", json={"status": "x"})
        assert resp.status_code == 405, (method, resp.status_code)


def test_trial_separates_requested_config_from_runtime_observation(client, register):
    setup_registry(register, ["arith"], ["cfg-a"])
    experiment = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
    }).json()
    trial = client.get(f"/v1/trials/{experiment['trials'][0]['id']}").json()
    assert trial["runtime_observation"] is None  # unknown stays null
    assert trial["requested"]["task"] == {"name": "arith", "version": "v1"}
    # the request snapshot is the normalized profile (defaults included, #36)
    from aco.models import parse_config_content
    assert trial["requested"]["config"] == parse_config_content(make_config("cfg-a")).model_dump()
    assert trial["requested"]["answer_slot"] == 1
    assert len(trial["fingerprint"]) == 64


def test_get_unknown_experiment_and_trial_404(client, register):
    assert client.get("/v1/experiments/nope").status_code == 404
    assert client.get("/v1/trials/nope").status_code == 404
    assert client.get("/v1/experiments/nope").json()["error"]["code"] == "not_found"


def test_openapi_generated_from_runtime_models(client, register):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    assert {"/v1/versions", "/v1/experiments", "/v1/experiments/{experiment_id}",
            "/v1/trials/{trial_id}"} <= paths
    schemas = set(spec["components"]["schemas"])
    assert {"VersionRegistration", "ExperimentCreate", "TrialOut"} <= schemas


# ------------------------------------------------------------------ #38

PI_PROFILE_CONTENT = {
    "schema_version": 1,
    "harness": "pi",
    "harness_version": "0.84.1",
    "model": "glm-5.3-flash",
    "thinking": "max",
    "provider": "ark-agent-plan",
    "provider_api_style": "openai-responses",
    "adapter_version": "0.1.1",
    "environment": "sha256:" + "b" * 64,
    "credentials": ["ark-agent-plan-main"],
}


def test_real_provider_target_requires_allow_paid_run(client, register):
    setup_registry(register, ["arith"], ["cfg-a"])
    register("config", "pi-cfg", "v1", dict(PI_PROFILE_CONTENT))
    resp = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "pi-cfg", "version": "v1"}],
    })
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "paid_run_not_allowed"
    # non-pi harnesses are not paid targets: creation stays open
    resp = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "cfg-a", "version": "v1"}],
    })
    assert resp.status_code == 202, resp.text


def test_real_provider_target_with_allow_paid_run_is_created(client, register):
    setup_registry(register, ["arith"], [])
    register("config", "pi-cfg", "v1", dict(PI_PROFILE_CONTENT))
    resp = client.post("/v1/experiments", json={
        "task": {"name": "arith", "version": "v1"},
        "targets": [{"name": "pi-cfg", "version": "v1"}],
        "allow_paid_run": True,
    })
    assert resp.status_code == 202, resp.text
    assert resp.json()["requested"]["allow_paid_run"] is True


def test_version_get_returns_registered_content(client, register):
    register("config", "pi-cfg", "v1", dict(PI_PROFILE_CONTENT))
    resp = client.get("/v1/versions/config/pi-cfg/v1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"]["harness"] == "pi"
    assert client.get("/v1/versions/config/missing/v1").status_code == 404


def test_suite_with_different_tasks_reuses_one_target_fingerprint(client, register):
    """A target describes shared execution conditions, so task instructions
    do not make one Pi target unusable for a suite."""
    setup_registry(register, ["task-one", "task-two"], [])
    register("config", "pi-cfg", "v1", dict(PI_PROFILE_CONTENT,
                                              environment="sha256:" + "b" * 64))
    register("suite", "pack", "v1", {
        "tasks": [{"name": "task-one", "version": "v1"},
                   {"name": "task-two", "version": "v1"}],
    })
    response = client.post("/v1/experiments", json={
        "suite": {"name": "pack", "version": "v1"},
        "targets": [{"name": "pi-cfg", "version": "v1"}],
        "allow_paid_run": True,
    })

    assert response.status_code == 202, response.text
    trials = response.json()["trials"]
    assert [trial["task"]["name"] for trial in trials] == ["task-one", "task-two"]
    assert len({trial["fingerprint"] for trial in trials}) == 1
