# Yui Cloud Agent

**「入力されなかったタスク」を発見する対話エージェント。**

既存ツールは "入力したタスク" を管理する。Yui は独り言・思いつき・雑談から、対話しながらタスクを抽出し、優先度と理由を自律判断して Google Tasks に登録する。過去の言及を記憶し、「前にも言ったのに終わってないタスク」の優先度を昇格させて指摘する。さらに、ユーザーが何も言わなくても放置タスクをバックグラウンドで見直し、Google Search grounding で関連情報を裏どりして添えてくる。ブラウザからそのまま対話でき、返答は Chirp3-HD の音声で読み上げられる。

> DevOps × AI Agent Hackathon（ファインディ主催 / Google Cloud 協賛）提出作品

## アーキテクチャ

```
【対話】ユーザー ⇄ /chat
  Firestore(会話履歴) を踏まえて Gemini が会話しながらタスクを抽出
  ↓
Firestore: task_mentions に記録 → 表記揺れを超えた再言及検出 → 優先度昇格
  ↓
Google Tasks API: 「Yui」リストへ登録・更新（優先度を絵文字ラベル化）

【自律】Cloud Scheduler（30分毎）→ /autonomous-review
  放置タスクを検知 → 優先度を自動で見直し
  → 高優先度タスクは Google Search grounding で裏どり調査を添付
  → Google Tasks にも反映
```

すべて Cloud Run 上で動作。Vertex AI / Firestore の認証は Application Default Credentials（APIキーなし）。
Google Tasks のみ、アカウント所有者本人が一度だけ許可した OAuth refresh token を Secret Manager 経由で使用。
状態変更・課金が発生するエンドポイントは `X-Yui-Token`（Secret Manager 管理のアプリトークン）で保護し、
未認証の第三者がタスク捏造や Gemini 消費（コストDoS）を行えないようにしている（`docs/cicd-setup.md §4.5`）。

## 競合との違い

- **Circleback 等の会議系ツール**: 会議が前提。Yui は会議の外（独り言・思いつき）を拾う
- **既存 ToDo アプリ**: 単発の入力を管理する。Yui は履歴を跨いで判断し、忘れられたタスクを自分から昇格させ、自分から調べてくる

## エンドポイント

| Method | Path | 役割 |
|---|---|---|
| GET | `/` | 対話用 Web UI |
| GET | `/health` | ヘルスチェック |
| POST | `/process` | 単発メモからタスク抽出（一発抽出） |
| POST | `/chat` | 複数ターンの対話。会話しながらタスク抽出も継続 |
| POST | `/tts` | Chirp3-HD による音声合成 |
| POST | `/autonomous-review` | 放置タスクの優先度見直し＋裏どり調査（Cloud Scheduler が30分毎に呼ぶ） |

## ローカル実行

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# http://127.0.0.1:8000/health
```

## デプロイ

```bash
gcloud run deploy yui-agent --source . --region asia-northeast1 --allow-unauthenticated
```

## ロードマップ

- Speech-to-Text による音声メモ入力
- ローカル版 YuiChan（デスクバディ）との人格・記憶統合
- 壁打ち・ブレスト相手としての対話深化、不在時のアイデア検証
- 毎朝ブリーフィング / 複数サービス連携
