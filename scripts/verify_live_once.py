#!/usr/bin/env python3
"""Make at most one approved, read-only Garmin MCP acceptance call.

The fixed endpoint must already be running the reviewed safe-health release.
This command never reads local credentials or contacts Garmin directly. A durable
exclusive marker is created BEFORE tools/call. Any existing marker, including an
empty or unfinished one, permanently prevents this command from trying again.
Do not remove the marker to retry an ambiguous result.
"""
import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo


ORIGIN = "https://garmin-mcp-server-kj2q.onrender.com"
HEALTH_URL = ORIGIN + "/health"
MCP_URL = ORIGIN + "/mcp"
DEFAULT_MARKER = Path(__file__).resolve().parents[1] / ".secrets" / "live-acceptance.json"
COOLDOWN_MARGIN_SECONDS = 60
# Free Render instances may need about a minute to wake. This applies only to
# process health, never to Garmin calls, and does not introduce any retries.
HEALTH_READ_TIMEOUT_SECONDS = 85
HEALTH_TOTAL_TIMEOUT_SECONDS = 90
SAFE_ERRORS = frozenset({
    "LOCAL_TOKEN_SEED_REQUIRED", "INVALID_TOKEN_SEED", "GARMIN_RATE_LIMITED",
    "GARMIN_AUTH_REJECTED_LOCAL_RESEED_REQUIRED", "GARMIN_REQUEST_FAILED",
    "LOCAL_LOGIN_FAILED",
})
TIMESTAMP_FIELDS = (
    "login_cooldown_until", "last_success_at", "last_login_at",
    "last_refresh_at", "last_rate_limit_at",
)


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid_timestamp")
    return parsed.astimezone(timezone.utc)


def validate_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_date")
    date.fromisoformat(value)
    return value


def safe_health(payload):
    """Validate only known fields; never propagate arbitrary server strings."""
    if not isinstance(payload, dict):
        raise ValueError("invalid_health")
    result = {}
    for field in ("ok", "tokens_loaded", "authenticated", "server_password_login_enabled"):
        if type(payload.get(field)) is not bool:
            raise ValueError("invalid_health")
        result[field] = payload[field]
    if payload.get("token_source") not in ("gist", "environment", None):
        raise ValueError("invalid_health")
    result["token_source"] = payload.get("token_source")
    count = payload.get("failure_count")
    if type(count) is not int or not 0 <= count <= 1_000_000:
        raise ValueError("invalid_health")
    result["failure_count"] = count
    for field in TIMESTAMP_FIELDS:
        if field not in payload:
            raise ValueError("invalid_health")
        parsed = parse_timestamp(payload[field])
        result[field] = iso(parsed) if parsed is not None else None
    for field in ("persistence_error", "last_error"):
        if field not in payload:
            raise ValueError("invalid_health")
        value = payload[field]
        result[field] = None if value is None else (
            value if isinstance(value, str) and value in SAFE_ERRORS else "UNRECOGNIZED_ERROR"
        )
    return result


def strict_json(text):
    def reject_constant(_value):
        raise ValueError("invalid_json")

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("invalid_json")
            value[key] = item
        return value

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


def health_error_code(exc):
    """Fixed diagnostic labels only: never expose exception text or responses."""
    import httpx

    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "health_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "health_http_error"
    if isinstance(exc, httpx.RequestError):
        return "health_connection_error"
    if isinstance(exc, (ValueError, TypeError, RecursionError)):
        return "health_invalid_response"
    return "health_unknown_error"


def valid_stats_result(result, requested_date):
    """get_stats catches errors as text, so isError=false alone proves nothing."""
    if getattr(result, "isError", None) is not False:
        return False
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        return False
    block = content[0]
    text = getattr(block, "text", None)
    if getattr(block, "type", None) != "text" or not isinstance(text, str) or len(text) > 512 * 1024:
        return False
    try:
        payload = strict_json(text)
    except (ValueError, TypeError, RecursionError):
        return False
    return isinstance(payload, dict) and bool(payload) and payload.get("date") == requested_date


def write_marker(target, report):
    """The marker's existence, not its contents, enforces at-most-once behavior."""
    target.seek(0)
    target.write(json.dumps(report, sort_keys=True, allow_nan=False) + "\n")
    target.truncate()
    target.flush()
    os.fsync(target.fileno())


def post_health_ok(before, after, started, finished):
    success = parse_timestamp(after["last_success_at"])
    previous = parse_timestamp(before["last_success_at"])
    margin = timedelta(seconds=COOLDOWN_MARGIN_SECONDS)
    return (
        after["ok"] is True
        and after["tokens_loaded"] is True
        and after["authenticated"] is True
        and after["server_password_login_enabled"] is False
        and after["token_source"] == "gist"
        and after["failure_count"] == 0
        and after["persistence_error"] is None
        and after["last_error"] is None
        and after["login_cooldown_until"] is None
        and after["last_login_at"] == before["last_login_at"]
        and success is not None
        and started - margin <= success <= finished + margin
        and (previous is None or success > previous)
    )


async def verify_once(*, requested_date, marker_path, fetch_health, call_stats, now=utc_now):
    """Injectable orchestration. Tests supply all network and clock dependencies."""
    try:
        requested_date = validate_date(requested_date)
    except (ValueError, TypeError):
        return {"status": "blocked", "reason": "invalid_date"}
    marker_path = Path(marker_path)
    report = {"date": requested_date, "status": "blocked", "tool_calls": 0}
    if marker_path.exists() or marker_path.is_symlink():
        return {**report, "status": "already_attempted", "reason": "marker_exists_do_not_retry"}
    try:
        before = safe_health(await fetch_health())
    except Exception as exc:
        return {**report, "reason": "preflight_health_unavailable_or_invalid",
                "health_error_code": health_error_code(exc)}
    report["health_before"] = before
    if not (before["ok"] and before["tokens_loaded"]
            and before["server_password_login_enabled"] is False
            and before["token_source"] == "gist"
            and before["persistence_error"] is None):
        return {**report, "reason": "preflight_not_ready"}
    cooldown = parse_timestamp(before["login_cooldown_until"])
    if cooldown is not None:
        eligible_at = cooldown + timedelta(seconds=COOLDOWN_MARGIN_SECONDS)
        if now() < eligible_at:
            return {**report, "status": "waiting", "reason": "cooldown_preserved",
                    "eligible_at": iso(eligible_at)}
    started = now()
    report.update(status="attempt_started", started_at=iso(started))
    try:
        if marker_path.parent.is_symlink():
            return {**report, "status": "blocked", "reason": "marker_parent_not_safe"}
        marker_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(marker_path, flags, 0o600)
    except FileExistsError:
        return {**report, "status": "already_attempted", "reason": "marker_exists_do_not_retry"}
    except OSError:
        return {**report, "status": "blocked", "reason": "marker_creation_failed"}
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        try:
            os.fchmod(target.fileno(), 0o600)
            write_marker(target, report)
        except OSError:
            return {**report, "status": "blocked", "reason": "marker_persistence_failed_do_not_retry"}
        result_valid = False
        report["tool_calls"] = 1
        try:
            result = await call_stats(requested_date)
            result_valid = valid_stats_result(result, requested_date)
            report.update(status="attempted_failed", reason="tool_result_not_validated")
        except Exception:
            report.update(status="attempted_unknown", reason="tool_transport_failure_do_not_retry")
        try:
            after = safe_health(await fetch_health())
            report["health_after"] = after
            if result_valid:
                if post_health_ok(before, after, started, now()):
                    report.update(status="passed", reason="read_only_result_and_persistence_verified")
                else:
                    report.update(status="attempted_failed", reason="postflight_state_not_verified")
        except Exception as exc:
            report["health_error_code"] = health_error_code(exc)
            if result_valid:
                report.update(status="attempted_unknown", reason="postflight_health_unavailable_or_invalid")
        report["finished_at"] = iso(now())
        try:
            write_marker(target, report)
        except OSError:
            return {**report, "status": "attempted_unknown", "reason": "marker_result_save_failed_do_not_retry"}
    return report


async def fetch_live_health():
    import httpx

    # No credentials, redirects, environment proxies, or request retries.
    async with asyncio.timeout(HEALTH_TOTAL_TIMEOUT_SECONDS):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(HEALTH_READ_TIMEOUT_SECONDS, connect=15),
            follow_redirects=False, trust_env=False,
        ) as client:
            response = await client.get(HEALTH_URL)
            response.raise_for_status()
            if len(response.content) > 64 * 1024:
                raise ValueError("health_response_too_large")
            return strict_json(response.text)


async def call_live_stats(requested_date):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    import httpx

    def http_client_factory(**kwargs):
        # The fixed service is public; do not load workstation proxy credentials.
        kwargs.update(trust_env=False, follow_redirects=False)
        return httpx.AsyncClient(**kwargs)

    transport = StreamableHttpTransport(MCP_URL, httpx_client_factory=http_client_factory)
    async with Client(transport, timeout=120, init_timeout=30) as client:
        return await client.call_tool_mcp("get_stats", {"date": requested_date}, timeout=120)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to today's date in Asia/Seoul")
    args = parser.parse_args(argv)
    requested_date = args.date or datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    # Third-party clients can include server payloads in diagnostics. Emit only
    # this command's fixed codes and validated health allowlist, never results.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(verify_once(
            requested_date=requested_date, marker_path=DEFAULT_MARKER,
            fetch_health=fetch_live_health, call_stats=call_live_stats,
        ))
    except (Exception, KeyboardInterrupt):
        report = {"status": "attempted_unknown", "reason": "interrupted_do_not_retry_existing_marker"}
    finally:
        logging.disable(previous_logging_disable)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] in ("passed", "waiting", "already_attempted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
