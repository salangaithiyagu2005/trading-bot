from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sysconfig
from contextlib import asynccontextmanager
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from nsebot import logstore
from nsebot.calendar_mod import is_weekend, is_holiday
from nsebot.clock import IST, now_ist
from nsebot.rules import RulesValidationError, load_scheduled, save_new_version, validate

security = HTTPBasic(auto_error=False)
DEFAULT_POLL_MS = "2000"
LOCKED_KEYS = {"LIVE_TRADING", "UI_PASSWORD"}
DEFAULT_SETTINGS = {"kite_redirect_url": "", "ui_poll_interval_ms": DEFAULT_POLL_MS}


class SettingsUpdate(BaseModel):
    kite_redirect_url: str | None = None
    ui_poll_interval_ms: int | str | None = None


class DataFileUpdate(BaseModel):
    name: str
    content: str


class TokenUpdate(BaseModel):
    mode: str
    token: str | None = None
    user_id: str | None = None


class ModeUpdate(BaseModel):
    mode: str
    confirm: bool


class RulesUpdate(BaseModel):
    yaml: str
    note: str = ""


class ReviewUpdate(BaseModel):
    day: str
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    source: str = "operator"


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./var")).resolve()


def _repo_data_dir() -> Path:
    return Path(os.environ.get("REPO_DATA_DIR", Path(__file__).resolve().parents[3] / "data")).resolve()


def _ui_dir() -> Path:
    source_dir = Path(__file__).resolve().parents[3] / "ui"
    if source_dir.is_dir():
        return source_dir
    return Path(sysconfig.get_path("data")) / "share" / "nsebot" / "ui"


def _connect():
    conn = logstore.connect(_data_dir())
    return conn


def _effective_credentials(data_dir: Path) -> dict[str, str]:
    return logstore.load_credentials(data_dir)


def _token_file(data_dir: Path) -> Path:
    return data_dir / "token.json"


def _read_token(data_dir: Path) -> dict | None:
    path = _token_file(data_dir)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="token_file_invalid") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("access_token"), str)
        or not value["access_token"].strip()
    ):
        raise HTTPException(status_code=500, detail="token_file_invalid")
    return value


def token_status(data_dir: Path, now: datetime | None = None) -> dict:
    token = _read_token(data_dir)
    if token is None:
        return {"status": "MISSING", "user_id": None, "created_at": None, "expires_at": None}
    try:
        created = datetime.fromisoformat(token["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="token_file_invalid") from exc
    if created.tzinfo is None:
        raise HTTPException(status_code=500, detail="token_file_invalid")
    current = (now or now_ist()).astimezone(IST)
    expiry = datetime.combine(current.date(), time(6), tzinfo=IST)
    expired = created.astimezone(IST) < expiry
    return {
        "status": "EXPIRED" if expired else "PRESENT",
        "user_id": token.get("user_id"),
        "created_at": created.isoformat(),
        "expires_at": expiry.isoformat(),
    }


def _write_token(data_dir: Path, access_token: str, user_id: str | None) -> None:
    if not access_token.strip():
        raise HTTPException(status_code=400, detail="token_required")
    payload = {
        "access_token": access_token.strip(),
        "user_id": user_id or "",
        "created_at": now_ist().isoformat(),
    }
    logstore._atomic_write(
        _token_file(data_dir),
        (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
        0o600,
    )


async def _exchange_request_token(
    request_token: str,
    credentials: dict[str, str],
) -> dict:
    api_key = credentials.get("KITE_API_KEY")
    api_secret = credentials.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="kite_credentials_missing")
    checksum = hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode("utf-8")
    ).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.kite.trade/session/token",
                data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
                headers={"X-Kite-Version": "3"},
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="kite_token_exchange_failed") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("access_token"):
        raise HTTPException(status_code=502, detail="kite_token_exchange_invalid_response")
    return data


def _scheduled_doc(conn) -> dict:
    rows = logstore.list_rule_versions(conn, status="scheduled", descending=True)
    if not rows:
        load_scheduled(conn)
        rows = logstore.list_rule_versions(conn, status="scheduled", descending=True)
    if not rows:
        raise RuntimeError("No scheduled rules version exists")
    return yaml.safe_load(rows[0]["yaml"])


def _credential_summary(data_dir: Path) -> dict:
    values = _effective_credentials(data_dir)
    return {
        "KITE_API_KEY": logstore.mask_secret(values.get("KITE_API_KEY")),
        "KITE_API_SECRET": bool(values.get("KITE_API_SECRET")),
        "XAI_API_KEY": bool(values.get("XAI_API_KEY")),
        "ALERT_WEBHOOK": logstore.mask_secret(values.get("ALERT_WEBHOOK")),
        "LIVE_TRADING": os.environ.get("LIVE_TRADING", "false").strip().lower() == "true",
        "DATA_DIR": str(data_dir),
    }


def _datafile_state(data_dir: Path) -> dict:
    result: dict[str, dict] = {}
    for name in sorted(logstore.DATA_FILES):
        path = data_dir / name
        if not path.exists():
            result[name] = {"content": "", "count": 0}
            continue
        content = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.splitlines()
                 if line.strip() and not line.strip().startswith("#")]
        result[name] = {"content": content, "count": len(lines)}
    return result


def _system_health(conn, data_dir: Path, now: datetime | None = None) -> dict:
    current = (now or now_ist()).astimezone(IST)
    engine = logstore.load_engine_status(conn)
    heartbeat_age: float | None = None
    if engine and engine.get("heartbeat_at"):
        try:
            heartbeat = datetime.fromisoformat(engine["heartbeat_at"]).astimezone(IST)
            heartbeat_age = max(0.0, (current - heartbeat).total_seconds())
        except ValueError:
            heartbeat_age = None
    try:
        holiday_today = is_holiday(current.date(), data_dir / "nse_holidays.txt")
        calendar_valid = True
    except ValueError:
        holiday_today = True
        calendar_valid = False
    in_hours = (
        not is_weekend(current.date())
        and not holiday_today
        and time(9, 15) <= current.time().replace(tzinfo=None) < time(15, 30)
    )
    last_tick_age: float | None = None
    if engine and engine.get("last_tick_at"):
        try:
            last_tick = datetime.fromisoformat(engine["last_tick_at"]).astimezone(IST)
            last_tick_age = max(0.0, (current - last_tick).total_seconds())
        except ValueError:
            last_tick_age = None
    return {
        "engine_alive": heartbeat_age is not None and heartbeat_age <= 15,
        "heartbeat_age_sec": round(heartbeat_age, 2) if heartbeat_age is not None else None,
        "engine_state": engine.get("state") if engine else None,
        "token_verify_status": engine.get("token_verify_status", "unverified") if engine else "unverified",
        "token_verified_at": engine.get("token_verified_at") if engine else None,
        "token_verify_error": engine.get("token_verify_error") if engine else None,
        "available_margin_inr": engine.get("available_margin_inr") if engine else None,
        "ws_connected": bool(engine and engine.get("ws_connected")),
        "ws_subscribed_count": engine.get("ws_subscribed_count", 0) if engine else 0,
        "tick_count": engine.get("tick_count", 0) if engine else 0,
        "last_tick_at": engine.get("last_tick_at") if engine else None,
        "last_tick_age_sec": round(last_tick_age, 2) if last_tick_age is not None else None,
        "ticker_live": bool(
            in_hours and engine and engine.get("ws_connected")
            and last_tick_age is not None
            and current.time().replace(tzinfo=None) >= time(9, 15, 5)
            and last_tick_age <= 2
        ),
        "market_hours": in_hours,
        "calendar_valid": calendar_valid,
        "nifty_spot_last": engine.get("nifty_spot_last") if engine else None,
        "nifty_fut_last": engine.get("nifty_fut_last") if engine else None,
        "warmup_status": engine.get("warmup_status", "pending") if engine else "pending",
        "last_1m_bar_ts": engine.get("last_1m_bar_ts") if engine else None,
        "last_5m_bar_ts": engine.get("last_5m_bar_ts") if engine else None,
        "or_complete": bool(engine and engine.get("or_complete")),
    }


def _authorize(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    configured = os.environ.get("UI_PASSWORD", "")
    if not configured or credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = hmac.compare_digest(credentials.username.encode(), b"operator")
    password_ok = hmac.compare_digest(credentials.password.encode(), configured.encode())
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not os.environ.get("UI_PASSWORD"):
            raise RuntimeError("UI_PASSWORD must be set before the API starts")
        data_dir = _data_dir()
        logstore.seed_data_files(_repo_data_dir(), data_dir)
        conn = _connect()
        try:
            load_scheduled(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(title="NSE Bot Operator API", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.oauth_state = None
    app.state.engine = None
    static_dir = _ui_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def ui(_user: str = Depends(_authorize)):
        file = _ui_dir() / "index.html"
        if not file.is_file():
            raise HTTPException(status_code=404, detail="ui_not_built")
        return FileResponse(file)

    @app.get("/login")
    def login(request: Request, _user: str = Depends(_authorize)):
        credentials = _effective_credentials(_data_dir())
        api_key = credentials.get("KITE_API_KEY")
        if not api_key:
            raise HTTPException(status_code=400, detail="kite_api_key_missing")
        conn = _connect()
        try:
            settings = logstore.get_app_settings(conn)
        finally:
            conn.close()
        redirect_url = settings.get("kite_redirect_url") or str(request.url_for("kite_callback"))
        app.state.oauth_state = secrets.token_urlsafe(24)
        query = urlencode({
            "v": "3",
            "api_key": api_key,
            "redirect_url": redirect_url,
            "state": app.state.oauth_state,
        })
        return RedirectResponse(f"https://kite.zerodha.com/connect/login?{query}")

    @app.get("/kite/callback", name="kite_callback")
    async def kite_callback(request: Request):
        request_token = request.query_params.get("request_token")
        state = request.query_params.get("state")
        if not request_token or not state or not app.state.oauth_state:
            raise HTTPException(status_code=400, detail="oauth_callback_invalid")
        if not hmac.compare_digest(str(state), str(app.state.oauth_state)):
            raise HTTPException(status_code=400, detail="oauth_state_mismatch")
        app.state.oauth_state = None
        data_dir = _data_dir()
        data = await _exchange_request_token(request_token, _effective_credentials(data_dir))
        _write_token(data_dir, data["access_token"], data.get("user_id"))
        conn = _connect()
        try:
            logstore.enqueue_command(conn, "RELOAD_TOKEN")
        finally:
            conn.close()
        return RedirectResponse("/#settings")

    @app.get("/api/session")
    def get_session(_user: str = Depends(_authorize)):
        data_dir = _data_dir()
        today = now_ist().date().isoformat()
        conn = _connect()
        try:
            session = logstore.get_session(conn, today)
            scheduled = _scheduled_doc(conn)
            paper_count = logstore.count_trades(conn, mode="paper")
            row = dict(session) if session else {}
            health = _system_health(conn, data_dir)
            bias_rows = [
                event for event in logstore.list_events(conn, day=today)
                if event["reason"] in {"bias_bullish", "bias_bearish", "bias_sideways"}
            ]
            bias_row = bias_rows[-1] if bias_rows else None
            bias_lines = json.loads(bias_row["snapshot_json"]).get("lines", []) if bias_row else []
            open_trades = logstore.list_trades(conn, day=today, open_only=True)
            open_trade = open_trades[0] if open_trades else None
        finally:
            conn.close()
        live_allowed = False
        return {
            "active_mode": row.get("mode", "paper"),
            "scheduled_mode": scheduled["risk"]["trading_mode"],
            "day": today,
            "server_live_allowed": live_allowed,
            "paper_trade_count": paper_count,
            "token_status": token_status(data_dir),
            "health": health,
            "state": row.get("state", "IDLE"),
            "user_armed": bool(row.get("user_armed", 0)),
            "block_reason": row.get("block_reason"),
            "bias": row.get("bias"),
            "score": row.get("score"),
            "veto": row.get("veto"),
            "bias_lines": bias_lines,
            "winning_sectors": json.loads(row["winning_sectors"]) if row.get("winning_sectors") else [],
            "top20": json.loads(row["top20_json"]) if row.get("top20_json") else [],
            "shortlist": json.loads(row["shortlist_json"]) if row.get("shortlist_json") else [],
            "active_underlying": open_trade["underlying"] if open_trade else None,
            "max_risk_inr_per_trade": scheduled["risk"]["max_risk_inr_per_trade"],
            "risk_inr": (
                str((Decimal(open_trade["entry_price"]) - Decimal(open_trade["stop_trigger"]))
                    * open_trade["qty"])
                if open_trade else None
            ),
        }

    @app.post("/api/arm")
    def arm(_user: str = Depends(_authorize)):
        conn = _connect()
        try:
            command_id = logstore.enqueue_command(conn, "ARM")
        finally:
            conn.close()
        return {"queued": True, "command_id": command_id}

    @app.post("/api/kill")
    def kill(_user: str = Depends(_authorize)):
        conn = _connect()
        try:
            command_id = logstore.enqueue_command(conn, "KILL")
        finally:
            conn.close()
        return {"queued": True, "command_id": command_id}

    @app.post("/api/mode")
    def mode_update(body: ModeUpdate, _user: str = Depends(_authorize)):
        if body.mode not in {"paper", "live"}:
            raise HTTPException(status_code=400, detail="invalid_mode")
        if body.confirm is not True:
            raise HTTPException(status_code=400, detail="confirmation_required")
        if body.mode == "live":
            raise HTTPException(status_code=403, detail="live_execution_not_implemented")
        conn = _connect()
        try:
            document = _scheduled_doc(conn)
            document["risk"]["trading_mode"] = body.mode
            try:
                validate(document)
            except RulesValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            version = save_new_version(conn, document, "Mode scheduled via UI")
        finally:
            conn.close()
        return {"scheduled_mode": body.mode, "rules_version": version}

    @app.get("/api/settings")
    def get_settings(_user: str = Depends(_authorize)):
        data_dir = _data_dir()
        conn = _connect()
        try:
            app_settings = {**DEFAULT_SETTINGS, **logstore.get_app_settings(conn)}
            health = _system_health(conn, data_dir)
            active = _scheduled_doc(conn)
            scheduled = _scheduled_doc(conn)
            session = logstore.get_session(conn, now_ist().date().isoformat())
            if session:
                active_row = logstore.get_rule_version(conn, session["rules_version"])
                if active_row:
                    active = yaml.safe_load(active_row["yaml"])
        finally:
            conn.close()
        return {
            "token_status": token_status(data_dir),
            "health": health,
            "credentials": _credential_summary(data_dir),
            "app_settings": app_settings,
            "data_files": _datafile_state(data_dir),
            "active": active,
            "scheduled": scheduled,
        }

    @app.post("/api/settings")
    def update_settings(body: SettingsUpdate, _user: str = Depends(_authorize)):
        updates = {
            key: str(value)
            for key, value in body.model_dump(exclude_none=True).items()
        }
        conn = _connect()
        try:
            logstore.set_app_settings(conn, updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            conn.close()
        return {"saved": True}

    @app.post("/api/config/credentials")
    async def update_credentials(request: Request, _user: str = Depends(_authorize)):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid_json") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="credentials_must_be_object")
        unknown = set(body) - logstore.ALLOWED_CREDENTIAL_KEYS
        if unknown:
            if unknown & LOCKED_KEYS:
                raise HTTPException(status_code=400, detail="locked_credential_key")
            raise HTTPException(status_code=400, detail="unknown_credential_key")
        if any(not isinstance(value, str) for value in body.values()):
            raise HTTPException(status_code=400, detail="credential_values_must_be_strings")
        data_dir = _data_dir()
        try:
            key_changed = logstore.save_credentials(data_dir, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn = _connect()
        try:
            if key_changed:
                _token_file(data_dir).unlink(missing_ok=True)
            logstore.enqueue_command(conn, "RELOAD_CONFIG")
        finally:
            conn.close()
        return {
            "saved": True,
            "credentials": _credential_summary(data_dir),
            "token_status": token_status(data_dir),
        }

    @app.post("/api/config/datafile")
    def update_datafile(body: DataFileUpdate, _user: str = Depends(_authorize)):
        try:
            logstore.validate_and_save_data_file(_data_dir(), body.name, body.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"saved": True, "name": body.name, "applies": "next_session"}

    @app.post("/api/token")
    async def token_update(body: TokenUpdate, _user: str = Depends(_authorize)):
        data_dir = _data_dir()
        if body.mode == "access_token":
            _write_token(data_dir, body.token or "", body.user_id)
        elif body.mode == "clear":
            _token_file(data_dir).unlink(missing_ok=True)
        elif body.mode == "request_token":
            if not body.token:
                raise HTTPException(status_code=400, detail="request_token_required")
            data = await _exchange_request_token(body.token, _effective_credentials(data_dir))
            _write_token(data_dir, data["access_token"], data.get("user_id"))
        else:
            raise HTTPException(status_code=400, detail="invalid_token_mode")
        conn = _connect()
        try:
            logstore.enqueue_command(conn, "RELOAD_TOKEN")
        finally:
            conn.close()
        return {"token_status": token_status(data_dir)}

    @app.post("/api/token/verify")
    def verify_token(_user: str = Depends(_authorize)):
        if token_status(_data_dir())["status"] != "PRESENT":
            raise HTTPException(status_code=400, detail="valid_local_token_required")
        conn = _connect()
        try:
            command_id = logstore.enqueue_command(conn, "VERIFY_TOKEN")
        finally:
            conn.close()
        return {"queued": True, "command_id": command_id}

    @app.get("/api/events")
    def events(
        day: str | None = None,
        reason: str | None = None,
        _user: str = Depends(_authorize),
    ):
        conn = _connect()
        try:
            rows = logstore.list_events(conn, day=day, reason=reason)
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @app.get("/api/trade")
    def trade(day: str | None = Query(default=None), _user: str = Depends(_authorize)):
        day = day or now_ist().date().isoformat()
        conn = _connect()
        try:
            row = logstore.get_trade(conn, day)
            return dict(row) if row else None
        finally:
            conn.close()

    @app.get("/api/trades")
    def trades(
        mode: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        _user: str = Depends(_authorize),
    ):
        conn = _connect()
        try:
            return logstore.trades_summary(conn, mode, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            conn.close()

    @app.get("/api/rules")
    def rules(_user: str = Depends(_authorize)):
        conn = _connect()
        try:
            scheduled = _scheduled_doc(conn)
            row = logstore.get_session(conn, now_ist().date().isoformat())
            locked = None
            if row:
                version = logstore.get_rule_version(conn, row["rules_version"])
                locked = yaml.safe_load(version["yaml"]) if version else None
            return {"scheduled": yaml.safe_dump(scheduled, sort_keys=False), "locked": locked}
        finally:
            conn.close()

    @app.post("/api/rules")
    def save_rules(body: RulesUpdate, _user: str = Depends(_authorize)):
        try:
            document = yaml.safe_load(body.yaml)
            parsed = validate(document)
        except (yaml.YAMLError, RulesValidationError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if parsed.risk.trading_mode == "live":
            raise HTTPException(status_code=403, detail="live_execution_not_implemented")
        conn = _connect()
        try:
            version = save_new_version(conn, document, body.note)
        finally:
            conn.close()
        return {"rules_version": version, "applies": "next_session"}

    @app.post("/api/review")
    def save_review(body: ReviewUpdate, _user: str = Depends(_authorize)):
        from nsebot.review import REVIEW_TAGS

        invalid_tags = set(body.tags) - REVIEW_TAGS
        if invalid_tags:
            raise HTTPException(status_code=400, detail="invalid_review_tag")
        conn = _connect()
        try:
            logstore.save_review(conn, {
                "day": body.day,
                "tags": json.dumps(body.tags),
                "note": body.note,
                "source": body.source,
                "created_at": now_ist().isoformat(),
            })
        finally:
            conn.close()
        return {"saved": True}

    @app.post("/api/review/draft")
    async def draft_review(request: Request, _user: str = Depends(_authorize)):
        from nsebot.review import ReviewError, draft_review as create_draft

        try:
            body = await request.json()
            day = body["day"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="day_required") from exc
        try:
            return await create_draft(_data_dir(), day)
        except ReviewError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="review_provider_failed") from exc

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("nsebot.api.app:app", host="127.0.0.1", port=8000, workers=1)
