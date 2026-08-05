from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import sys

from .command import CommandRunner
from .config import AppConfig, load_config
from .errors import FixAgentError


_LABEL = "com.inswave.code-fix-agent"
_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def launchd_environment(config: AppConfig) -> dict[str, str]:
    environment = {"PATH": _PATH}
    required_names: set[str] = set()
    if config.server.token_env:
        required_names.add(config.server.token_env)
    optional_names = {
        repository.github_token_env
        for repository in config.repositories
        if repository.github_token_env
    }
    for repository in config.repositories:
        discord = repository.discord
        if not discord.enabled:
            continue
        if discord.webhook_url_env:
            required_names.add(discord.webhook_url_env)
        if discord.webhook_token_env:
            required_names.add(discord.webhook_token_env)
    for name in sorted(required_names):
        value = os.environ.get(name)
        if not value:
            raise FixAgentError(f"required environment variable is not set: {name}")
        environment[name] = value
    for name in sorted(optional_names):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def launchd_payload(
    config: AppConfig,
    config_path: Path,
    executable: Path,
    log_dir: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    return {
        "Label": _LABEL,
        "ProgramArguments": [
            str(executable),
            "serve",
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": str(config_path.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "code-fix-agent.out.log"),
        "StandardErrorPath": str(log_dir / "code-fix-agent.err.log"),
        "EnvironmentVariables": environment,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Umask": 0o077,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fix-agent-launchd")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--launch-agents-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
        executable = Path(sys.argv[0]).resolve().parent / "fix-agent"
        if not executable.is_file():
            discovered = shutil.which("fix-agent")
            if not discovered:
                raise FixAgentError("fix-agent executable was not found")
            executable = Path(discovered).resolve()
        output_dir = (args.output_dir or config.state_dir / "launchd").resolve()
        launch_agents_dir = (
            args.launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
        ).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        environment = launchd_environment(config)
        payload = launchd_payload(
            config,
            config_path,
            executable,
            (Path.home() / "Library" / "Logs").resolve(),
            environment,
        )
        generated_path = output_dir / f"{_LABEL}.plist"
        generated_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))
        generated_path.chmod(0o600)
        print(f"generated {generated_path}")
        if not args.install:
            return 0
        if sys.platform != "darwin":
            raise FixAgentError("launchd installation is supported only on macOS")
        launch_agents_dir.mkdir(parents=True, exist_ok=True)
        installed_path = launch_agents_dir / generated_path.name
        domain = f"gui/{os.getuid()}"
        runner = CommandRunner()
        runner.run(
            ["launchctl", "bootout", f"{domain}/{_LABEL}"], check=False
        )
        shutil.copy2(generated_path, installed_path)
        installed_path.chmod(0o600)
        runner.run(["launchctl", "bootstrap", domain, str(installed_path)])
        print(f"installed {_LABEL} from {installed_path}")
        return 0
    except (FixAgentError, OSError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
