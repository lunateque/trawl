import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from trawl.config.models import ConfigError


def command(config: Path) -> tuple[str, ...]:
    return (sys.executable, "-m", "trawl", "--config", str(config.resolve()), "run", "--watch")


def windows_xml(args: tuple[str, ...], user: str) -> str:
    task = Element(
        "Task", {"version": "1.2", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    )
    trigger = SubElement(SubElement(task, "Triggers"), "LogonTrigger")
    SubElement(trigger, "UserId").text = user
    principal = SubElement(SubElement(task, "Principals"), "Principal", {"id": "Owner"})
    SubElement(principal, "UserId").text = user
    SubElement(principal, "LogonType").text = "InteractiveToken"
    settings = SubElement(task, "Settings")
    SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
    SubElement(settings, "Hidden").text = "true"
    restart = SubElement(settings, "RestartOnFailure")
    SubElement(restart, "Interval").text = "PT1M"
    SubElement(restart, "Count").text = "3"
    action = SubElement(SubElement(task, "Actions", {"Context": "Owner"}), "Exec")
    executable = Path(args[0])
    # pythonw keeps the console hidden even for an interactive logon trigger.
    SubElement(action, "Command").text = str(executable.with_name("pythonw.exe"))
    SubElement(action, "Arguments").text = subprocess.list2cmdline(args[1:])
    return tostring(task, encoding="unicode")


def systemd_unit(args: tuple[str, ...]) -> str:
    executable = " ".join(
        json.dumps(arg.replace("%", "%%").replace("$", "$$"), ensure_ascii=False) for arg in args
    )
    return (
        "[Unit]\nDescription=trawl music library\n\n[Service]\nType=simple\n"
        f"ExecStart={executable}\nRestart=on-failure\nTimeoutStopSec=infinity\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _call(binary: str, *args: str) -> int:
    if not (executable := shutil.which(binary)):
        raise ConfigError(f"{binary} was not found")
    return subprocess.run([executable, *args], check=False).returncode  # noqa: S603


def _windows(action: str, args: tuple[str, ...]) -> int:
    if action == "install":
        with tempfile.TemporaryDirectory(prefix="trawl-task-") as temp:
            path = Path(temp) / "trawl.xml"
            user = getpass.getuser()
            if domain := os.environ.get("USERDOMAIN"):
                user = f"{domain}\\{user}"
            path.write_text(windows_xml(args, user), encoding="utf-16")
            return _call("schtasks", "/Create", "/TN", "trawl", "/XML", str(path), "/F")
    if action == "uninstall":
        return _call("schtasks", "/Delete", "/TN", "trawl", "/F")
    return _call("schtasks", "/Query", "/TN", "trawl")


def _linux(action: str, args: tuple[str, ...]) -> int:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    unit = config / "systemd" / "user" / "trawl.service"
    if action == "install":
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(args), encoding="utf-8")
        if code := _call("systemctl", "--user", "daemon-reload"):
            return code
        return _call("systemctl", "--user", "enable", "--now", "trawl")
    if action == "uninstall":
        if status := _call("systemctl", "--user", "disable", "--now", "trawl"):
            return status
        unit.unlink(missing_ok=True)
        return _call("systemctl", "--user", "daemon-reload")
    return _call("systemctl", "--user", "status", "trawl")


def service(action: str, config: Path) -> int:
    if sys.platform == "win32":
        return _windows(action, command(config))
    if sys.platform == "linux":
        return _linux(action, command(config))
    raise ConfigError("Service installation is supported on Windows and Linux")
