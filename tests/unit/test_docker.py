import subprocess

from rdm import docker


class _Recorder:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.calls: list[dict] = []
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, self._returncode, self._stdout, self._stderr)


def test_compose_command_shape(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    result = docker.compose("up", "-d", "toolbox", compose_file="custom.yml")
    assert rec.calls[0]["argv"] == [
        "docker", "compose", "-f", "custom.yml", "up", "-d", "toolbox",
    ]
    assert isinstance(rec.calls[0]["argv"], list)
    assert rec.calls[0].get("shell", False) is False
    assert result.returncode == 0


def test_compose_default_compose_file(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    docker.compose("ps")
    argv = rec.calls[0]["argv"]
    assert argv[:2] == ["docker", "compose"]
    assert argv[2] == "-f"
    assert argv[3].endswith("home/docker-compose.yml") or argv[3].endswith("home\\docker-compose.yml")
    assert argv[4:] == ["ps"]


def test_compose_ps_format(monkeypatch):
    rec = _Recorder(stdout="rdm-toolbox Up (healthy)\n")
    monkeypatch.setattr(subprocess, "run", rec)
    out = docker.compose_ps("c.yml")
    assert out == "rdm-toolbox Up (healthy)\n"
    assert rec.calls[0]["argv"] == [
        "docker", "compose", "-f", "c.yml", "ps",
        "--format", "{{.Name}} {{.Status}}",
    ]


def test_compose_timeout_returns_124(monkeypatch):
    def expired(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 120))

    monkeypatch.setattr(subprocess, "run", expired)
    result = docker.compose("up", "-d", timeout=5)
    assert result.returncode == 124
    assert result.stdout == ""
    assert result.stderr == "docker timeout"


def test_run_timeout_returns_124(monkeypatch):
    def expired(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 120))

    monkeypatch.setattr(subprocess, "run", expired)
    result = docker.run("ps", timeout=5)
    assert result.returncode == 124
    assert result.stdout == ""
    assert result.stderr == "docker timeout"
