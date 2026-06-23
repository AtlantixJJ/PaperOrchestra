#!/usr/bin/env python3
"""
common_subagent.py — Run subagents with full logging and markdown conversion.
"""
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def reorder_fields(value: Any) -> Any:
    if isinstance(value, dict):
        reordered: Dict[str, Any] = {}
        for key, subval in value.items():
            if key not in ("old_string", "new_string"):
                reordered[key] = reorder_fields(subval)
        if "old_string" in value:
            reordered["old_string"] = reorder_fields(value["old_string"])
        if "new_string" in value:
            reordered["new_string"] = reorder_fields(value["new_string"])
        return reordered
    if isinstance(value, list):
        return [reorder_fields(item) for item in value]
    return value


def flush_buffer(lines: List[str], chunks: List[str]) -> None:
    if not chunks:
        return
    text = "".join(chunks).strip()
    if not text:
        chunks.clear()
        return
    lines.append(text)
    lines.append("")
    chunks.clear()


def format_triple_quoted_field(key_text: str, value: str, indent: int) -> str:
    pad = " " * indent
    return f'{pad}{key_text}:\n{pad}"""\n{value}\n{pad}"""'


def dumps_with_triple_quotes(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines: List[str] = ["{"]
        items = list(value.items())
        for idx, (key, subval) in enumerate(items):
            key_text = json.dumps(key, ensure_ascii=True)
            is_last = idx == len(items) - 1
            if key in ("old_string", "new_string") and isinstance(subval, str):
                block = format_triple_quoted_field(key_text, subval, indent + 2)
                if not is_last:
                    block += ","
                lines.append(block)
            else:
                subtext = dumps_with_triple_quotes(subval, indent + 2)
                entry = f'{" " * (indent + 2)}{key_text}: {subtext}'
                if not is_last:
                    entry += ","
                lines.append(entry)
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for idx, item in enumerate(value):
            item_text = dumps_with_triple_quotes(item, indent + 2)
            line = f'{" " * (indent + 2)}{item_text}'
            if idx != len(value) - 1:
                line += ","
            lines.append(line)
        lines.append(f"{pad}]")
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=True)


def format_tool_use(record: Dict[str, Any]) -> str:
    payload = reorder_fields(
        {
            "tool_name": record.get("tool_name"),
            "parameters": record.get("parameters", {}),
        }
    )
    formatted = dumps_with_triple_quotes(payload, indent=0)
    return "```json\n" + formatted + "\n```"


def parse_log_to_markdown(input_path: Path) -> str:
    prompt_path = input_path.with_suffix(".prompt.txt")
    meta_path = input_path.with_suffix(".meta")
    out_lines: List[str] = [
        f"# Parsed Messages from `{input_path.name}`",
        "",
    ]

    if prompt_path.exists():
        prompt_text = prompt_path.read_text(encoding="utf-8").rstrip()
        out_lines.extend(
            [
                "## Prompt",
                "",
                "```text",
                prompt_text,
                "```",
                "",
            ]
        )

    if meta_path.exists():
        meta_text = meta_path.read_text(encoding="utf-8").rstrip()
        out_lines.extend(
            [
                "## Meta",
                "",
                "```text",
                meta_text,
                "```",
                "",
            ]
        )

    out_lines.extend(["## Transcript", ""])

    current_role: Optional[str] = None
    current_chunks: List[str] = []

    for raw_line in input_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        rtype = record.get("type")

        if rtype == "message":
            role = record.get("role", "unknown")
            content = record.get("content", "")
            delta = bool(record.get("delta", False))

            if delta:
                if current_role is None:
                    current_role = role
                if role != current_role:
                    flush_buffer(out_lines, current_chunks)
                    current_role = role
                current_chunks.append(content)
                continue

            flush_buffer(out_lines, current_chunks)
            current_role = None
            text = str(content).strip()
            if text:
                out_lines.append(text)
                out_lines.append("")
            continue

        if rtype == "tool_use":
            flush_buffer(out_lines, current_chunks)
            current_role = None
            out_lines.append(format_tool_use(record))
            out_lines.append("")
            continue

    flush_buffer(out_lines, current_chunks)
    return "\n".join(out_lines).rstrip() + "\n"

def extract_model_response(input_path: Path) -> str:
    """Extracts only the model's text response for the caller, ignoring tools."""
    chunks = []
    current_role = None
    final_text = ""
    
    if not input_path.exists():
        return ""
        
    for raw_line in input_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
            
        if record.get("type") == "message":
            role = record.get("role", "unknown")
            if role == "assistant":
                if record.get("delta"):
                    chunks.append(record.get("content", ""))
                else:
                    text = str(record.get("content", "")).strip()
                    if text:
                        final_text += text + "\n"
                        
    if chunks:
        final_text += "".join(chunks).strip() + "\n"
        
    return final_text

def run_subagent(command: str, prompt: str, timeout: int, cwd: Path, log_name: str) -> tuple[int, str, str]:
    """
    Run a subagent command (e.g. agy or Gemini CLI), capturing stdout/stderr
    and saving logs, metadata, and prompts into a ./logs folder just
    like the run_subagent shell script.
    """
    log_dir = cwd / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    prompt_path = log_dir / f"{log_name}.prompt.txt"
    log_path = log_dir / f"{log_name}.log"
    meta_path = log_dir / f"{log_name}.meta"
    md_path = log_dir / f"{log_name}.md"
    
    cmd = shlex.split(command)
    
    backend = cmd[0].lower()
    if backend == "agy" and not any(flag in command for flag in ("-p", "--print", "--prompt")):
        cmd.extend(["-p", prompt])
    elif backend == "gemini" and "--output-format" not in command:
        cmd.extend(["-y", "--output-format", "stream-json", "-p", prompt])
    elif backend == "codex" and "--json" not in command:
        cmd.extend(["exec", "--ephemeral", "--json", prompt])
    elif backend == "claude" and "--output-format" not in command:
        cmd.extend(["--model", "claude-sonnet-4-6", "--output-format", "stream-json", "-p", prompt])
    else:
        cmd.extend(["-p", prompt])
        
    meta_content = f"backend={backend}\ncwd={cwd}\nlog={log_path}\nprompt_file={prompt_path}\ncmd={shlex.join(cmd)}\n"
    meta_path.write_text(meta_content, encoding="utf-8")
    
    print(f"Executing subagent: {shlex.join(cmd)}")
    with open(log_path, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            text=True,
            bufsize=1,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        stdout_chunks = []
        for char in iter(lambda: proc.stdout.read(1), ""):
            f.write(char)
            f.flush()
            stdout_chunks.append(char)
            
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            
    proc_stdout = "".join(stdout_chunks)
    proc_stderr = ""
    proc_returncode = proc.returncode
    
    # Generate the Markdown transcript
    try:
        md_content = parse_log_to_markdown(log_path)
        md_path.write_text(md_content, encoding="utf-8")
    except Exception as e:
        print(f"WARN: Failed to convert log to markdown: {e}")
        
    # Extract the clean model response to return as stdout to the caller
    # If the backend didn't use stream-json (e.g. raw text), fallback to raw stdout
    if backend in ("gemini", "codex"):
        clean_stdout = extract_model_response(log_path)
        if not clean_stdout.strip(): # Fallback if parsing failed
            clean_stdout = proc_stdout
    else:
        clean_stdout = proc_stdout
            
    return proc_returncode, clean_stdout, proc_stderr
