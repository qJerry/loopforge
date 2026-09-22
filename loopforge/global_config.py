"""LoopForge 全局配置读写。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_GLOBAL_CONFIG_PATH = Path("~/.loopforge/config.json").expanduser()
DEFAULT_GLOBAL_CONFIG: Dict[str, Any] = {
    "version": 1,
    "notifications": {
        "wecom": {
            "enabled": False,
            "webhook_env": "LOOPFORGE_WECOM_WEBHOOK",
            "webhook_url": "",
        }
    },
    "scan": {
        "daily_minutes": 15,
        "initial_minutes": 45,
        "budget_exceeded_alert_after": 2,
    },
}


def global_config_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return path.expanduser()
    configured = os.environ.get("LOOPFORGE_GLOBAL_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_GLOBAL_CONFIG_PATH


def read_global_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = global_config_path(path)
    if not config_path.exists():
        return _with_metadata(DEFAULT_GLOBAL_CONFIG, config_path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        payload = _merge_config({})
        payload["errors"] = [f"全局配置读取失败：{exc}"]
        return _with_metadata(payload, config_path)
    if not isinstance(raw, dict):
        payload = _merge_config({})
        payload["errors"] = ["全局配置必须是对象"]
        return _with_metadata(payload, config_path)
    return _with_metadata(_merge_config(raw), config_path)


def write_global_config(updates: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = global_config_path(path)
    current = read_global_config(config_path)
    payload = _merge_config({**current, **updates})
    payload.pop("config_path", None)
    payload.pop("exists", None)
    payload.pop("errors", None)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _with_metadata(payload, config_path)


def update_wecom_config(enabled: bool, webhook_env: str = "", webhook_url: str = "", path: Optional[Path] = None) -> Dict[str, Any]:
    current = read_global_config(path)
    notifications = current.get("notifications") if isinstance(current.get("notifications"), dict) else {}
    wecom = notifications.get("wecom") if isinstance(notifications.get("wecom"), dict) else {}
    next_wecom = {
        "enabled": bool(enabled),
        "webhook_env": webhook_env.strip() or str(wecom.get("webhook_env") or "LOOPFORGE_WECOM_WEBHOOK"),
        "webhook_url": webhook_url.strip(),
    }
    return write_global_config(
        {
            "notifications": {
                **notifications,
                "wecom": next_wecom,
            }
        },
        path,
    )


def wecom_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    notifications = config.get("notifications") if isinstance(config.get("notifications"), dict) else {}
    raw = notifications.get("wecom") if isinstance(notifications.get("wecom"), dict) else {}
    return {
        "enabled": raw.get("enabled") is True,
        "webhook_env": str(raw.get("webhook_env") or "LOOPFORGE_WECOM_WEBHOOK").strip() or "LOOPFORGE_WECOM_WEBHOOK",
        "webhook_url": str(raw.get("webhook_url") or "").strip(),
    }


def _merge_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    defaults = json.loads(json.dumps(DEFAULT_GLOBAL_CONFIG))
    notifications = raw.get("notifications") if isinstance(raw.get("notifications"), dict) else {}
    wecom = notifications.get("wecom") if isinstance(notifications.get("wecom"), dict) else {}
    scan = raw.get("scan") if isinstance(raw.get("scan"), dict) else {}
    defaults["version"] = _positive_int(raw.get("version"), defaults["version"])
    defaults["notifications"]["wecom"].update(
        {
            "enabled": wecom.get("enabled") is True,
            "webhook_env": str(wecom.get("webhook_env") or defaults["notifications"]["wecom"]["webhook_env"]),
            "webhook_url": str(wecom.get("webhook_url") or ""),
        }
    )
    defaults["scan"].update(
        {
            "daily_minutes": _positive_int(scan.get("daily_minutes"), defaults["scan"]["daily_minutes"]),
            "initial_minutes": _positive_int(scan.get("initial_minutes"), defaults["scan"]["initial_minutes"]),
            "budget_exceeded_alert_after": _positive_int(
                scan.get("budget_exceeded_alert_after"),
                defaults["scan"]["budget_exceeded_alert_after"],
            ),
        }
    )
    return defaults


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _with_metadata(payload: Dict[str, Any], config_path: Path) -> Dict[str, Any]:
    result = json.loads(json.dumps(payload))
    result["config_path"] = str(config_path)
    result["exists"] = config_path.exists()
    result.setdefault("errors", [])
    return result
