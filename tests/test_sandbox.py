from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from discord_codex_bot import sandbox
from discord_codex_bot.sandbox import RunResult, extract_runs, render_result, run_code


def test_extract_runs_takes_multiline_code_verbatim_and_caps_the_count() -> None:
    answer = (
        '<run lang="python">\nimport math\nprint(1 < 2, math.pi)\n</run>'
        '<run lang="sh">echo hi</run><run lang="python">print(3)</run>'
    )
    runs = extract_runs(answer)
    assert runs == [("python", "import math\nprint(1 < 2, math.pi)"), ("sh", "echo hi")]
    assert extract_runs('<run lang="ruby">x</run>') == []


class Response:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def post(self, url, json=None):
        self.calls.append((url, json))
        return Response(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def test_run_code_posts_and_saves_returned_files(monkeypatch, config, tmp_path) -> None:
    payload = {
        "exit": 0,
        "timed_out": False,
        "stdout": "42\n",
        "stderr": "",
        "files": [{"name": "../a.png", "b64": "aGk="}, {"name": "big.bin", "skipped": "9 bytes"}],
    }
    session = Session(payload)
    monkeypatch.setattr(sandbox.aiohttp, "ClientSession", lambda **kw: session)
    result = await run_code("python", "print(42)", config, tmp_path / "out")
    assert session.calls[0][0] == "http://sandbox:8070/run"
    assert session.calls[0][1] == {"lang": "python", "code": "print(42)", "timeout": 30}
    assert result.exit == 0 and result.stdout == "42\n"
    assert result.files == [tmp_path / "out" / "a.png"]  # path traversal stripped
    assert (tmp_path / "out" / "a.png").read_bytes() == b"hi"
    assert result.skipped == ["big.bin（9 bytes）"]
    monkeypatch.setattr(sandbox.aiohttp, "ClientSession", lambda **kw: Session({"error": "bad"}))
    failed = await run_code("python", "x", config, None)
    assert failed.exit == -1 and failed.stderr == "bad"


def test_render_result_shapes() -> None:
    ok = render_result("python", RunResult(0, False, "hi\n", "", [Path("/x/out.txt")], []))
    assert ok == (
        '<RESULT kind="run" lang="python" status="exit 0">\nhi\n'
        "[檔案已回傳並附給成員：out.txt]\n</RESULT>"
    )
    late = render_result("sh", RunResult(-1, True, "", "boom", [], ["big（cap）"]))
    assert 'status="逾時被中止"' in late and "[stderr]\nboom" in late
    assert "[未回傳：big（cap）]" in late
    assert "（沒有輸出）" in render_result("sh", RunResult(0, False, "", "", [], []))


def _server_module():
    spec = importlib.util.spec_from_file_location(
        "sandbox_server", Path(__file__).resolve().parents[1] / "sandbox" / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_server"] = module
    spec.loader.exec_module(module)
    return module


def test_server_execute_runs_python_collects_files_and_times_out(tmp_path, monkeypatch) -> None:
    server = _server_module()
    monkeypatch.setattr(server, "WORK", str(tmp_path))
    monkeypatch.setattr(server, "_limits", lambda: None)  # rlimits vary by host; not under test
    result = server.execute("python", "print(2**10)\nopen('out/a.txt','w').write('x')", 5)
    assert result["exit"] == 0 and result["stdout"].strip() == "1024" and not result["timed_out"]
    assert result["files"] == [{"name": "a.txt", "b64": "eA=="}]
    assert list(tmp_path.iterdir()) == []  # work dir cleaned
    slow = server.execute("sh", "sleep 3", 1)
    assert slow["timed_out"] and slow["exit"] == -1
    assert "unsupported" in server.execute("ruby", "x", 1)["error"]
