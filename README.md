# voice-transcriber2

会議録音を日本語で文字起こしする単機能 Web アプリ。OpenAI の音声モデルで書き起こし、LLM で誤変換・句読点・段落を整えて返す。

本番: <https://voice-transcriber2.onrender.com>（Render free / Docker）

## 何をするか

1. 音声/動画を選択し、用語ヒントとAI整形の設定を確認して「文字起こし開始」を押す（選択しただけでは送信しない）
2. アップロードされた音声/動画を ffmpeg で 16kHz mono mp3 (64kbps) に再エンコード（25MB ≒ 54 分）
3. 上限を超える長さなら `silencedetect` で無音位置を探し、そこで分割（切り出し後のサイズも検証し、超えていれば再分割）
4. チャンクごとに `gpt-transcribe`（既定）で書き起こし。直前チャンクの末尾を prompt に継ぎ足して文脈を繋ぐ
5. 用語ヒント（UI のテキスト欄、200 文字まで）を `keywords` として渡し、専門用語・人名の誤変換を抑える
6. `gpt-5.4-mini` で校正（要約禁止、長さ比 0.7〜1.5 を外れたら原文に戻す）。UI のチェックで無効化可、Raw/Polished は結果画面で切替
7. Supabase に利用日時・音声時間・文字数・token 数・推定 API 料金を記録。音声と文字起こし本文は保存しない

フロントは静的 PWA（`static/`）。UI を変えたら `static/sw.js` の `CACHE_NAME` を上げないと旧キャッシュが残る。

## ローカル起動

```bash
cp .env.example .env   # OPENAI_API_KEY を入れる
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

ffmpeg / ffprobe が PATH に必要（無ければ起動時に警告が出る）。Docker なら `docker compose up --build`。

## 環境変数

| 変数 | 既定 | 意味 |
|---|---|---|
| `OPENAI_API_KEY` | (必須) | OpenAI API キー |
| `TRANSCRIBE_MODEL` | `gpt-transcribe` | `whisper-1` / `gpt-4o-transcribe` も可 |
| `TRANSCRIBE_LANGUAGE` | `ja` | `auto` で言語指定なし |
| `TRANSCRIBE_PROMPT` | モデル別既定文 | 書き起こしプロンプトの上書き |
| `TRANSCRIBE_PREV_TAIL_CHARS` | `60` | 前チャンク末尾を継ぎ足す文字数 |
| `FFMPEG_TIMEOUT_SEC` | `600` | ffmpeg 1 回あたりのタイムアウト |
| `POLISH_MODEL` | `gpt-5.4-mini` | 校正に使う chat モデル |
| `SUPABASE_URL` | なし | 利用履歴を保存する Supabase Project URL |
| `SUPABASE_SECRET_KEY` | なし | サーバ専用の Supabase Secret key |
| `HISTORY_PASSWORD` | なし | `/history` と履歴 API の Basic 認証パスワード |
| `LOG_INCLUDE_FILENAME` | `false` | `true` の場合だけ元ファイル名を履歴へ保存 |
| `TRANSCRIPTION_PRICE_PER_MINUTE` | モデル別 | 未登録の文字起こしモデルの単価上書き |

空文字は未設定と同じ扱い（既定に倒れる）。

## API

`POST /transcribe`（multipart）

| フィールド | 型 | 意味 |
|---|---|---|
| `file` | file | 音声または動画 |
| `prompt` | string | 用語ヒント（任意、200 文字まで） |
| `polish` | bool | LLM 校正の有無（既定 true） |

レスポンス（JSON）:

| キー | 型 | 意味 |
|---|---|---|
| `text` | string | 表示用本文。校正が成功していれば校正後、そうでなければ `raw` と同じ |
| `raw` | string | 書き起こし原文 |
| `polished` | bool | `text` が校正済みか |
| `model` | string | 書き起こしモデル |
| `duration` | number | 音声長（秒） |
| `segments` | int | 分割チャンク数 |
| `polish_partial` | bool | `polished=true` のときだけ存在。一部チャンクが校正に失敗して原文のまま |
| `polish_model` | string | `polished=true` のときだけ存在。校正モデル |

`polish=false`、書き起こしが空、全チャンクの校正が失敗した場合は `polished=false` で、`polish_partial` / `polish_model` は付かない。

## 利用履歴

`/history` で直近200件の利用量と費用を表示する。ブラウザの Basic 認証ではユーザー名は任意、パスワードに `HISTORY_PASSWORD` を入力する。

- `GET /api/history?limit=100`: 履歴と合計
- `GET /api/history/export.csv`: 最大500件のCSV
- 音声と文字起こし本文は保存しない
- ファイル名も既定では保存しない
- Supabaseへの保存失敗は文字起こし本体を失敗させない

SupabaseのSecret keyはRenderの環境変数だけに設定する。公開JavaScriptやGitHubへ書かないこと。2026年末に廃止予定の旧`service_role`キーではなく、`sb_secret_...`形式を使う。

## モデル比較

```bash
python3 scripts/ab_compare.py 音声.m4a --hint "射出成形、金型、粘度"
```

本番と同じ前処理・プロンプトで複数モデルを走らせ、`ab_out/<音声名>/compare.md` に並べる。25MB 超は対象外なので抜粋を渡す。

2026-08 の比較（実会議 4 分抜粋）では `gpt-transcribe` が欠落なし・専門用語ほぼ全正解で、`whisper-1`（中盤欠落）と `gpt-4o-transcribe`（冒頭 40 秒欠落）を上回った。

## デプロイ

master への push で `.github/workflows/render-deploy.yml` が Render の Deploy Hook を叩く。Secret `RENDER_DEPLOY_HOOK` に Hook URL を登録しておくこと。

Render 側の GitHub App 連携（push 検知の自動デプロイ）は動いていないので、この経路が本線。反映確認は次のとおり。

```bash
curl -s https://voice-transcriber2.onrender.com/sw.js | grep CACHE_NAME
```

`OPENAI_API_KEY`、`SUPABASE_SECRET_KEY`、`HISTORY_PASSWORD` は Render Dashboard で設定する（`render.yaml` は `sync: false`）。

## レビュー

PR を開くと Codex が自動でレビューする。Claude は PR コメントで `@claude` を呼ぶ（`.github/workflows/claude.yml`）。

## 作業ログ

`logs/yyyy-MM.md`。精度改善の経緯と判断根拠はここにある。
