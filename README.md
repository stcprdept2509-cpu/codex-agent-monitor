# Agent Monitor

Codex / Claude Code のローカル実行状況を、ブラウザで見るための読み取り専用モニターです。

## できること

- Codex の `~/.codex/state_5.sqlite` と rollout jsonl を読み、稼働中 / 待機 / エラーを表示
- Claude Code の `~/.claude/projects/**/*.jsonl` を読み、ツール実行中かどうかを表示
- メインエージェント、サブエージェント、スキルの3階層を表示
- 仕事の種類はスキル名やタイトルから推測として表示
- 実データだけを表示し、CPU使用率や進捗率などの架空データは出しません

## 起動方法

```bash
python3 server.py
```

開くURL:

```text
http://127.0.0.1:8799/
```

Macならこちらでも起動できます。

```bash
./run.sh
```

## 他のPCで使う

通常はそのまま動きます。

```bash
git clone <this-repository-url>
cd codex-agent-monitor
python3 server.py
```

Codex や Claude Code の保存場所が標準と違う場合は、環境変数で指定できます。

```bash
CODEX_HOME="$HOME/.codex" \
CLAUDE_PROJECTS_DIR="$HOME/.claude/projects" \
PORT=8799 \
python3 server.py
```

Codex DBの場所だけを直接指定する場合:

```bash
CODEX_DB_PATH="$HOME/.codex/state_5.sqlite" python3 server.py
```

## 会社やチームで使う場合

このアプリは、起動したPCのローカルファイルを読みます。  
そのため、Vercelなどのクラウドに置くだけでは、他社や他メンバーのPC内にあるエージェント状態は読めません。

チーム利用の現実的な形は次のどちらかです。

1. 各PCでこのアプリをローカル起動する
2. 各PCに読み取り専用コレクターを置き、中央サーバーへ最小限の状態だけ送る

まずは 1 のローカル起動版が安全です。  
2 を作る場合は、認証、会社ごとの分離、送信データの匿名化、ログ保持期間を設計してから実装してください。

## セキュリティ

- このアプリはローカルファイルを読み取るだけで、Codex / Claude Code のデータを書き換えません。
- ブラウザは標準で `127.0.0.1` からだけ見る想定です。
- `HOST=0.0.0.0` にすると同じネットワーク内から見える可能性があります。社外秘の作業名が出るため、必要がない限り使わないでください。

## ヘルスチェック

```bash
curl http://127.0.0.1:8799/api/health
```

## データの正直さ

表示される数値や状態はローカルに存在する実データから作っています。  
取得できないものは推測せず、表示しません。
