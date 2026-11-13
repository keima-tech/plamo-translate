"""pytest 用セットアップ: ホスト側 python で app/ を import できるようにする。

DATA_DIR / OLLAMA_URL は main import 前に設定する（モジュールレベルのグローバル）。
OLLAMA_URL はダミーポートにして、テストが誤って実 Ollama を叩かないようにする。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="plamo-test-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:59999"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
