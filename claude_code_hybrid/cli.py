"""``claude-hybrid``: start the gateway if it is not running, then start
Claude Code pointed at it, on your own Claude login.

    claude-hybrid                         a normal Claude Code session
    claude-hybrid -p "summarize README"   anything after the name goes to claude
    claude-hybrid --continue
    claude-hybrid init                    write the config file (asks for URL and key)
    claude-hybrid status                  is the gateway up, and where does it route
    claude-hybrid stop                    stop the gateway
    claude-hybrid gateway                 run the gateway in the foreground

Configuration, in order of precedence: environment, then the config file
(``claude-hybrid init`` writes it; ``claude-hybrid status`` shows its path):

    HYBRID_UPSTREAM_URL    base URL of the endpoint that serves every non-Claude
                           model, speaking the Anthropic Messages API
                           (e.g. https://openrouter.ai/api, http://localhost:11434,
                           https://api.moonshot.ai/anthropic, a LiteLLM proxy)
    HYBRID_UPSTREAM_KEY    its API key (sent as Bearer and x-api-key)
    HYBRID_PORT            gateway port, default 8290
    HYBRID_CUSTOM_MODEL    a model id to list in Claude Code's /model picker and to
                           exempt from the ~15 s availability check (slow first
                           tokens, e.g. kimi-k3); optional
    HYBRID_CUSTOM_MODEL_NAME  its display name in the picker; optional
    HYBRID_ANTHROPIC_URL   where claude-* goes, default https://api.anthropic.com

What the launcher sets for the ``claude`` process, and only for it:
``ANTHROPIC_BASE_URL=http://127.0.0.1:<port>``; ``ANTHROPIC_API_KEY`` and
``ANTHROPIC_AUTH_TOKEN`` removed (either would replace the subscription
login; the hybrid session *is* the subscription login);
``CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`` so the gateway log can tell a
subagent turn from a main turn. The upstream key is put in the gateway's
environment only; ``claude`` never sees it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from claude_code_hybrid.gateway import DEFAULT_PORT

CONFIG_KEYS = ("HYBRID_UPSTREAM_URL", "HYBRID_UPSTREAM_KEY", "HYBRID_PORT",
               "HYBRID_CUSTOM_MODEL", "HYBRID_CUSTOM_MODEL_NAME", "HYBRID_ANTHROPIC_URL")


def config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "claude-code-hybrid" / "config.env"


def log_path() -> Path:
    return Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp") \
        / "claude-hybrid-gateway.log"


def read_config() -> dict[str, str]:
    """Environment first, then the config file; unset keys absent."""
    values: dict[str, str] = {}
    path = config_path()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in CONFIG_KEYS and v:
                values[k] = v
    for k in CONFIG_KEYS:
        if os.environ.get(k, "").strip():
            values[k] = os.environ[k].strip()
    return values


def port_of(cfg: dict[str, str]) -> int:
    return int(cfg.get("HYBRID_PORT") or DEFAULT_PORT)


def gateway_health(port: int) -> dict | None:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        data = r.json()
        return data if data.get("mode") == "gateway" else None
    except (httpx.HTTPError, ValueError):
        return None


def start_gateway(cfg: dict[str, str]) -> None:
    """Start the gateway detached, with the upstream key in its environment
    only, and wait until it answers."""
    port = port_of(cfg)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HYBRID_")}
    env.update(HYBRID_UPSTREAM_URL=cfg["HYBRID_UPSTREAM_URL"],
               HYBRID_UPSTREAM_KEY=cfg["HYBRID_UPSTREAM_KEY"],
               HYBRID_PORT=str(port), HYBRID_LOG_FILE=str(log_path()))
    if cfg.get("HYBRID_ANTHROPIC_URL"):
        env["HYBRID_ANTHROPIC_URL"] = cfg["HYBRID_ANTHROPIC_URL"]
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, env=env, cwd=str(Path.home()))
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "claude_code_hybrid.gateway"], **kwargs)
    deadline = time.monotonic() + 20
    while gateway_health(port) is None:
        if time.monotonic() > deadline:
            sys.exit(f"claude-hybrid: gateway did not start on port {port}; see {log_path()}")
        time.sleep(0.25)


def stop_gateway(port: int) -> int:
    """Stop whatever listens on the gateway port. Returns processes stopped."""
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True).stdout
        pids = {line.split()[-1] for line in out.splitlines()
                if f"127.0.0.1:{port}" in line and "LISTENING" in line}
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        return len(pids)
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout.split()
    for pid in out:
        subprocess.run(["kill", pid], capture_output=True)
    return len(out)


def cmd_init(argv: list[str]) -> int:
    path = config_path()
    existing = read_config()
    url = _arg(argv, "--upstream-url") or _prompt("Upstream base URL (serves the non-Claude models)",
                                                   existing.get("HYBRID_UPSTREAM_URL"))
    key = _arg(argv, "--upstream-key") or _prompt("Upstream API key", existing.get("HYBRID_UPSTREAM_KEY"),
                                                   secret=True)
    custom = _arg(argv, "--custom-model") or existing.get("HYBRID_CUSTOM_MODEL", "")
    if not url or not key:
        sys.exit("claude-hybrid init: both the URL and the key are required")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"HYBRID_UPSTREAM_URL={url}", f"HYBRID_UPSTREAM_KEY={key}"]
    if custom:
        lines.append(f"HYBRID_CUSTOM_MODEL={custom}")
    for k in ("HYBRID_PORT", "HYBRID_CUSTOM_MODEL_NAME", "HYBRID_ANTHROPIC_URL"):
        if existing.get(k):
            lines.append(f"{k}={existing[k]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    print(f"claude-hybrid: wrote {path}")
    return 0


def cmd_status(cfg: dict[str, str]) -> int:
    port = port_of(cfg)
    print(f"config file: {config_path()} ({'present' if config_path().is_file() else 'absent'})")
    print(f"upstream:    {cfg.get('HYBRID_UPSTREAM_URL') or '(not set)'}")
    print(f"key:         {'set' if cfg.get('HYBRID_UPSTREAM_KEY') else '(not set)'}")
    health = gateway_health(port)
    if health:
        print(f"gateway:     up on 127.0.0.1:{port}, claude-* -> {health['anthropic']}, "
              f"everything else -> {health['upstream']}")
    else:
        print(f"gateway:     not running on 127.0.0.1:{port}")
    print(f"log:         {log_path()}")
    return 0


def run_claude(cfg: dict[str, str], argv: list[str]) -> int:
    claude = shutil.which("claude")
    if not claude:
        sys.exit("claude-hybrid: `claude` is not on PATH (install Claude Code first)")
    port = port_of(cfg)
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"):
        env.pop(name, None)
    env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] = "1"
    if cfg.get("HYBRID_CUSTOM_MODEL") and not env.get("ANTHROPIC_CUSTOM_MODEL_OPTION"):
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = cfg["HYBRID_CUSTOM_MODEL"]
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = \
            cfg.get("HYBRID_CUSTOM_MODEL_NAME") or f"{cfg['HYBRID_CUSTOM_MODEL']} (hybrid)"
    for k in CONFIG_KEYS:
        env.pop(k, None)
    if os.name == "nt":
        return subprocess.call([claude, *argv], env=env)
    os.execve(claude, [claude, *argv], env)
    return 0  # not reached


def _arg(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


def _prompt(label: str, default: str | None, secret: bool = False) -> str:
    shown = " [keep current]" if default else ""
    try:
        if secret:
            import getpass
            value = getpass.getpass(f"{label}{shown}: ")
        else:
            value = input(f"{label}{shown}: ")
    except EOFError:
        value = ""
    return value.strip() or (default or "")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv and argv[0] == "init":
        return cmd_init(argv[1:])
    cfg = read_config()
    if argv and argv[0] == "status":
        return cmd_status(cfg)
    if argv and argv[0] == "stop":
        n = stop_gateway(port_of(cfg))
        print(f"claude-hybrid: stopped {n} gateway process(es) on port {port_of(cfg)}")
        return 0
    missing = [k for k in ("HYBRID_UPSTREAM_URL", "HYBRID_UPSTREAM_KEY") if not cfg.get(k)]
    if missing:
        sys.exit(f"claude-hybrid: {', '.join(missing)} not set; run `claude-hybrid init` "
                 f"or export them ({config_path()})")
    if argv and argv[0] == "gateway":
        os.environ.update(HYBRID_UPSTREAM_URL=cfg["HYBRID_UPSTREAM_URL"],
                          HYBRID_UPSTREAM_KEY=cfg["HYBRID_UPSTREAM_KEY"],
                          HYBRID_PORT=str(port_of(cfg)))
        if cfg.get("HYBRID_ANTHROPIC_URL"):
            os.environ["HYBRID_ANTHROPIC_URL"] = cfg["HYBRID_ANTHROPIC_URL"]
        from claude_code_hybrid.gateway import main as gateway_main
        gateway_main()
        return 0
    if gateway_health(port_of(cfg)) is None:
        start_gateway(cfg)
    return run_claude(cfg, argv)


if __name__ == "__main__":
    sys.exit(main())
