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
MODEL = os.getenv("TRANSCRIBE_MODEL") or "gpt-transcribe"   # 空文字も既定に倒す
LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE") or "ja"        # 同上。言語指定なしにしたければ "auto"
# prompt の効き方はモデルで違う:
# - whisper-1: 指示文は効かず「見本文」で文体・句読点が揃う
# - gpt-transcribe / gpt-4o-*: 文脈説明＋指示文が効く（フィラー除去など）
DEFAULT_PROMPTS = {
    "whisper-1": "こんにちは。今日は、会議の内容を記録します。よろしくお願いします。",
}
GPT_DEFAULT_PROMPT = "日本語の会話の録音。「あの」「えっと」などのフィラーは省き、句読点を付けて書き起こす。"
PROMPT_OVERRIDE = os.getenv("TRANSCRIBE_PROMPT") or None


def base_prompt(model: Optional[str] = None) -> str:
    """環境変数が最優先、無ければモデル別の既定。model 未指定なら現在の MODEL。"""
    return PROMPT_OVERRIDE or DEFAULT_PROMPTS.get(model or MODEL, GPT_DEFAULT_PROMPT)
# whisper-1 の prompt 上限は 224 トークン（日本語で 100〜150 文字程度）。
# 前チャンク末尾は継続用なので短く留め、用語ヒントを押し出さないようにする
PREV_TAIL_CHARS = int(os.getenv("TRANSCRIBE_PREV_TAIL_CHARS") or "60")

# LLM 後処理（同音異義の誤変換修正・句読点・段落分け）。POLISH_MODEL= で無効化はしない、UI 側で選ぶ
POLISH_MODEL = os.getenv("POLISH_MODEL") or "gpt-5.4-mini"
POLISH_CHUNK_CHARS = 3000        # 1回の校正に渡す本文の目安（文末で切る）
POLISH_RATIO_MIN, POLISH_RATIO_MAX = 0.7, 1.5   # この範囲を外れたら要約/水増しとみなし原文に戻す
POLISH_SYSTEM = """あなたは日本語の会議録音の書き起こしを校正する編集者です。以下を厳守してください。
- 内容の要約・省略・言い換えはしない。発言の順序と情報量を保つ
- 直すのは次だけ: 同音異義語の誤変換（文脈と用語集で判断）、句読点、明らかな聞き間違い
- 用語集にある語（人名・製品名・専門用語）と音が近い語は、用語集の表記に置き換える
- 話題の切れ目で空行を入れて段落に分ける
- 「あの」「えっと」「まあ」等のフィラーと、同じ語の言い直しは取り除いてよい
- 話者名や敬称は追加しない
- 出力は校正後の本文のみ。前置きや説明は書かない"""

API_MAX_BYTES = 25 * 1024 * 1024          # OpenAI 側の上限
TARGET_CHUNK_BYTES = 24 * 1024 * 1024     # 余裕をみた分割目標
AUDIO_BITRATE = "64k"                     # 16kHz mono mp3。25MB で約 54 分入る
SILENCE_NOISE_DB = "-35dB"
SILENCE_MIN_SEC = "0.5"
FFMPEG_TIMEOUT_SEC = int(os.getenv("FFMPEG_TIMEOUT_SEC") or "600")
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
    parts = [base_prompt()]
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
        lang = None if LANGUAGE == "auto" else LANGUAGE
        if MODEL == "gpt-transcribe":
            extra = {}
            if lang:
                extra["languages"] = [lang]
            keywords = split_keywords(user_hint)
            if keywords:
                extra["keywords"] = keywords
            if extra:
                kwargs["extra_body"] = extra
        elif lang:
            kwargs["language"] = lang
        transcript = client.audio.transcriptions.create(**kwargs)
    return transcript.text.strip()


# --- LLM 後処理 ---------------------------------------------------------------
def split_for_polish(text: str, limit: int = POLISH_CHUNK_CHARS) -> List[str]:
    """文末（。！？/改行）で切りながら limit 文字前後の塊にする。"""
    pieces = re.split(r"(?<=[。！？\n])", text)
    chunks, buf = [], ""
    for piece in pieces:
        # 句読点の無い巨大な塊は文字数で機械的に割る
        while len(piece) > limit:
            if buf.strip():
                chunks.append(buf)
                buf = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if buf and len(buf) + len(piece) > limit:
            chunks.append(buf)
            buf = ""
        buf += piece
    if buf.strip():
        chunks.append(buf)
    return chunks


def polish_chunk(client: openai.OpenAI, text: str, user_hint: Optional[str], idx: int) -> Tuple[str, bool]:
    """1塊を校正して (本文, 整形できたか) を返す。API エラーも長さ比逸脱も原文に落とす。"""
    user = (f"用語集: {user_hint.strip()}\n\n" if user_hint else "") + f"--- 書き起こし ---\n{text}"
    try:
        r = client.chat.completions.create(
            model=POLISH_MODEL,
            messages=[{"role": "system", "content": POLISH_SYSTEM}, {"role": "user", "content": user}],
        )
    except Exception as e:
        print(f"[polish] chunk {idx}: {type(e).__name__}: {e} → 原文を採用", flush=True)
        return text, False
    out = (r.choices[0].message.content or "").strip()
    ratio = len(out) / max(len(text), 1)
    if not out or not (POLISH_RATIO_MIN <= ratio <= POLISH_RATIO_MAX):
        print(f"[polish] chunk {idx}: 長さ比 {ratio:.2f} が範囲外 → 原文を採用", flush=True)
        return text, False
    return out, True


def polish_text(client: openai.OpenAI, text: str, user_hint: Optional[str]) -> Tuple[str, int, int]:
    """(本文, 整形できた塊数, 全塊数)。塊の連結は単一改行にして、機械的な境界を段落に見せない。"""
    results = [polish_chunk(client, c, user_hint, i) for i, c in enumerate(split_for_polish(text))]
    ok = sum(1 for _, done in results if done)
    return "\n".join(t for t, _ in results), ok, len(results)


@app.post("/transcribe")
def transcribe_audio(
    file: UploadFile = File(...),
    prompt: Optional[str] = Form(None),
    polish: bool = Form(True),
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

        raw = "\n".join(t for t in texts if t)
        result = {
            "text": raw,
            "raw": raw,
            "polished": False,
            "model": MODEL,
            "duration": round(duration, 1),
            "segments": len(chunks),
        }
        if polish and raw.strip():
            text, ok, total = polish_text(client, raw, prompt)
            if ok > 0:
                result["text"] = text
                result["polished"] = True
                result["polish_partial"] = ok < total
                result["polish_model"] = POLISH_MODEL
            else:
                print(f"[polish] 全 {total} 塊が原文に戻った", flush=True)
        return result

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
