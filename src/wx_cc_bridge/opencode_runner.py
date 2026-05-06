"""Wrap `opencode -p` CLI as an async function.

This mirrors claude_runner's contract so bridge routing can switch engines.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")


@dataclass
class OpenCodeResult:
    text: str
    session_id: str
    error: str | None = None


async def ask(
    prompt: str,
    cwd: Path,
    session_id: str | None = None,
    timeout: float = 600.0,
) -> OpenCodeResult:
    cwd.mkdir(parents=True, exist_ok=True)

    args = [
        OPENCODE_BIN,
        "-p",
        prompt,
        "--output-format",
        "json",
    ]
    if session_id:
        args += ["--resume", session_id]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return OpenCodeResult("", session_id or "", error=f"未找到 `{OPENCODE_BIN}` 命令，检查 PATH")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return OpenCodeResult("", session_id or "", error=f"超时 {timeout}s")

    if proc.returncode != 0:
        return OpenCodeResult(
            "",
            session_id or "",
            error=f"exit={proc.returncode}: {stderr.decode(errors='replace')[:1500]}",
        )

    raw = stdout.decode(errors="replace").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return OpenCodeResult("", session_id or "", error=f"JSON parse: {e}; raw head: {raw[:400]!r}")

    if isinstance(data, dict) and data.get("is_error"):
        return OpenCodeResult("", data.get("session_id") or session_id or "", error=data.get("result", raw[:500]))

    return OpenCodeResult(
        text=(data.get("result") or "").strip(),
        session_id=data.get("session_id") or session_id or "",
    )

