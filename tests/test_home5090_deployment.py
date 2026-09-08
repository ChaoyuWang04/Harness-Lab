from __future__ import annotations

from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_is_os_bounded_and_refuses_conflict_removal() -> None:
    script = (LAB_ROOT / "scripts" / "bootstrap_home5090.sh").read_text(encoding="utf-8")

    assert '"${VERSION_ID}" != "24.04"' in script
    assert "Refusing to remove conflicting packages automatically" in script
    assert '"nvidia-container-toolkit=${nvidia_toolkit_version}"' in script
    assert "nvidia-ctk runtime configure --runtime=docker" in script


def test_tunnel_fails_closed_and_only_forwards_public_ui_ports() -> None:
    script = (LAB_ROOT / "scripts" / "home5090_tunnel.sh").read_text(encoding="utf-8")

    assert "ExitOnForwardFailure=yes" in script
    assert "127.0.0.1:18000:127.0.0.1:8000" in script
    assert "127.0.0.1:13300:127.0.0.1:3300" in script
    for private_port in ("5432", "6379", "4317", "4318", "11434"):
        assert f"-L 127.0.0.1:{private_port}" not in script


def test_remote_verifier_never_renders_secret_values() -> None:
    script = (LAB_ROOT / "scripts" / "verify_home5090.sh").read_text(encoding="utf-8")

    assert "config --quiet" in script
    assert "config >" not in script
    assert "nvidia-smi" in script


def test_m2_gate_runs_in_an_isolated_container_on_the_runtime_host() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m2_home5090.sh").read_text(encoding="utf-8")

    assert "--network host" in script
    assert "--user" in script
    assert "--group-add" in script
    assert "/var/run/docker.sock:/var/run/docker.sock" in script
    assert "/usr/bin/docker:/usr/bin/docker:ro" in script
    assert "docker-compose:/usr/libexec/docker/cli-plugins/docker-compose:ro" in script
    assert '"${lab_root}:/workspace"' in script
    assert "python scripts/verify_m2.py" in script


def test_m3_gate_uses_isolated_database_and_always_restores_normal_runtime() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3" in script
    assert "harness_m3_test" in script
    assert "redis://redis:6379/1" in script
    assert "trap restore_normal_runtime EXIT" in script
    assert "up -d redis" in script
    assert "--profile m3" in script
    assert "harness-lab_default" in script
    assert "/var/run/docker.sock:/var/run/docker.sock" in script
    assert '"${lab_root}:${lab_root}"' in script
    assert "python tests/test_outbox.py" in script
    assert "python scripts/verify_m3.py" in script
    assert "--scale worker=1" in script


def test_m3_gate2_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert 'gate_id="${1:-gate1}"' in script
    assert "harness_m3_gate2" in script
    assert "redis://redis:6379/2" in script
    assert "artifacts/m3/gate2/gate_m3.json" in script
    assert 'case "${gate_id}"' in script
    assert "--output" in script


def test_m3_gate3_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate3" in script
    assert "redis://redis:6379/3" in script
    assert "artifacts/m3/gate3/gate_m3.json" in script


def test_m3_gate4_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate4" in script
    assert "redis://redis:6379/4" in script
    assert "artifacts/m3/gate4/gate_m3.json" in script


def test_m3_gate5_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate5" in script
    assert "redis://redis:6379/5" in script
    assert "artifacts/m3/gate5/gate_m3.json" in script


def test_m3_gate6_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate6" in script
    assert "redis://redis:6379/6" in script
    assert "artifacts/m3/gate6/gate_m3.json" in script


def test_m3_gate7_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate7" in script
    assert "redis://redis:6379/7" in script
    assert "artifacts/m3/gate7/gate_m3.json" in script


def test_m3_gate8_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate8" in script
    assert "redis://redis:6379/8" in script
    assert "artifacts/m3/gate8/gate_m3.json" in script


def test_m3_gate9_has_fresh_database_redis_and_evidence_namespaces() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "harness_m3_gate9" in script
    assert "redis://redis:6379/11" in script
    assert "artifacts/m3/gate9/gate_m3.json" in script


def test_poolwarm_load_probe_has_fresh_namespace_and_mode() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "poolwarm_probe" in script
    assert "harness_m3_poolwarm_probe" in script
    assert "redis://redis:6379/9" in script
    assert "artifacts/m3/diagnostics/load_four_poolwarm.json" in script
    assert "--load-four-only" in script


def test_writepath_load_probe_has_fresh_namespace_and_mode() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert "writepath_probe" in script
    assert "harness_m3_writepath_probe" in script
    assert "redis://redis:6379/10" in script
    assert "artifacts/m3/diagnostics/load_four_writepath.json" in script
    assert script.count("verifier_mode_args=(--load-four-only)") >= 2
    assert "python tests/test_runs_service.py" in script


def test_m3_wrapper_rebuilds_every_python_service_before_injection() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert (
        '"${compose[@]}" build api dispatcher worker sweeper migrate chaos-proxy'
        in script
    )
    assert "from app.chaos.hooks import crash_after_publish_once" in script


def test_m3_wrapper_resets_only_the_exact_root_owned_dispatcher_marker() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert '"${lab_root}/data/chaos:/var/lib/harness-chaos"' in script
    assert (
        "Path('/var/lib/harness-chaos/dispatcher-after-publish.once').unlink(missing_ok=True)"
        in script
    )


def test_api_capacity_probe_is_isolated_and_restores_normal_api() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_api_capacity_home5090.sh").read_text(
        encoding="utf-8"
    )

    assert "harness_m3_api4_probe4" in script
    assert "artifacts/m3/diagnostics/api4_capacity_probe4.json" in script
    assert "trap restore_normal_api EXIT" in script
    assert "--api-capacity-only" in script
    assert "--redis-url redis://redis:6379/15" in script
