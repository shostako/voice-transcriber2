"""Supabase-backed usage metadata for transcription requests.

Audio and transcription text are deliberately never persisted here.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

TRANSCRIPTION_PRICE_PER_MINUTE = {
    "gpt-transcribe": Decimal("0.0045"),
    "gpt-4o-transcribe": Decimal("0.006"),
    "gpt-4o-mini-transcribe": Decimal("0.003"),
    "whisper-1": Decimal("0.006"),
}

POLISH_PRICE_PER_MILLION_TOKENS = {
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
    "gpt-5.4-nano": (Decimal("0.20"), Decimal("1.25")),
}

PUBLIC_COLUMNS = (
    "id,created_at,status,original_filename,audio_duration_seconds,"
    "raw_characters,output_characters,transcription_model,"
    "transcription_segments,transcription_cost_usd,polish_requested,"
    "polish_succeeded,polish_partial,polish_model,polish_input_tokens,"
    "polish_output_tokens,polish_cost_usd,total_cost_usd,"
    "processing_seconds,error_type,error_message"
)


def transcription_cost(model: str, duration_seconds: Optional[float]) -> Decimal:
    if duration_seconds is None:
        return Decimal("0")
    override = os.getenv("TRANSCRIPTION_PRICE_PER_MINUTE")
    price = Decimal(override) if override else TRANSCRIPTION_PRICE_PER_MINUTE.get(model)
    if price is None:
        print(f"[history] {model} の文字起こし単価が未登録 → $0 として記録", flush=True)
        return Decimal("0")
    return (Decimal(str(duration_seconds)) / Decimal("60") * price).quantize(Decimal("0.00000001"))


def polish_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    prices = POLISH_PRICE_PER_MILLION_TOKENS.get(model)
    if prices is None:
        print(f"[history] {model} の整形単価が未登録 → $0 として記録", flush=True)
        return Decimal("0")
    input_price, output_price = prices
    cost = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal("1000000")
    return cost.quantize(Decimal("0.00000001"))


def safe_error_message(error: BaseException, limit: int = 300) -> str:
    text = str(error)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"\bsb_(?:secret|publishable)_[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[-limit:]


def configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SECRET_KEY"))


def _headers() -> Dict[str, str]:
    key = os.getenv("SUPABASE_SECRET_KEY") or ""
    headers = {"apikey": key, "Content-Type": "application/json"}
    # New sb_secret keys are sent only as apikey. Legacy service_role keys are JWTs.
    if key and not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    if not base or not os.getenv("SUPABASE_SECRET_KEY"):
        raise RuntimeError("Supabase の環境変数が未設定です")
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"Supabase HTTP {exc.code}: {detail}") from exc


def insert_log(payload: Dict[str, Any]) -> bool:
    """Best-effort insert. Logging must never break transcription."""
    if not configured():
        print("[history] Supabase 未設定のため利用履歴を保存しません", flush=True)
        return False
    try:
        _request("POST", "/rest/v1/transcription_logs", payload)
        return True
    except Exception as exc:
        print(f"[history] 保存失敗: {safe_error_message(exc)}", flush=True)
        return False


def fetch_logs(limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(limit, 500))
    query = urllib.parse.urlencode({"select": PUBLIC_COLUMNS, "order": "created_at.desc", "limit": limit})
    return _request("GET", f"/rest/v1/transcription_logs?{query}") or []


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    return {
        "requests": len(rows),
        "successes": sum(1 for row in rows if row["status"] == "success"),
        "errors": sum(1 for row in rows if row["status"] == "error"),
        "audio_duration_seconds": round(sum(float(row.get("audio_duration_seconds") or 0) for row in rows), 1),
        "raw_characters": sum(int(row.get("raw_characters") or 0) for row in rows),
        "output_characters": sum(int(row.get("output_characters") or 0) for row in rows),
        "total_cost_usd": round(sum(float(row.get("total_cost_usd") or 0) for row in rows), 8),
    }


def logs_csv(rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows)
    output = io.StringIO()
    fields = PUBLIC_COLUMNS.split(",")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return "\ufeff" + output.getvalue()
