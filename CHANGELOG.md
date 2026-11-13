# CHANGELOG

PLaMo Local Translate の変更履歴。形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、バージョン管理は [Semantic Versioning](https://semver.org/lang/ja/) に準拠します。

## [Unreleased]

### Changed
- リポジトリ名を `plamo-translate` から `plamo-local-translate` に変更（Forgejo/GitHub とも旧名はリダイレクト。PFN 公式 CLI の PyPI 名との混同回避）
- アプリ表示名を「PLaMo翻訳」から「PLaMo Local Translate」に変更（PFN 公式サービス「PLaMo™翻訳」との同名を回避）。ヘッダーバッジを「Built with PLaMo 3 Translate」に更新
- README・UI フッターに「本プロジェクトは非公式で Preferred Networks と関係がない」旨の免責事項を追加

## [1.0.0] - 2026-11-13

初版リリース。

### Added

- **テキスト翻訳** — 53 言語（日本語⇄英語ほか）。用語集（固有名詞・専門用語の対訳指定）、日本語への敬体/常体指定、長文の自動チャンク分割（HTML/Markdown 構造保持）、NDJSON ストリーミング
- **レイアウト保持の文書翻訳** — Word / PowerPoint / Excel / Markdown / HTML / テキスト（書式・構造を書き戻し）と PDF（BabelDOC 経由。短い見出し・表内テキストにも対応）
- **翻訳プレビュー** — 翻訳前後の「見た目」プレビュー（LibreOffice / poppler によるページ画像レンダリング。日本語 PDF の非埋め込みフォントにも poppler-data で対応）、原文⇄訳文のセグメント対照と hover 双方向ハイライト、ズーム（50〜400%・Ctrl+ホイール・連動）、リサイズ可能モーダル、Markdown コードフェンス（``` / ~~~）の翻訳スキップ
- **進捗と履歴** — 翻訳進捗のプログレスバー（Office/md/html は確定バー、PDF は翻訳済みセグメント数＋不定バー）、セッション内翻訳履歴（最大 20 件、プレビュー再表示・ダウンロード可）
- **柔軟なバックエンド** — Ollama（推奨・モデル自動検出）/ llama.cpp（llama-server）/ LM Studio。WebUI の ⚙ 接続設定からエンドポイント URL・API キー（マスク保存）・モデル名を再起動不要で変更可能。接続テスト付き
- **ローカル完結** — 既定で 127.0.0.1 バインド。リモート API を自分で設定しない限り、原文も訳文も自分のマシンから出ない
- **UI** — 本家 PLaMo翻訳 準拠のデザイン（原文/訳文ペイン、⇄スワップ）、ライト/ダークテーマ、モバイル対応、ドラッグ&ドロップ投入、ラベルとコントロール間の余白調整
- **デプロイ** — Docker Compose 一発起動（LibreOffice・poppler・日本語フォント同梱）
- **テスト・CI** — pytest 42 件（チャンク分割・デリミタ保持・フェンススキップ・設定 API など）＋ GitHub Actions CI

### Known limitations

- ⚙ 接続設定画面に認証はありません。信頼できないネットワークに公開する場合はリバースプロキシ等で認証を必ず追加してください
- OCR 未対応（スキャン PDF は翻訳できません）
- 「見た目」プレビューの再現度は LibreOffice / poppler のレンダリングに依存します（複雑な図形・SmartArt 等は崩れ得る。DL 成果物自体は python-docx / python-pptx / openpyxl / BabelDOC の品質）
- 翻訳履歴はメモリ保持のみで、ブラウザ再読み込みすると消えます
- Word 段落内の混在書式は先頭書式に統一されます（段落単位翻訳のトレードオフ）
- 詳細な使い方・導線は [README.md](README.md)（日本語）/ [README.en.md](README.en.md) を参照してください
