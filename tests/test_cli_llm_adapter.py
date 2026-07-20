# cli_llm_adapter.generate(Claude/Codex CLI subprocess 어댑터)를 subprocess.run mock으로 검증한다.
# 실바이너리 없이 실행되며, File-IPC 계약(프롬프트=stdin 파일, 결과=출력 파일, 파기)을 확인한다.
import os
import shutil

import pytest

import adapters.cli_llm_adapter as cli_llm_adapter
from config import settings


# stdin으로 넘어온 프롬프트 tempfile에서 "결과를 ... 저장하라: <경로>" 뒤의 출력 경로를 뽑아
# 그 파일에 가짜 결과를 써준다 — 실제 claude/codex 에이전트 CLI가 파일을 쓰는 동작을 흉내낸다.
def _fake_run_writes_output(output_text: str):
    captured = {}

    def fake_run(args, stdin=None, capture_output=None, text=None, timeout=None, check=None):
        captured["args"] = args
        captured["timeout"] = timeout
        full_prompt = stdin.read()
        captured["prompt"] = full_prompt
        output_path = full_prompt.rsplit("저장하라: ", 1)[1].strip()
        captured["output_path"] = output_path
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output_text)

        class _FakeCompleted:
            returncode = 0

        return _FakeCompleted()

    return fake_run, captured


def test_generate_reads_output_file_and_cleans_up_tempfiles(monkeypatch):
    fake_run, captured = _fake_run_writes_output("claude 응답 텍스트")
    monkeypatch.setattr(cli_llm_adapter.subprocess, "run", fake_run)

    result = cli_llm_adapter.generate("질문", backend="claude_cli")

    assert result == "claude 응답 텍스트"
    assert captured["args"][0] == settings.claude_cli_path
    assert captured["args"][1:] == ["-p"]
    assert "질문" in captured["prompt"]
    # 프롬프트/결과 tempfile 모두 try/finally로 파기됐어야 한다.
    assert not os.path.exists(captured["output_path"])


def test_generate_codex_cli_uses_exec_subcommand(monkeypatch):
    fake_run, captured = _fake_run_writes_output("codex 응답")
    monkeypatch.setattr(cli_llm_adapter.subprocess, "run", fake_run)

    result = cli_llm_adapter.generate("질문", backend="codex_cli")

    assert result == "codex 응답"
    assert captured["args"][0] == settings.codex_cli_path
    assert captured["args"][1:] == ["exec"]


def test_generate_raises_on_missing_binary(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("바이너리 없음")

    monkeypatch.setattr(cli_llm_adapter.subprocess, "run", fake_run)

    with pytest.raises(cli_llm_adapter.CliLLMError):
        cli_llm_adapter.generate("질문", backend="claude_cli")


def test_generate_raises_on_timeout(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

    monkeypatch.setattr(cli_llm_adapter.subprocess, "run", fake_run)

    with pytest.raises(cli_llm_adapter.CliLLMError):
        cli_llm_adapter.generate("질문", backend="claude_cli")


def test_generate_raises_on_nonzero_exit(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd="claude", stderr="에러 발생")

    monkeypatch.setattr(cli_llm_adapter.subprocess, "run", fake_run)

    with pytest.raises(cli_llm_adapter.CliLLMError):
        cli_llm_adapter.generate("질문", backend="claude_cli")


def test_generate_raises_on_empty_output_file(monkeypatch):
    fake_run, captured = _fake_run_writes_output("")
    monkeypatch.setattr(cli_llm_adapter.subprocess, "run", fake_run)

    with pytest.raises(cli_llm_adapter.CliLLMError):
        cli_llm_adapter.generate("질문", backend="claude_cli")


def test_generate_rejects_unknown_backend():
    with pytest.raises(ValueError):
        cli_llm_adapter.generate("질문", backend="does_not_exist")


# 실 CLI 바이너리가 PATH에 있을 때만 도는 통합 테스트 — 없으면 스킵.
@pytest.mark.skipif(
    shutil.which(settings.claude_cli_path) is None,
    reason="claude CLI가 PATH에 없음",
)
def test_generate_against_real_claude_cli_smoke():
    result = cli_llm_adapter.generate("1+1은 몇이야? 숫자만 답해.", backend="claude_cli")
    assert isinstance(result, str)
    assert len(result) > 0
