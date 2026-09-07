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


def test_m3_wrapper_rebuilds_every_python_service_before_injection() -> None:
    script = (LAB_ROOT / "scripts" / "run_verify_m3_home5090.sh").read_text(encoding="utf-8")

    assert (
        '"${compose[@]}" build api dispatcher worker sweeper migrate chaos-proxy'
        in script
    )
    assert "from app.chaos.hooks import crash_after_publish_once" in script
