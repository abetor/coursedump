"""Port for interchangeable command-line agent harnesses.

The minimum contract is a headless one-shot process with prompt and cwd, a
text result on stdout, and a distinguishable failure. Capability flags describe
native support; wrappers must not claim capabilities a harness lacks. Stop
classification is output-based because these CLIs expose no quota query API.
"""
from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

# Common CLI-harness patterns; adapters may extend quota_patterns(). Separate
# failures by recovery action: subscription quota requires waiting for a reset
# window, while request throttling is transient and should be retried soon.
# Observed quota forms include:
# claude - "Claude usage limit reached ... resets at", "You've hit your weekly limit",
# "5-hour limit reached"; codex - "You've hit your usage limit", "out of credits".
# The negative lookbehind distinguishes "usage limit exceeded" from the
# transient phrase "rate limit exceeded".
_QUOTA = re.compile(r"usage limit|weekly limit|session limit|5-hour|out of credits|quota"
                    r"|(?:hit|reached) your [^.\n]{0,24}limit"
                    r"|(?<!rate[ -])\blimit (?:reached|exceeded)", re.I)
# Transient failures include request throttling and network/server errors. Quota
# is checked first for mixed messages such as "429: usage limit reached" because
# repeatedly hitting a true quota is worse than waiting once unnecessarily.
_TRANSIENT = re.compile(r"\b(?:429|500|502|503|529)\b|too many requests|rate.?limit"
                        r"|overloaded|timed?.?out|connection|"
                        r"temporarily|try again|ECONNRESET|EAI_AGAIN", re.I)

# Map stop classes to the shared CLI exit-code contract.
STOP_TO_EXIT = {"done": 0, "quota": 75, "transient": 111, "fatal": 1}


@dataclass
class Capabilities:
    """Capabilities provided natively; wrappers may emulate the rest."""
    json_events: bool
    schema_output: bool
    native_resume: bool
    subagents: bool
    mcp: bool


@dataclass
class RunResult:
    ok: bool
    text: str
    exit_code: int
    session_id: Optional[str] = None
    stop: str = "done"                 # done | quota | transient | fatal
    cost_usd: Optional[float] = None
    raw: dict = field(default_factory=dict)
    stderr: str = ""


class HarnessAdapter(ABC):
    """Per-CLI command construction and output parsing; run() is shared."""
    name: str = "harness"

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def build_cmd(self, prompt: str, *, model: Optional[str] = None,
                  effort: Optional[str] = None, resume_session_id: Optional[str] = None,
                  schema_path: Optional[str] = None, system_prompt_path: Optional[str] = None,
                  allowed_tools: Optional[str] = None) -> list[str]: ...

    @abstractmethod
    def parse_output(self, stdout: str, exit_code: int) -> RunResult: ...

    def quota_patterns(self) -> Optional[re.Pattern]:
        """Return per-CLI quota indicators in addition to common patterns."""
        return None

    def classify(self, result: RunResult) -> str:
        """Classify done, quota, transient, or fatal in contract order.

        Quota is checked even on exit zero because a harness may return a limit
        message as successful output. Success is checked before transient
        patterns so collected material mentioning rate limits cannot turn a
        successful run into a retry. Raw subtype/api_error_status/error fields
        are part of the adapter contract.
        """
        blob = f"{result.text}\n{result.stderr}\n{result.raw.get('subtype', '')}\n" \
               f"{result.raw.get('api_error_status', '')}\n{result.raw.get('error', '')}"
        extra = self.quota_patterns()
        if _QUOTA.search(blob) or (extra and extra.search(blob)):
            return "quota"
        if result.ok:
            return "done"
        if _TRANSIENT.search(blob):
            return "transient"
        return "fatal"

    def run(self, prompt: str, *, cwd: str, timeout: Optional[float] = None,
            **build_kw) -> RunResult:
        """Run one harness process; this is the only process-spawn point."""
        cmd = self.build_cmd(prompt, **build_kw)
        try:
            # DEVNULL prevents a headless subprocess from waiting for inherited
            # stdin until EOF.
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return RunResult(ok=False, text=out, exit_code=-1,
                             stderr="wall-timeout", stop="transient")
        res = self.parse_output(proc.stdout, proc.returncode)
        res.stderr = proc.stderr or ""
        res.stop = self.classify(res)
        if res.stop != "done":
            res.ok = False
        return res
