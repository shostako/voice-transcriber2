import os
import math
import shutil
import subprocess
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import openai

load_dotenv()

app = FastAPI(title="音声文字起こし")


@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """音声ファイルを文字起こしする"""
    if not file:
        raise HTTPException(status_code=400, detail="ファイルがアップロードされていません")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return JSONResponse(
            status_code=500,
            content={"error": "OpenAI APIキーが設定されていません。.envファイルを確認してください。"}
        )

    temp_filename = f"temp_{file.filename}"
    try:
        # 一時ファイルに保存
        with open(temp_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        client = openai.OpenAI(api_key=api_key)
        file_size = os.path.getsize(temp_filename)
        MAX_SIZE = 25 * 1024 * 1024  # 25MB

        if file_size <= MAX_SIZE:
            # 通常処理
            with open(temp_filename, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            final_text = transcript.text
        else:
            # 大容量ファイルの分割処理
            final_text = await _process_large_file(temp_filename, client)

        return {"text": final_text}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)


async def _process_large_file(temp_filename: str, client: openai.OpenAI) -> str:
    """25MB超のファイルをffmpegで分割して処理"""
    # 音声の長さを取得
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        temp_filename
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    duration = float(result.stdout.strip())

    file_ext = os.path.splitext(temp_filename)[1]
    chunk_length = 10 * 60  # 10分
    num_chunks = math.ceil(duration / chunk_length)

    full_transcript = []

    for i in range(num_chunks):
        chunk_filename = f"temp_chunk_{i}{file_ext}"
        start_time = i * chunk_length

        # ffmpegで分割
        split_cmd = [
            "ffmpeg",
            "-y",
            "-i", temp_filename,
            "-ss", str(start_time),
            "-t", str(chunk_length),
            "-acodec", "copy",
            chunk_filename
        ]
        subprocess.run(split_cmd, check=True, capture_output=True)

        try:
            with open(chunk_filename, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            full_transcript.append(transcript.text)
        finally:
            if os.path.exists(chunk_filename):
                os.remove(chunk_filename)

    return " ".join(full_transcript)


# 静的ファイル配信（APIルートの後に配置）
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
