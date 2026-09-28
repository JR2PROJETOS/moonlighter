import asyncio
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Protocol

import anthropic
from anthropic.types import TextBlock
from moonlighter.core.config import llm_backend, moonlighter_home, validate_codex_models
from moonlighter.core.metrics import record_call


class LLMCaller(Protocol):
    """LLM caller protocol: accepts prompt, model, and optional cache_prefix."""

    async def __call__(self, prompt: str, model: str, cache_prefix: str | None = None) -> str: ...


# Signals that the LLM exhausted its quota/spend limit — worth aborting the
# scan and retrying later, instead of treating it as a per-job error.
SPEND_LIMIT_MARKERS = (
    "spend limit",
    "session limit",
    "quota",
    "rate limit",
    "too many requests",
    "overloaded",
    "429",
    "usage limit",
)

# Word-boundary match so markers only fire on real tokens: bare substring
# matching let "quota" match inside "quotation" and "429" match inside "4290",
# false-positive-aborting the whole scan on unrelated JSON/network errors.
_SPEND_LIMIT_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(marker) for marker in SPEND_LIMIT_MARKERS) + r")\b"
)


def is_spend_limit(exc: Exception) -> bool:
    """True if the exception indicates the LLM's quota/spend limit was exhausted."""
    msg = str(exc).lower()
    return bool(_SPEND_LIMIT_PATTERN.search(msg))


def make_caller(config: dict[str, Any]) -> LLMCaller:
    """Return the appropriate LLM caller based on config['llm_backend'].

    llm_backend: "cli"  → uses the `claude -p` CLI (no API key required)
    llm_backend: "api"  → uses the Anthropic Python SDK (requires ANTHROPIC_API_KEY)
    Default: "cli". Anything else raises ConfigError rather than silently
    selecting a backend the user did not ask for.
    """
    if llm_backend(config) == "cli":
        return _call_cli
    if llm_backend(config) == "codex":
        return make_codex_caller(config)
    return make_api_caller()


# Sandbox posture validated empirically (canary experiment, 2026-07-09 —
# specs/2026-07-09-s2-canary-experiment-results.md): without this, the default
# `-p` reads any file on disk with no permission prompt, sees 168 account MCP
# tools, and injects the operator's global CLAUDE.md into every job evaluation.
# --bare NEVER goes here: it disables OAuth/keychain, the subscription auth
# the entire 'cli' backend exists to use.
_CLI_SANDBOX_ARGS: tuple[str, ...] = (
    "--safe-mode",
    "--no-session-persistence",
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
)


def _cli_workdir() -> Path:
    """Dedicated, neutral cwd for the CLI subprocess — never the repository
    (S-02): a cwd with nothing of value for a compromised agent to explore."""
    workdir = moonlighter_home() / "cli-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o700)
    return workdir


async def _call_cli(prompt: str, model: str, cache_prefix: str | None = None) -> str:
    """Call Claude via the installed `claude` CLI subprocess, sandboxed.

    Uses the active Claude Code session — no API key needed.
    The `model` parameter is ignored (CLI uses whichever model the session provides).
    The cache_prefix is concatenated before the prompt (the CLI doesn't expose cache_control);
    the actual caching effect only exists in the api backend.

    The prompt goes over STDIN, never argv (S-01): this removes the exposure in
    `ps` and makes argument injection structurally impossible (there's no prompt
    in argv to become a flag). The subprocess runs with an explicit set of
    lockdown flags (S-02/S-14/S-15) and in a neutral cwd outside the repository.
    """
    full = f"{cache_prefix}\n\n{prompt}" if cache_prefix is not None else prompt
    # Strip ANTHROPIC_API_KEY so the CLI uses the claude.ai session (subscription)
    # instead of the API key (which requires separate API credits).
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError(
            "the `claude` CLI was not found on PATH. Install it, or put it on PATH — "
            "this project uses the CLI backend (the claude.ai subscription), not an API key."
        )
    start = perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *_CLI_SANDBOX_ARGS,
            "-p",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(_cli_workdir()),
        )
        stdout, stderr = await proc.communicate(input=full.encode())
        if proc.returncode != 0:
            detail = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"claude CLI exited with code {proc.returncode}: {detail[:300]}")
        return stdout.decode()
    finally:
        record_call(perf_counter() - start)


def codex_executable(config: dict[str, Any]) -> str:
    """Resolve an optional executable override without guessing installation paths."""
    command = config.get("codex_path") or "codex"
    executable = shutil.which(str(Path(command).expanduser()))
    if executable is None:
        raise RuntimeError("Codex executable not found; set codex_path or add codex to PATH")
    if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise RuntimeError("Set codex_path to the native Codex executable, not a shell shim")
    return executable


def make_codex_caller(config: dict[str, Any]) -> LLMCaller:
    """Use an existing ChatGPT login, without changing authentication state."""
    validate_codex_models(config)

    async def _call(prompt: str, model: str, cache_prefix: str | None = None) -> str:
        validate_codex_models({"llm_model": model})
        executable = codex_executable(config)
        full = f"{cache_prefix}\n\n{prompt}" if cache_prefix is not None else prompt
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() not in {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"}
        }
        start = perf_counter()
        try:
            with TemporaryDirectory(prefix="moonlighter-codex-") as directory:

                async def run(
                    *args: str, data: bytes | None = None
                ) -> tuple[int | None, bytes, bytes]:
                    proc = await asyncio.create_subprocess_exec(
                        executable,
                        *args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=directory,
                    )
                    try:
                        stdout, stderr = await proc.communicate(input=data)
                    except asyncio.CancelledError:
                        if proc.returncode is None:
                            proc.kill()
                        await proc.communicate()
                        raise
                    return proc.returncode, stdout, stderr

                code, stdout, stderr = await run("login", "status")
                status = (stdout + stderr).decode(errors="replace").lower()
                if code != 0 or "logged in using chatgpt" not in status:
                    raise RuntimeError(
                        "Codex requires an existing ChatGPT login; check codex login status"
                    )
                output = Path(directory) / "final.txt"
                code, stdout, stderr = await run(
                    "--ignore-user-config",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "-c",
                    'model_provider="openai"',
                    "-c",
                    "features.shell_tool=false",
                    "-c",
                    "features.unified_exec=false",
                    "-c",
                    'web_search="disabled"',
                    "--model",
                    model,
                    "--output-last-message",
                    str(output),
                    "-",
                    data=full.encode("utf-8"),
                )
                if code != 0:
                    detail = (stderr + b"\n" + stdout).decode(errors="replace").strip()
                    # Normalize structured error names before truncation, preserving the
                    # existing quota detector without classifying expired auth as quota.
                    limited = is_spend_limit(RuntimeError(detail.replace("_", " ")))
                    prefix = "usage limit: " if limited else ""
                    raise RuntimeError(f"Codex exited with code {code}: {prefix}{detail[:300]}")
                if not output.is_file():
                    raise RuntimeError("Codex did not write a final response")
                final = output.read_text(encoding="utf-8")
                if not final.strip():
                    raise RuntimeError("Codex returned an empty final response")
                return final
        finally:
            record_call(perf_counter() - start)

    return _call


def make_api_caller(max_tokens: int = 2048) -> LLMCaller:
    """Return an async caller that uses the Anthropic Python SDK.

    Requires ANTHROPIC_API_KEY in the environment.
    """
    client = anthropic.AsyncAnthropic()

    async def _call(prompt: str, model: str, cache_prefix: str | None = None) -> str:
        # When cache_prefix is provided, sends two blocks: the static prefix
        # with ephemeral cache_control (marked for caching on Anthropic's side)
        # followed by the dynamic prompt. Without a prefix, sends a plain string (backcompat).
        if cache_prefix is not None:
            content: Any = [
                {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prompt},
            ]
        else:
            content = prompt
        start = perf_counter()
        input_tokens = output_tokens = 0
        try:
            message = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
            block = message.content[0]
            if not isinstance(block, TextBlock):
                raise RuntimeError("unexpected model response (non-text block)")
            return block.text
        finally:
            record_call(
                perf_counter() - start, input_tokens=input_tokens, output_tokens=output_tokens
            )

    return _call
