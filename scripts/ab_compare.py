#!/usr/bin/env python3
"""同じ音声を複数モデルで文字起こしして並べる A/B スクリプト。

使い方:
  python3 scripts/ab_compare.py 音声.m4a [--models whisper-1,gpt-4o-transcribe,gpt-transcribe]
                                          [--hint "射出成形、金型、粘度"] [--out ab_out]

main.py の normalize_audio / transcribe_file をそのまま使うので、本番と同じ前処理・
プロンプト構成で比較できる。25MB 超は対象外（抜粋を渡せ）。結果は
<out>/<音声名>/<model>.txt と compare.md に書く。判定は目視前提。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402
import openai  # noqa: E402

DEFAULT_MODELS = "whisper-1,gpt-4o-transcribe,gpt-transcribe"


def run(src: str, models, hint, out_dir: str) -> None:
    name = os.path.splitext(os.path.basename(src))[0]
    dest = os.path.join(out_dir, name)
    os.makedirs(dest, exist_ok=True)

    audio = os.path.join(dest, "audio.mp3")
    main.normalize_audio(src, audio)
    duration = main.probe_duration(audio)
    size = os.path.getsize(audio)
    if size > main.API_MAX_BYTES:
        sys.exit(f"{size/1e6:.1f}MB は上限超。抜粋を渡せ")
    print(f"{name}: {duration:.0f}s {size/1e6:.1f}MB")

    client = openai.OpenAI()
    prompt = main.build_prompt(hint, "")
    results = {}
    for model in models:
        main.MODEL = model
        t = time.time()
        try:
            text = main.transcribe_file(client, audio, prompt, hint)
        except Exception as e:  # モデル非対応なども結果として残す
            text = f"[ERROR] {e}"
        elapsed = time.time() - t
        results[model] = (text, elapsed)
        with open(os.path.join(dest, f"{model}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  {model:22s} {elapsed:5.1f}s {len(text):5d}文字")

    with open(os.path.join(dest, "compare.md"), "w", encoding="utf-8") as f:
        f.write(f"# {name}\n\n{duration:.0f}s / hint: {hint or '(なし)'}\n\n")
        f.write("| model | 秒 | 文字数 |\n|---|---|---|\n")
        for m, (text, el) in results.items():
            f.write(f"| {m} | {el:.1f} | {len(text)} |\n")
        for m, (text, _) in results.items():
            f.write(f"\n## {m}\n\n{text}\n")
    print(f"  → {os.path.join(dest, 'compare.md')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--hint", default=None)
    ap.add_argument("--out", default="ab_out")
    a = ap.parse_args()
    for src in a.files:
        run(src, a.models.split(","), a.hint, a.out)
