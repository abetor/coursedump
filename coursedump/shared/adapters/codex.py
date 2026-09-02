"""OpenAI Codex CLI adapter with verified command-line flag behavior.

`codex exec [resume <id>] --json -c approval_policy=never [--sandbox ...]
[--model M] [-c model_reasoning_effort=E] [-c model_instructions_file=PATH]
[--output-schema PATH] "prompt"` -> stdout is a JSONL event stream
(thread.started/thread_id, item.completed/agent_message, turn.completed/usage).

Verified constraints: DEVNULL prevents inherited-stdin hangs; resume is the
`exec resume` subcommand and rejects `--sandbox`; approval and effort are
configuration overrides; output schema is a file path and cannot be combined
with resume; omitted model means account default; usage is reported as tokens.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Capabilities, HarnessAdapter, RunResult

# Full and short aliases from the Claude model family.
_CLAUDE_ALIASES = {"sonnet", "opus", "haiku"}


def _claude_family(model: str) -> bool:
    m = model.lower()
    return m.startswith("claude") or m in _CLAUDE_ALIASES


class CodexAdapter(HarnessAdapter):
    name = "codex"

    def __init__(self, binary: str = "codex"):
        self.binary = binary

    def capabilities(self) -> Capabilities:
        # Subagents use TOML definitions; MCP is available as client and server.
        return Capabilities(json_events=True, schema_output=True, native_resume=True,
                            subagents=True, mcp=True)

    def build_cmd(self, prompt, *, model=None, effort=None, resume_session_id=None,
                  schema_path=None, system_prompt_path=None, allowed_tools=None):
        # Codex has no per-tool allowlist; the sandbox controls availability.
        if resume_session_id and schema_path:
            # Fail closed: choose resume with post-validation or a fresh
            # schema-constrained session.
            raise ValueError("codex: schema and resume are incompatible (codex#14343); choose one")
        cmd = [self.binary, "exec"]
        if resume_session_id:
            cmd += ["resume", resume_session_id]  # Subcommand, not --resume.
            # `exec resume` requires a configuration override for sandbox mode.
            sandbox = ["-c", "sandbox_mode=workspace-write"]
        else:
            sandbox = ["--sandbox", "workspace-write"]
        # Autonomous headless run with no approval prompts.
        cmd += ["--json", "-c", "approval_policy=never", *sandbox]
        # Working directories may intentionally be outside a Git repository.
        cmd += ["--skip-git-repo-check"]
        # Omit Claude-family model names and use the account default instead.
        if model and not _claude_family(model):
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]  # Configuration override.
        if system_prompt_path:
            cmd += ["-c", f"model_instructions_file={system_prompt_path}"]
        if schema_path:
            schema = Path(schema_path).read_text("utf-8").strip()
            if not schema:
                raise ValueError(f"empty schema {schema_path}; refusing an unconstrained call")
            cmd += ["--output-schema", schema_path]  # Codex accepts a schema file path.
        cmd.append(prompt)
        return cmd

    def parse_output(self, stdout, exit_code) -> RunResult:
        """Parse a JSONL stream into text, session ID, usage, and errors.

        Join text blocks rather than their repr, skip malformed lines, and
        preserve raw output as a failure when no valid JSON event exists.
        """
        text_parts: list[str] = []
        usage = err = session_id = None
        parsed_any = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(ev, dict):
                continue
            parsed_any = True
            t = ev.get("type")
            if t == "thread.started":
                session_id = ev.get("thread_id")  # Codex resume session identifier.
                continue
            item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
            itype = item.get("item_type") or item.get("type")
            if t == "item.completed" and itype == "agent_message":
                txt = item.get("text")
                if txt is None:
                    txt = item.get("content")
                if isinstance(txt, list):
                    txt = "".join(b.get("text", "") for b in txt if isinstance(b, dict))
                if txt:
                    text_parts.append(str(txt))
            elif t == "turn.completed":
                usage = ev.get("usage")
            elif t == "error" or ev.get("is_error"):
                err = str(ev.get("message") or ev.get("error") or "codex error")
        if not parsed_any:
            # Preserve non-JSONL output from failures before format initialization.
            return RunResult(ok=False, text=stdout or "", exit_code=exit_code)
        ok = exit_code == 0 and err is None
        # Keep error in raw so the shared classifier can inspect it.
        return RunResult(ok=ok, text="\n".join(text_parts), exit_code=exit_code,
                         session_id=session_id, cost_usd=None,
                         raw={"usage": usage, "error": err or ""})
