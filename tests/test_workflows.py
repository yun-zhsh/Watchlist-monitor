"""The GitHub Actions workflows: the tests gate the publication, so keep that wiring from being removed by accident."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow):
    return workflow.get("on") or workflow[True]  # YAML 1.1 reads a bare `on` as the boolean True


def test_tests_run_on_pull_requests_and_can_be_called_by_the_publish_workflow():
    ci = load("ci.yml")
    assert {"pull_request", "workflow_call"} <= set(triggers(ci))
    assert set(ci["jobs"]) == {"test", "image"}
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python"] == ["3.10", "3.12"]  # the oldest and the newest supported


def test_nothing_is_published_after_a_code_change_unless_the_tests_pass():
    pages = load("pages.yml")
    tests, build = pages["jobs"]["tests"], pages["jobs"]["build"]
    assert tests["uses"] == "./.github/workflows/ci.yml"
    assert tests["if"] == (  # scheduled runs and data-only manual runs on main reuse code that passed
        "github.event_name != 'schedule' && !(github.event_name == 'workflow_dispatch' && inputs.data_only && github.ref == 'refs/heads/main')"
    )
    assert build["needs"] == "tests"
    condition = build["if"]
    assert "needs.tests.result == 'success'" in condition and "needs.tests.result == 'skipped'" in condition  # not "always run"
    deploy = pages["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["if"] == "${{ !cancelled() && needs.build.result == 'success' }}"  # published only after a good build


def test_a_manual_run_tests_the_code_unless_data_only_is_ticked():
    pages = load("pages.yml")
    data_only = triggers(pages)["workflow_dispatch"]["inputs"]["data_only"]
    assert data_only["type"] == "boolean" and data_only["default"] is False  # a plain "Run workflow" still runs the tests
    condition = pages["jobs"]["tests"]["if"]
    assert "inputs.data_only" in condition and "github.ref == 'refs/heads/main'" in condition  # never skipped on another branch


def upstream(jobs, name):
    """Every job that `name` needs, directly or through other jobs."""
    needs = jobs[name].get("needs", [])
    direct = [needs] if isinstance(needs, str) else list(needs)
    return set(direct).union(*(upstream(jobs, job) for job in direct))


def test_a_job_after_a_skippable_job_is_not_skipped_with_it():
    # GitHub skips a job when any job upstream of it, however far back, was skipped, unless the job has a condition with a
    # status check function. The scheduled runs skip "tests", which once left the deployment skipped on every scheduled
    # run, while the runs still showed as successful.
    for name in ("ci.yml", "pages.yml", "home-order.yml"):
        jobs = load(name)["jobs"]
        for job_name, job in jobs.items():
            skippable = [up for up in upstream(jobs, job_name) if "if" in jobs[up]]
            if skippable:
                condition = str(job.get("if", ""))
                assert "!cancelled()" in condition or "always()" in condition, (name, job_name, skippable)


def test_the_scheduled_run_reaches_the_deployment():
    jobs = load("pages.yml")["jobs"]
    assert "deploy" in jobs and upstream(jobs, "deploy") == {"build", "tests"}
    assert "schedule" in str(jobs["tests"]["if"])  # the job a scheduled run skips is upstream of the deployment...
    assert "!cancelled()" in jobs["deploy"]["if"]  # ...so the deployment must not inherit the skip


def test_the_database_size_check_runs_before_the_data_is_committed():
    steps = [step.get("name", "") for step in load("pages.yml")["jobs"]["build"]["steps"]]
    assert steps.index("Check the size of the database") < steps.index("Commit the updated data")


def test_stale_data_still_publishes_but_fails_the_run_at_the_end():
    pages = load("pages.yml")
    build_steps = [step.get("name", step.get("uses", "")) for step in pages["jobs"]["build"]["steps"]]
    assert build_steps.index("Check that the data is fresh") < build_steps.index("actions/upload-pages-artifact@v3")
    check = next(step for step in pages["jobs"]["build"]["steps"] if step.get("name") == "Check that the data is fresh")
    assert "valuation-monitor-check" in check["run"] and "continue-on-error" not in check  # a stale series must not stop the publication...
    assert pages["jobs"]["build"]["outputs"]["stale"] == "${{ steps.freshness.outputs.stale }}"
    last = pages["jobs"]["deploy"]["steps"][-1]  # ...but after it has happened, the run fails
    assert last["name"] == "Fail the run if the data is stale" and last["if"] == "needs.build.outputs.stale == 'true'" and "exit 1" in last["run"]


def test_the_home_page_order_workflow_only_rewrites_config_and_republishes():
    workflow = load("home-order.yml")
    order = triggers(workflow)["workflow_dispatch"]["inputs"]["order"]
    assert set(triggers(workflow)) == {"workflow_dispatch"} and order["type"] == "string" and order["required"] is True
    assert workflow["permissions"] == {"contents": "read"}  # each job asks for what it needs
    save, publish = workflow["jobs"]["save"], workflow["jobs"]["publish"]
    assert save["if"] == "github.ref == 'refs/heads/main'"
    assert save["permissions"] == {"contents": "write"}
    steps = {step.get("name", step.get("uses")): step for step in save["steps"]}
    write = steps["Write the new order into config.yaml"]
    assert write["env"] == {"ORDER": "${{ inputs.order }}"} and "${{" not in write["run"]  # the input never becomes script text
    assert "valuation-monitor-home-order" in write["run"]
    commit = steps["Commit the new order"]["run"]
    assert "git add config.yaml" in commit and "git add data" not in commit and "git add -A" not in commit
    assert "git pull --rebase origin main" in commit and "git push origin HEAD:main" in commit
    # republished after the save, in the same group as the scheduled data workflow, without tests or a data update
    assert publish["needs"] == "save"
    assert publish["concurrency"] == {"group": load("pages.yml")["concurrency"]["group"], "cancel-in-progress": False}
    assert publish["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    runs = " ".join(step.get("run", "") for step in publish["steps"])
    assert "valuation-monitor-export --offline" in runs and "pytest" not in runs and "git push" not in runs
    assert "SEC_USER_AGENT" not in str(workflow) and "ci.yml" not in str(workflow)
    assert [step.get("uses") for step in publish["steps"]][-2:] == ["actions/upload-pages-artifact@v3", "actions/deploy-pages@v4"]
