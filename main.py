import os
import re
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import openai

load_dotenv()

app = FastAPI(title="音声文字起こし")

# --- 設定（環境変数で上書き可） ---------------------------------------------
# 2026-08-26 の A/B（実録音4分）で gpt-transcribe が唯一欠落なし・用語正解率最良だったので既定に
MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-transcribe")
LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "ja")
# prompt の効き方はモデルで違う:
# - whisper-1: 指示文は効かず「見本文」で文体・句読点が揃う
# - gpt-transcribe / gpt-4o-*: 文脈説明＋指示文が効く（フィラー除去など）
DEFAULT_PROMPTS = {
    "whisper-1": "こんにちは。今日は、会議の内容を記録します。よろしくお願いします。",
}
GPT_DEFAULT_PROMPT = "日本語の会話の録音。「あの」「えっと」などのフィラーは省き、句読点を付けて書き起こす。"
BASE_PROMPT = os.getenv("TRANSCRIBE_PROMPT") or DEFAULT_PROMPTS.get(MODEL, GPT_DEFAULT_PROMPT)
# whisper-1 の prompt 上限は 224 トークン（日本語で 100〜150 文字程度）。
# 前チャンク末尾は継続用なので短く留め、用語ヒントを押し出さないようにする
PREV_TAIL_CHARS = int(os.getenv("TRANSCRIBE_PREV_TAIL_CHARS", "60"))

API_MAX_BYTES = 25 * 1024 * 1024          # OpenAI 側の上限
TARGET_CHUNK_BYTES = 24 * 1024 * 1024     # 余裕をみた分割目標
AUDIO_BITRATE = "64k"                     # 16kHz mono mp3。25MB で約 54 分入る
SILENCE_NOISE_DB = "-35dB"
SILENCE_MIN_SEC = "0.5"
FFMPEG_TIMEOUT_SEC = int(os.getenv("FFMPEG_TIMEOUT_SEC", "600"))
USER_HINT_MAX_CHARS = 200
MAX_RESPLIT_DEPTH = 4

for _tool in ("ffmpeg", "ffprobe"):
    if shutil.which(_tool) is None:
        print(f"[warn] {_tool} が見つかりません。文字起こしは失敗します", flush=True)


# --- ffmpeg ラッパ ------------------------------------------------------------
def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SEC)
    except FileNotFoundError:
        raise RuntimeError(f"{cmd[0]} が見つかりません。サーバ環境に ffmpeg を入れてください")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]} が {FFMPEG_TIMEOUT_SEC} 秒以内に終わりませんでした")


def _check(r: subprocess.CompletedProcess, what: str) -> None:
    if r.returncode != 0:
        raise RuntimeError(f"{what}に失敗しました: {r.stderr.strip()[-300:]}")


def normalize_audio(src: str, dst: str) -> None:
    """入力を 16kHz mono mp3 に落とす。Whisper は内部で 16kHz mono に潰すので情報損失はない。
    動画コンテナからは音声トラックだけ抜く (-vn)。"""
    r = _run([
        "ffmpeg", "-y", "-v", "error",
        "-i", src,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", AUDIO_BITRATE,
        dst,
    ])
    if r.returncode != 0 or not os.path.exists(dst):
        raise ValueError(f"音声として読み込めませんでした: {r.stderr.strip()[-300:]}")


def probe_duration(path: str) -> float:
    r = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ])
    _check(r, "長さの取得")
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        raise RuntimeError(f"長さを読めませんでした: {r.stdout!r}")
    if duration <= 0:
        raise ValueError("音声の長さが 0 です。ファイルが壊れているか無音です")
    return duration


def detect_silence_midpoints(path: str) -> List[float]:
    """silencedetect で無音区間を拾い、その中点（秒）を返す。"""
    r = _run([
        "ffmpeg", "-hide_banner", "-v", "info", "-nostats",
        "-i", path,
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_SEC}",
        "-f", "null", "-",
    ])
    _check(r, "無音検出")
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", r.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def plan_segments(path: str, duration: float) -> Tuple[List[Tuple[float, float]], List[float]]:
    """25MB 超なら無音位置で切る区間を貪欲に決める。
    区間の秒数は平均ビットレートから見積もった上限以下になるが、バイト数の最終確認は
    切り出し後に ensure_fits で行う。無音中点のリストも返す（再分割用）。"""
    size = os.path.getsize(path)
    if size <= TARGET_CHUNK_BYTES:
        return [(0.0, duration)], []

    bytes_per_sec = size / duration
    max_seg = TARGET_CHUNK_BYTES / bytes_per_sec
    silences = detect_silence_midpoints(path)

    segments: List[Tuple[float, float]] = []
    cursor = 0.0
    while duration - cursor > max_seg:
        lo = cursor + max_seg * 0.5
        hi = cursor + max_seg * 0.98
        ideal = cursor + max_seg * 0.9
        candidates = [t for t in silences if lo <= t <= hi]
        cut = min(candidates, key=lambda t: abs(t - ideal)) if candidates else cursor + max_seg * 0.95
        segments.append((cursor, cut))
        cursor = cut
    segments.append((cursor, duration))
    return segments, silences


def split_near_middle(start: float, end: float, silences: List[float]) -> float:
    """区間の中央付近（30〜70%）で最も中央に近い無音、無ければ中央。"""
    mid = (start + end) / 2
    span = end - start
    cands = [t for t in silences if start + span * 0.3 <= t <= start + span * 0.7]
    return min(cands, key=lambda t: abs(t - mid)) if cands else mid


def ensure_fits(src: str, workdir: str, tag: str, start: float, end: float,
                silences: List[float], depth: int = 0) -> List[str]:
    """区間を切り出し、実サイズが API 上限を超えていれば無音位置で半分に割って再帰。
    見積もり（平均ビットレート）が外れても送信前に必ず上限以下に収める。"""
    dst = os.path.join(workdir, f"chunk_{tag}.mp3")
    cut_segment(src, dst, start, end)
    if os.path.getsize(dst) <= API_MAX_BYTES:
        return [dst]
    os.remove(dst)
    if depth >= MAX_RESPLIT_DEPTH:
        raise RuntimeError(f"区間 {start:.0f}-{end:.0f}s を上限以下に分割できませんでした")
    cut = split_near_middle(start, end, silences)
    return (ensure_fits(src, workdir, f"{tag}a", start, cut, silences, depth + 1)
            + ensure_fits(src, workdir, f"{tag}b", cut, end, silences, depth + 1))


def cut_segment(src: str, dst: str, start: float, end: float) -> None:
    r = _run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-i", src,
        "-t", f"{end - start:.3f}",
        "-c", "copy",
        dst,
    ])
    if r.returncode != 0:
        raise RuntimeError(f"分割に失敗しました: {r.stderr.strip()[-300:]}")


# --- 文字起こし ---------------------------------------------------------------
def build_prompt(user_hint: Optional[str], prev_tail: str) -> str:
    """Whisper は prompt が長いと先頭から捨てるので、重要なものほど後ろに置く。"""
    parts = [BASE_PROMPT]
    if user_hint:
        parts.append(user_hint.strip()[:USER_HINT_MAX_CHARS])
    if prev_tail:
        parts.append(prev_tail)
    return " ".join(p for p in parts if p)


def split_keywords(user_hint: Optional[str]) -> List[str]:
    """用語ヒントを読点・カンマ・空白で割って keywords リストにする。"""
    if not user_hint:
        return []
    return [k for k in re.split(r"[、,，\s]+", user_hint.strip()) if k][:50]


def transcribe_file(client: openai.OpenAI, path: str, prompt: str,
                    user_hint: Optional[str] = None) -> str:
    """モデル差分はここに閉じ込める。
    - whisper-1 / gpt-4o-*: language(単数) + prompt
    - gpt-transcribe: languages(複数) + keywords + prompt。language 単数は非対応"""
    with open(path, "rb") as audio_file:
        kwargs = dict(model=MODEL, file=audio_file, prompt=prompt)
        if MODEL == "gpt-transcribe":
            extra = {}
            if LANGUAGE:
                extra["languages"] = [LANGUAGE]
            keywords = split_keywords(user_hint)
            if keywords:
                extra["keywords"] = keywords
            if extra:
                kwargs["extra_body"] = extra
        elif LANGUAGE:
            kwargs["language"] = LANGUAGE
        transcript = client.audio.transcriptions.create(**kwargs)
    return transcript.text.strip()


@app.post("/transcribe")
def transcribe_audio(
    file: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
):
    """音声/動画ファイルを文字起こしする。prompt は用語ヒント（任意）。
    ffmpeg と OpenAI 呼び出しはブロッキングなので同期関数にしてスレッドプールに逃がす。"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return JSONResponse(
            status_code=500,
            content={"error": "OpenAI APIキーが設定されていません。.envファイルを確認してください。"},
        )

    workdir = tempfile.mkdtemp(prefix="transcribe_")
    try:
        ext = os.path.splitext(file.filename or "")[1][:8]
        src = os.path.join(workdir, f"input{ext}")
        with open(src, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        audio = os.path.join(workdir, "audio.mp3")
        normalize_audio(src, audio)
        duration = probe_duration(audio)
        segments, silences = plan_segments(audio, duration)

        if len(segments) == 1:
            chunks = [audio]
        else:
            chunks = []
            for i, (start, end) in enumerate(segments):
                chunks.extend(ensure_fits(audio, workdir, str(i), start, end, silences))

        client = openai.OpenAI(api_key=api_key)
        texts: List[str] = []
        prev_tail = ""
        for target in chunks:
            text = transcribe_file(client, target, build_prompt(prompt, prev_tail), prompt)
            texts.append(text)
            prev_tail = text[-PREV_TAIL_CHARS:] if PREV_TAIL_CHARS > 0 else ""

        return {
            "text": "\n".join(t for t in texts if t),
            "model": MODEL,
            "duration": round(duration, 1),
            "segments": len(chunks),
        }

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# 静的ファイル配信（APIルートの後に配置）
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
