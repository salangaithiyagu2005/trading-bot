from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx

from nsebot import logstore

REVIEW_TAGS = {
    "bias_wrong", "late_confirm", "chop_after_entry", "spread_too_wide",
    "sl_too_tight", "target_too_far", "structure_exit_saved_me",
    "structure_exit_too_early", "expiry_noise", "news_spike",
    "no_signal_correct_skip", "would_have_worked_if_chased",
}


class ReviewError(ValueError):
    pass


REVIEW_PROMPT = (
    "You are writing a journal note for one NSE intraday session. Use only the JSON. "
    "Do not invent prices. Choose 1 to 3 tags from this list only: bias_wrong, "
    "late_confirm, chop_after_entry, spread_too_wide, sl_too_tight, target_too_far, "
    "structure_exit_saved_me, structure_exit_too_early, expiry_noise, news_spike, "
    "no_signal_correct_skip, would_have_worked_if_chased. "
    "would_have_worked_if_chased is a warning that chasing is not allowed. "
    "Do not propose a new indicator. Reply with JSON "
    '{"tags":[...],"note":"..."} and nothing else.'
)


def _parse_draft(day: str, text: str) -> dict[str, str | list[str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewError("review_provider_invalid_response") from exc
    if not isinstance(payload, dict) or set(payload) != {"tags", "note"}:
        raise ReviewError("review_provider_invalid_response")
    tags = payload["tags"]
    note = payload["note"]
    if (
        not isinstance(tags, list)
        or not 1 <= len(tags) <= 3
        or any(not isinstance(tag, str) or tag not in REVIEW_TAGS for tag in tags)
        or len(set(tags)) != len(tags)
        or not isinstance(note, str)
        or not note.strip()
    ):
        raise ReviewError("review_provider_invalid_response")
    return {"day": day, "draft": note.strip(), "tags": tags}


async def draft_review(data_dir: Path, day: str) -> dict[str, str | list[str]]:
    if not isinstance(day, str):
        raise ReviewError("day must be YYYY-MM-DD")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError as exc:
        raise ReviewError("day must be YYYY-MM-DD") from exc
    credentials = logstore.load_credentials(data_dir)
    api_key = credentials.get("XAI_API_KEY")
    if not api_key:
        raise ReviewError("model_not_configured")
    conn = logstore.connect(data_dir)
    try:
        events = [
            dict(row) for row in logstore.list_events(
                conn,
                day=day,
                fields=("ts", "state_from", "state_to", "reason", "reason_text", "snapshot_json"),
            )
        ]
        trade_row = logstore.get_trade(conn, day)
        trade = dict(trade_row) if trade_row else None
        session = logstore.get_session(conn, day)
        rules = None
        if session:
            version = logstore.get_rule_version(conn, session["rules_version"])
            rules = version["yaml"] if version else None
    finally:
        conn.close()
    request_payload = {
        "model": "grok-3-mini",
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": REVIEW_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"day": day, "events": events, "trade": trade, "rules_yaml": rules},
                    separators=(",", ":"),
                ),
            },
        ],
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_payload,
        )
    response.raise_for_status()
    payload = response.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewError("review_provider_invalid_response") from exc
    if not isinstance(text, str) or not text.strip():
        raise ReviewError("review_provider_invalid_response")
    return _parse_draft(day, text)
