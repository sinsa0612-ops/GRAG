# Claude CLI/Codex CLI를 subprocess로 감싸는 어댑터 — 다른 모듈은 subprocess/CLI의 존재를 모른다.
# AI-INSTRUCTIONS의 File-based IPC 패턴을 그대로 따른다: 프롬프트는 argv가 아니라 tempfile에 써서
# stdin으로 흘려보내고(비로그인 subprocess에서 argv는 ps에 노출되고, 두 CLI 모두 대화형 셸 함수가
# 아닌 PATH의 실바이너리로 stdin 프롬프트를 받는 에이전트형 CLI다), 결과도 stdout(0바이트 버그가
# 있음) 대신 CLI에게 "이 경로에 쓰라"고 지시한 결과 tempfile을 읽는다. 성공/실패 무관 try/finally로
# 두 tempfile을 반드시 파기한다.
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


# CLI 미설치/타임아웃/비정상 종료/빈 결과를 감싸는 전용 예외.
# fail-closed: 원인을 메시지에 명확히 담아 즉시 중단시키고, 조용히 다른 백엔드로 새지 않게 한다.
class CliLLMError(RuntimeError):
    pass


# backend 이름 -> (바이너리 경로, CLI 인자) 매핑. 두 CLI 모두 프롬프트를 stdin으로 받는다.
def _cli_spec(backend: str) -> tuple[str, list[str]]:
    if backend == "claude_cli":
        return settings.claude_cli_path, ["-p"]
    if backend == "codex_cli":
        return settings.codex_cli_path, ["exec"]
    raise ValueError(f"알 수 없는 CLI backend: {backend!r}")


# 프롬프트를 지정된 CLI(claude_cli/codex_cli)에 전달하고 응답 텍스트를 반환한다.
# model은 라우터 시그니처 일관성을 위해 받되, 이번 마일스톤에서는 CLI 자체 기본 모델을 쓰고 무시한다
# (CLI 모델 선택 지원은 범위 밖). response_schema는 구조화 출력 강제가 불가하므로 라우터가 아예
# 넘기지 않는다 — 호출부가 기존 Gemma 대응과 동형으로 원문을 방어적 파싱한다.
def generate(prompt: str, backend: str, model: str | None = None) -> str:
    binary, cli_args = _cli_spec(backend)

    prompt_fd, prompt_path = tempfile.mkstemp(suffix=".txt", prefix="grag_cli_prompt_")
    output_fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="grag_cli_output_")
    os.close(prompt_fd)
    os.close(output_fd)
    try:
        full_prompt = (
            f"{prompt}\n\n결과를 다른 설명 없이 아래 경로 파일에만 순수 텍스트로 작성하고 저장하라: "
            f"{output_path}"
        )
        Path(prompt_path).write_text(full_prompt, encoding="utf-8")

        with open(prompt_path, encoding="utf-8") as prompt_file:
            try:
                subprocess.run(
                    [binary, *cli_args],
                    stdin=prompt_file,
                    capture_output=True,
                    text=True,
                    timeout=settings.cli_llm_timeout_sec,
                    check=True,
                )
            except FileNotFoundError as exc:
                logger.error("CLI 바이너리 없음(fail-closed): %s", exc)
                raise CliLLMError(
                    f"CLI 바이너리를 찾을 수 없음('{binary}') — 설치 여부/PATH(또는 .env의 "
                    f"CLAUDE_CLI_PATH/CODEX_CLI_PATH)를 확인하라: {exc}"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                logger.error("CLI 타임아웃(fail-closed): %s", exc)
                raise CliLLMError(
                    f"CLI({binary}) 호출이 {settings.cli_llm_timeout_sec}초 내에 끝나지 않음: {exc}"
                ) from exc
            except subprocess.CalledProcessError as exc:
                logger.error("CLI 비정상 종료(fail-closed): code=%s stderr=%s", exc.returncode, exc.stderr)
                raise CliLLMError(
                    f"CLI({binary}) 비정상 종료(code={exc.returncode}): {exc.stderr}"
                ) from exc

        output_file = Path(output_path)
        if not output_file.exists() or output_file.stat().st_size == 0:
            logger.error("CLI 결과 파일 없음/빈 파일(fail-closed): %s", output_path)
            raise CliLLMError(f"CLI({binary})가 결과 파일을 남기지 않음: {output_path}")
        return output_file.read_text(encoding="utf-8").strip()
    finally:
        for p in (prompt_path, output_path):
            if os.path.exists(p):
                os.remove(p)
