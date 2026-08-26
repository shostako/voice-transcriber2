# Changelog

## 2026-08-26

精度改善パス。すべて PR 経由で本番反映済み。

- 言語固定（`ja`）、16kHz mono mp3 再エンコード、無音位置での分割、前チャンク末尾のプロンプト継続（#1）
  - レビュー対応: 分割後サイズ検証と再分割、ffmpeg の returncode/timeout チェック、エンドポイントの同期化、ヒント長上限
- モデル A/B（`scripts/ab_compare.py`）の結果で既定を `whisper-1` → `gpt-transcribe` に切替。用語ヒントを `keywords` で渡す（#2）
- LLM 後処理（`gpt-5.4-mini`）: 誤変換修正・句読点・段落分け。UI に用語ヒント欄と校正 ON/OFF、Raw/Polished 切替を追加（#3）
  - 3000 文字で文末分割、要約防止の長さ比ガード、チャンク単位の失敗は原文フォールバック（`polish_partial`）
- Render の GitHub App 連携が死んでいたため、GitHub Actions から Deploy Hook を叩く経路を追加（#4, #5）

## 2025-12-02

初版。FastAPI + OpenAI `whisper-1`、25MB 超の自動分割、PWA、Docker、Render デプロイ。Claude Code Action（`@claude` レビュー）を追加。
