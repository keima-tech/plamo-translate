"""PLaMo Translate — プライベート翻訳サービス (PLaMo 3 Translate + Ollama)"""
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import doctrans

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")  # 既定値。config.json があればそれが優先
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
GLOSSARY_FILE = DATA_DIR / "glossaries.json"
CONFIG_FILE = DATA_DIR / "config.json"
OUT_DIR = DATA_DIR / "out"
MAX_CHUNK = 2600  # 1チャンクの最大文字数（num_ctx 16384 に対して安全側）
NUM_CTX = 16384

app = FastAPI(title="PLaMo Local Translate")

STATIC_DIR = Path(__file__).parent / "static"


def pick_default(names):
    # 既定モデル: 2B > 8B > 先頭。-2b/-8b はハイフン付きで一致させ 0.8b の誤マッチを防ぐ
    for pat in ("-2b", "-8b"):
        for n in names:
            if pat in n.lower():
                return n
    return names[0] if names else None


# ---------- glossary storage ----------
def load_glossaries() -> dict:
    if GLOSSARY_FILE.exists():
        try:
            return json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_glossaries(g: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GLOSSARY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(GLOSSARY_FILE)


class GlossaryBody(BaseModel):
    name: str
    entries: list[dict]  # [{"src": "...", "dst": "..."}]


@app.get("/api/glossaries")
async def get_glossaries():
    return load_glossaries()


@app.put("/api/glossaries")
async def put_glossary(body: GlossaryBody):
    name = body.name.strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "不正な用語集名")
    entries = [
        {"src": e["src"].strip(), "dst": e["dst"].strip()}
        for e in body.entries
        if e.get("src", "").strip() and e.get("dst", "").strip()
    ]
    g = load_glossaries()
    g[name] = entries
    save_glossaries(g)
    return {"ok": True, "name": name, "count": len(entries)}


@app.delete("/api/glossaries/{name}")
async def delete_glossary(name: str):
    g = load_glossaries()
    if name not in g:
        raise HTTPException(404, "用語集が見つかりません")
    del g[name]
    save_glossaries(g)
    return {"ok": True}


# ---------- connection config (config.json / 再起動不要・mtime キャッシュ) ----------
# SSRF 回避のため、エンドポイントはブラウザからリクエスト毎には受け取らず
# サーバー側 DATA_DIR/config.json に保存する（api_key 平文保存のため 0600）。
_cfg_cache: dict = {"key": None, "data": {}}


def load_config() -> dict:
    """保存済み設定を解決して返す。ollama_url 未設定なら環境変数 OLLAMA_URL が既定。"""
    try:
        st = CONFIG_FILE.stat()
        ck = (st.st_mtime_ns, st.st_size)
    except OSError:
        ck = None
    if ck != _cfg_cache["key"]:
        data: dict = {}
        if ck is not None:
            try:
                parsed = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:  # noqa: BLE001
                data = {}
        _cfg_cache["key"] = ck
        _cfg_cache["data"] = data
    d = _cfg_cache["data"]
    return {
        "ollama_url": ((d.get("ollama_url") or "").strip() or OLLAMA_URL).rstrip("/"),
        "api_key": (d.get("api_key") or "").strip(),
        "model": (d.get("model") or "").strip(),
    }


def save_config(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)  # replace 前に付与（api_key 平文ファイルの無権限窓を無くす）
    tmp.replace(CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    _cfg_cache["key"] = None  # mtime キャッシュ無効化（同一秒内 save でも確実に再読）


def ollama_headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}


async def list_ollama_models(cfg: dict) -> list[str]:
    """モデル一覧取得。Ollama /api/tags を試し、失敗なら OpenAI 互換 /v1/models にフォールバック。
    両方失敗時は例外（呼び出し側で接続エラー理由として扱う）。"""
    url, headers = cfg["ollama_url"], ollama_headers(cfg)
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{url}/api/tags", headers=headers)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            r = await c.get(f"{url}/v1/models", headers=headers)
            r.raise_for_status()
            return [m.get("id") or m.get("name", "") for m in r.json().get("data", [])]


async def resolve_model(explicit: str | None) -> str:
    """明示指定 > config.json の model（リモート API 等の自動検出不対応環境用） > 自動検出。"""
    if explicit:
        return explicit
    cfg = load_config()
    if cfg["model"]:
        return cfg["model"]
    try:
        names = await list_ollama_models(cfg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Ollama に接続できません（{cfg['ollama_url']}）: {str(e)[:150]}")
    return pick_default([n for n in names if "plamo-3-translate" in n]) or ""


class ConfigBody(BaseModel):
    ollama_url: str | None = None   # None=変更しない、""=環境変数既定に戻す
    api_key: str | None = None      # None/""=既存維持、"__CLEAR__"=消去
    model: str | None = None        # None=変更しない、""=自動検出に戻す


def _config_public(cfg: dict) -> dict:
    key = cfg["api_key"]
    return {
        "ollama_url": cfg["ollama_url"],
        "model": cfg["model"],
        "api_key_set": bool(key),
        "api_key_masked": ("****" + key[-4:]) if key else "",
    }


def _read_raw_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/config")
async def get_config():
    return _config_public(load_config())


@app.put("/api/config")
async def put_config(body: ConfigBody):
    cur = _read_raw_config()
    if body.ollama_url is not None:
        url = body.ollama_url.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(400, "エンドポイント URL は http:// または https:// で始めてください")
        cur["ollama_url"] = url
    if body.api_key is not None:
        if body.api_key == "__CLEAR__":
            cur["api_key"] = ""
        elif body.api_key.strip():
            cur["api_key"] = body.api_key.strip()
        # 空文字は「既存維持」（UI のプレースホルダ仕様と対応）
    if body.model is not None:
        cur["model"] = body.model.strip()
    save_config(cur)
    return _config_public(load_config())


@app.post("/api/config/test")
async def config_test():
    """保存済み設定で接続テスト。成功時は検出モデル一覧、失敗時は理由（HTTP 200 で ok:false）"""
    cfg = load_config()
    try:
        names = await list_ollama_models(cfg)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": cfg["ollama_url"], "error": f"接続失敗: {str(e)[:200]}"}
    plamo = [n for n in names if "plamo-3-translate" in n]
    return {"ok": True, "url": cfg["ollama_url"], "models": names[:50], "plamo": plamo,
            "default": cfg["model"] or pick_default(plamo)}


# ---------- models ----------
@app.get("/api/models")
async def list_models():
    cfg = load_config()
    try:
        names = await list_ollama_models(cfg)
    except Exception:  # noqa: BLE001
        if cfg["model"]:  # /api/tags 非対応のリモート API でも model 指定なら動く
            return {"models": [cfg["model"]], "default": cfg["model"]}
        raise HTTPException(502, f"Ollama に接続できません（{cfg['ollama_url']}）")
    plamo = [n for n in names if "plamo-3-translate" in n]
    if cfg["model"]:
        return {"models": [cfg["model"]] + [n for n in plamo if n != cfg["model"]],
                "default": cfg["model"]}
    return {"models": plamo, "default": pick_default(plamo)}


# ---------- chunking ----------
_BLOCK_END = re.compile(
    r"(</(?:p|div|h[1-6]|li|tr|table|thead|tbody|section|article|blockquote|pre|ul|ol|figcaption|dt|dd|summary|details|caption)>)"
)
_SENT_SPLIT = re.compile(r"(?<=[。．.!?！？])|\n+")


def _split_blocks(para: str) -> list[str]:
    parts = _BLOCK_END.split(para)
    out = []
    for idx in range(0, len(parts), 2):
        seg = parts[idx] + (parts[idx + 1] if idx + 1 < len(parts) else "")
        if seg:
            out.append(seg)
    return out


def _hard_split(s: str) -> list[str]:
    parts, i = [], 0
    while i < len(s):
        seg = s[i : i + MAX_CHUNK]
        cut = -1
        if len(seg) == MAX_CHUNK:
            cut = max((seg.rfind(d) + len(d) for d in ("。", "\n", ". ", "、", " ")), default=-1)
        if cut > MAX_CHUNK // 2:
            parts.append(seg[:cut])
            i += cut
        else:
            parts.append(seg)
            i += MAX_CHUNK
    return parts


def split_chunks(text: str) -> list[str]:
    # 1) 空行 or HTMLブロック終端で分割
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if "</" in para and ">" in para:
            blocks.extend(_split_blocks(para))
        else:
            blocks.append(para)
    # 2) 長すぎるブロックは文単位で分割
    pieces: list[str] = []
    for b in blocks:
        if len(b) <= MAX_CHUNK:
            pieces.append(b)
            continue
        cur = ""
        for s in _SENT_SPLIT.split(b):
            for s2 in [s] if len(s) <= MAX_CHUNK else _hard_split(s):
                if len(cur) + len(s2) + 1 <= MAX_CHUNK:
                    cur += ("\n" if cur else "") + s2
                else:
                    if cur:
                        pieces.append(cur)
                    cur = s2
        if cur:
            pieces.append(cur)
    # 3) ギリギリまで結合
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if len(cur) + len(p) + 2 <= MAX_CHUNK:
            cur += ("\n\n" if cur else "") + p
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text]


# ---------- translation ----------
class TranslateBody(BaseModel):
    text: str
    target_lang: str = "en"
    source_lang: str | None = None
    form: str | None = None  # plain | polite (target=ja のときのみ)
    model: str | None = None
    glossary: list[dict] | None = None  # [{"src":..,"dst":..}]


def build_system(body: TranslateBody) -> str:
    lines = ["[plamo-translate]", f"target_lang={body.target_lang}"]
    if body.source_lang:
        lines.append(f"source_lang={body.source_lang}")
    if body.form and body.target_lang == "ja":
        lines.append(f"form={body.form}")
    for e in body.glossary or []:
        lines.append(f"glossary={e['src']} => {e['dst']}")
    return "\n".join(lines)


async def translate_chunk(client: httpx.AsyncClient, model: str, system: str, chunk: str) -> str:
    cfg = load_config()  # リクエスト毎に解決（保存後・再起動なしで次回から反映）
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": chunk},
        ],
        "stream": False,
        "temperature": 0,
        "options": {"num_ctx": NUM_CTX, "stop": ["<|plamo:tag|>", "<|plamo:bos|>"]},
    }
    r = await client.post(f"{cfg['ollama_url']}/v1/chat/completions", json=payload,
                          headers=ollama_headers(cfg), timeout=600)
    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"].strip()


@app.post("/api/translate")
async def translate(body: TranslateBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "テキストが空です")
    body.model = await resolve_model(body.model)
    if not body.model:
        raise HTTPException(503, "PLaMo 3 Translate モデルが Ollama に見つかりません")

    system = build_system(body)
    chunks = split_chunks(text)

    async def stream():
        async with httpx.AsyncClient() as client:
            # start に原文チャンク本文を含める（フロントでセグメント対照ビューを組むため）
            yield json.dumps({"ev": "start", "chunks": len(chunks), "texts": chunks}, ensure_ascii=False) + "\n"
            for i, ch in enumerate(chunks):
                try:
                    out = await translate_chunk(client, body.model, system, ch)
                except Exception as e:  # noqa: BLE001
                    yield json.dumps({"ev": "error", "i": i, "msg": str(e)[:300]}, ensure_ascii=False) + "\n"
                    return
                yield json.dumps({"ev": "chunk", "i": i, "text": out}, ensure_ascii=False) + "\n"
            yield json.dumps({"ev": "done"}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------- PDF translation (BabelDOC + shim) ----------
# BabelDOC は汎用プロンプトで OpenAI 互換 API を叩く。PLaMo 3 Translate は
# [plamo-translate] 形式しか正しく扱えないため、当アプリ内に shim を置き、
# BabelDOC → shim → Ollama の順に要求を書き換えて流す。
import asyncio
import shutil

SHIM_JOBS: dict[str, dict] = {}  # job -> {"system":..., "model":..., "count": 翻訳済みセグメント数}
_LANG_NAME = {"ja": "Japanese", "en": "English", "zh-hans": "Simplified Chinese",
              "zh-hant": "Traditional Chinese", "ko": "Korean", "fr": "French",
              "de": "German", "es": "Spanish", "it": "Italian", "ru": "Russian",
              "pt": "Portuguese", "nl": "Dutch", "th": "Thai", "vi": "Vietnamese"}
_MARKERS = ["Now translate the following text:", "次に、以下のテキストを翻訳してください：",
            "次に、以下のテキストを翻訳してください:"]


@app.post("/internal/shim/{job}/v1/chat/completions")
async def shim_chat(job: str, request: Request):
    ctx = SHIM_JOBS.get(job)
    if not ctx:
        raise HTTPException(404, "shim job not found")
    body = await request.json()
    user = ""
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            user = m.get("content") or ""
            break
    for mk in _MARKERS:  # プロンプト本体（翻訳対象）だけを取り出す
        if mk in user:
            user = user.split(mk, 1)[1].strip()
            break
    async with httpx.AsyncClient() as client:
        out = await translate_chunk(client, ctx["model"], ctx["system"], user)
    ctx["count"] = ctx.get("count", 0) + 1  # 進捗可視化用: 翻訳成功セグメント数
    return {"id": "chatcmpl-shim", "object": "chat.completion", "created": 0,
            "model": ctx["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": out},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


async def translate_pdf(data: bytes, body: "TranslateBody", glossary_csv: str | None):
    """BabelDOC をサブプロセスで実行。進歩 dict を yield して最後に PDF bytes を返す。

    クライアント切断（ジェネレータのキャンセル）でも全体タイムアウトでも
    finally で babeldoc を必ず kill する。kill しないと孤児化した babeldoc が
    消えた shim を叩き続けて 404 リトライストームを起こす。
    """
    job = uuid.uuid4().hex
    SHIM_JOBS[job] = {"system": build_system(body), "model": body.model, "count": 0}
    work = OUT_DIR / job
    work.mkdir(parents=True, exist_ok=True)
    proc = None
    timeout = int(os.environ.get("PDF_TIMEOUT_SECS", "1800"))  # デフォルト 30 分
    try:
        (work / "in.pdf").write_bytes(data)
        total_pages = await _pdf_pages(work / "in.pdf")
        yield {"ev": "start", "segments": 0, "pdf": True, "total_pages": total_pages}
        lang_in = _LANG_NAME.get(body.source_lang or "", body.source_lang or "auto")
        lang_out = _LANG_NAME.get(body.target_lang, body.target_lang)
        cmd = ["babeldoc", "--openai",
               "--openai-base-url", f"http://127.0.0.1:{os.environ.get('PORT','9130')}/internal/shim/{job}/v1",
               "--openai-model", "plamo-shim", "-k", "shim",
               "--lang-in", lang_in, "--lang-out", lang_out,
               "--files", str(work / "in.pdf"), "--output", str(work),
               "--no-auto-extract-glossary", "--skip-scanned-detection", "--no-dual",
               "--translate-table-text", "--min-text-length", "2"]
        if glossary_csv:
            (work / "glossary.csv").write_text(glossary_csv, encoding="utf-8")
            cmd += ["--glossary-files", str(work / "glossary.csv")]
        if not shutil.which("babeldoc"):
            raise RuntimeError("babeldoc がインストールされていません")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.STDOUT)
        # wait_for(proc.wait()) を毎回作り直すと、タイムアウト時のキャンセルで
        # 以降の wait() が即時復帰してループが空転し進捗が出なくなる（実測: 8分でprogress 2件）。
        # 単一の wait タスクを shield して再利用する。
        waiter = asyncio.create_task(proc.wait())
        secs = 0
        while not waiter.done():
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            except asyncio.TimeoutError:
                secs += 5
                if secs > timeout:
                    raise RuntimeError(f"PDF 翻訳がタイムアウトしました（{timeout}秒）。中断します")
                segs = SHIM_JOBS.get(job, {}).get("count", 0)
                if total_pages:
                    msg = f"PDF 翻訳中… 翻訳済み {segs} セグメント（全 {total_pages} ページ）"
                else:
                    msg = f"PDF 翻訳中… 翻訳済み {segs} セグメント（{secs}秒経過）"
                yield {"ev": "progress", "msg": msg, "segs": segs, "pages": total_pages}
        await waiter
        if proc.returncode != 0:
            raise RuntimeError(f"BabelDOC が失敗しました (exit {proc.returncode})")
        outs = list(work.glob("*.mono.pdf")) or list(work.glob("*.pdf"))
        if not outs:
            raise RuntimeError("翻訳 PDF が生成されませんでした")
        yield {"ev": "pdfdone", "bytes": outs[0].read_bytes()}
    finally:
        # 切断/タイムアウト/エラーのいずれでも babeldoc を確実に停止してから後片付け
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        SHIM_JOBS.pop(job, None)
        shutil.rmtree(work, ignore_errors=True)


# ---------- office file translation ----------

@app.post("/api/preview-file")
async def preview_file(file: UploadFile = File(...)):
    """Office ファイルから翻訳対象セグメントを抽出して一覧を返す（翻訳はしない）。
    PDF はセグメント抽出の概念が無いため対象外（フロントで即翻訳に回す）。"""
    name = file.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in doctrans.OPENERS:
        raise HTTPException(400, "プレビュー対応形式は .docx / .pptx / .xlsx / .md / .html / .txt です（PDF はプレビューなしで翻訳されます）")
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "ファイルが大きすぎます（100MBまで）")
    try:
        segs, _save = doctrans.OPENERS[ext](data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"ファイルを読み込めません: {str(e)[:200]}") from e
    segments = [str(s["get"]()).strip() for s in segs]
    segments = [s for s in segments if s]
    return {"name": name, "ext": ext, "count": len(segments), "segments": segments}


@app.post("/api/translate-file")
async def translate_file(
    file: UploadFile = File(...),
    target_lang: str = Form("en"),
    source_lang: str = Form(""),
    form: str = Form(""),
    model: str = Form(""),
    glossary: str = Form(""),
):
    name = file.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in doctrans.OPENERS and ext != ".pdf":
        raise HTTPException(400, "対応形式は .pdf / .docx / .pptx / .xlsx / .md / .markdown / .html / .htm / .txt です")
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "ファイルが大きすぎます（100MBまで）")
    if ext == ".pdf":
        return await _translate_pdf_endpoint(name, data, target_lang, source_lang, form, model, glossary)
    try:
        segs, save = doctrans.OPENERS[ext](data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"ファイルを読み込めません: {str(e)[:200]}") from e
    if not segs:
        raise HTTPException(422, "翻訳できるテキストが見つかりません")

    body = TranslateBody(
        text="_", target_lang=target_lang,
        source_lang=source_lang or None, form=form or None,
        model=model or None,
        glossary=load_glossaries().get(glossary) if glossary else None,
    )
    body.model = await resolve_model(body.model)
    if not body.model:
        raise HTTPException(503, "PLaMo 3 Translate モデルが Ollama に見つかりません")
    system = build_system(body)
    out_name = re.sub(r"\.[^.]+$", "", name) + "_訳" + ext
    token = f"{uuid.uuid4().hex}{ext}"

    async def stream():
        yield json.dumps({"ev": "start", "segments": len(segs)}, ensure_ascii=False) + "\n"
        try:
            async with httpx.AsyncClient() as client:
                async for done, total, pairs in doctrans.translate_all(
                    segs, lambda t: translate_chunk(client, body.model, system, t)
                ):
                    yield json.dumps({"ev": "batch", "i": done, "of": total}, ensure_ascii=False) + "\n"
                    if pairs:
                        # 確定済み (index, 原文, 訳文) ペアをバッチ毎に流す（翻訳後プレビュー用）
                        yield json.dumps({"ev": "seg", "pairs": pairs}, ensure_ascii=False) + "\n"
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            OUT_DIR.joinpath(token).write_bytes(save())
            yield json.dumps({"ev": "file", "url": f"api/file-out/{token}?name={quote(out_name)}",
                              "name": out_name}, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001
            yield json.dumps({"ev": "error", "msg": str(e)[:300]}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


async def _translate_pdf_endpoint(name, data, target_lang, source_lang, form, model, glossary):
    body = TranslateBody(
        text="_", target_lang=target_lang,
        source_lang=source_lang or None, form=form or None, model=model or None,
        glossary=load_glossaries().get(glossary) if glossary else None,
    )
    body.model = await resolve_model(body.model)
    if not body.model:
        raise HTTPException(503, "PLaMo 3 Translate モデルが Ollama に見つかりません")
    csv = None
    if body.glossary:
        csv = "\n".join(f"{e['src']},{e['dst']}" for e in body.glossary)
    out_name = re.sub(r"\.[^.]+$", "", name) + "_訳.pdf"
    token = f"{uuid.uuid4().hex}.pdf"

    async def stream():
        pdf_bytes = None
        try:
            # start イベント（total_pages 含む）は translate_pdf 側で yield する
            async for ev in translate_pdf(data, body, csv):
                if ev["ev"] == "pdfdone":
                    pdf_bytes = ev["bytes"]
                else:
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
            if pdf_bytes is None:
                raise RuntimeError("翻訳 PDF が生成されませんでした")
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / token).write_bytes(pdf_bytes)
            yield json.dumps({"ev": "file", "url": f"api/file-out/{token}?name={quote(out_name)}",
                              "name": out_name}, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001
            yield json.dumps({"ev": "error", "msg": str(e)[:300]}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.api_route("/api/file-out/{token}", methods=["GET", "HEAD"])
async def file_out(token: str, name: str = ""):
    _cleanup_out()
    path = OUT_DIR / Path(token).name
    if "/" in token or not path.exists():
        raise HTTPException(404, "成果物が見つかりません")
    return FileResponse(path, filename=name or path.name)


# ---------- visual render preview ----------
# Office/PDF → LibreOffice+poppler でページ画像化、md/html → iframe用HTML、txt → pre 用テキスト。
# soffice 変換はメモリを食うためグローバルロックで直列化、タイムアウト 60 秒。
import markdown as md_lib

RENDER_IMG_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}
RENDER_HTML_EXTS = {".html", ".htm", ".md", ".markdown"}
RENDER_TEXT_EXTS = {".txt"}
RENDER_MAX_PAGES = 20
RENDER_TIMEOUT = 60
RENDER_LOCK = asyncio.Lock()


def _cleanup_out() -> None:
    """2時間より古い成果物（render-* ディレクトリ含む）を掃除"""
    if not OUT_DIR.exists():
        return
    import time as _t
    now = _t.time()
    for f in OUT_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > 7200:
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink()
        except OSError:
            pass


def _read_text(data: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


async def _run(cmd: list[str], timeout: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError(f"変換がタイムアウトしました（{timeout}秒）")
    if proc.returncode != 0:
        msg = (err or b"").decode("utf-8", "replace").strip()[-200:]
        raise RuntimeError(f"変換に失敗しました (exit {proc.returncode}){'：' + msg if msg else ''}")


async def _pdf_pages(pdf: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        "pdfinfo", str(pdf), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 30)
    m = re.search(rb"Pages:\s+(\d+)", out or b"")
    return int(m.group(1)) if m else 0


_MD_PAGE = ("<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            "body{font-family:system-ui,-apple-system,'Noto Sans JP',sans-serif;margin:1rem;"
            "color:#1c1e21;background:#fff;font-size:14px;line-height:1.7;word-break:break-word}"
            "pre,code{font-family:ui-monospace,SFMono-Regular,monospace;background:#f4f5f7;border-radius:6px}"
            "pre{padding:.6rem;overflow:auto}code{padding:.1em .3em}pre code{padding:0}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3em .6em}"
            "img{max-width:100%}</style></head><body>")


async def render_document(src: Path, ext: str, job: str) -> dict:
    """ファイルを見た目プレビュー用データに変換。結果は OUT_DIR/render-<job>/meta.json にキャッシュ"""
    _cleanup_out()
    work = OUT_DIR / f"render-{job}"
    meta = work / "meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if ext in RENDER_HTML_EXTS:
        text = _read_text(src.read_bytes())
        if ext in (".md", ".markdown"):
            html = _MD_PAGE + md_lib.markdown(
                text, extensions=["fenced_code", "tables", "nl2br"]) + "</body></html>"
        else:
            html = text
        res = {"via": "iframe-html", "html": html}
    elif ext in RENDER_TEXT_EXTS:
        res = {"via": "pre", "text": _read_text(src.read_bytes())}
    elif ext in RENDER_IMG_EXTS:
        async with RENDER_LOCK:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            pdf = work / "doc.pdf"
            if ext == ".pdf":
                shutil.copyfile(src, pdf)
            else:
                profile = f"/tmp/lo-{job}"
                env = dict(os.environ, HOME="/tmp", XDG_CACHE_HOME="/data/cache")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "soffice", "--headless", "--norestore",
                        f"-env:UserInstallation=file://{profile}",
                        "--convert-to", "pdf", "--outdir", str(work), str(src),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env)
                    try:
                        _, err = await asyncio.wait_for(proc.communicate(), RENDER_TIMEOUT)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.wait()
                        raise RuntimeError("LibreOffice 変換がタイムアウトしました（60秒）")
                    if proc.returncode != 0:
                        msg = (err or b"").decode("utf-8", "replace").strip()[-150:]
                        raise RuntimeError(f"LibreOffice 変換に失敗しました (exit {proc.returncode}){('：' + msg) if msg else ''}")
                    produced = work / (src.stem + ".pdf")
                    if not produced.exists():
                        raise RuntimeError("LibreOffice が PDF を生成しませんでした")
                    produced.rename(pdf)
                finally:
                    shutil.rmtree(profile, ignore_errors=True)
            total = await _pdf_pages(pdf) or 1
            n = min(total, RENDER_MAX_PAGES)
            await _run(["pdftoppm", "-png", "-r", "100", "-f", "1", "-l", str(n),
                        str(pdf), str(work / "p")], RENDER_TIMEOUT)
            def _pgnum(p: Path) -> int:
                m = re.search(r"(\d+)", p.name)
                return int(m.group(1)) if m else 0
            pngs = sorted(work.glob("p-*.png"), key=_pgnum)
            pages = []
            for i, p in enumerate(pngs, 1):
                p.rename(work / f"p{i}.png")
                pages.append(f"api/render-out/{job}/p{i}.png")
            if not pages:
                raise RuntimeError("ページ画像を生成できませんでした")
            res = {"via": "images", "pages": pages, "total": total,
                   "omitted": max(0, total - n)}
    else:
        raise HTTPException(400, "レンダリング対応形式は .pdf / .docx / .pptx / .xlsx / .md / .html / .txt です")
    work.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


@app.post("/api/render-preview")
async def render_preview(file: UploadFile = File(...)):
    """アップロード元の原文を見た目プレビュー用に変換"""
    name = file.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in RENDER_IMG_EXTS | RENDER_HTML_EXTS | RENDER_TEXT_EXTS:
        raise HTTPException(400, "レンダリング対応形式は .pdf / .docx / .pptx / .xlsx / .md / .html / .txt です")
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "ファイルが大きすぎます（100MBまで）")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    job = uuid.uuid4().hex
    src = OUT_DIR / f"render-src-{job}{ext}"
    src.write_bytes(data)
    try:
        return await render_document(src, ext, job)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"レンダリングに失敗しました: {str(e)[:200]}") from e
    finally:
        src.unlink(missing_ok=True)


@app.post("/api/render-output")
async def render_output(token: str = Form(...)):
    """翻訳成果物（/data/out/<token>）を見た目プレビュー用に変換（render-* は token 名でキャッシュ）"""
    if "/" in token:
        raise HTTPException(400, "不正なトークンです")
    path = OUT_DIR / Path(token).name
    if not path.exists():
        raise HTTPException(404, "成果物が見つかりません")
    try:
        return await render_document(path, path.suffix.lower(), path.stem)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"レンダリングに失敗しました: {str(e)[:200]}") from e


@app.get("/api/render-out/{job}/{fname}")
async def render_out(job: str, fname: str):
    if not re.fullmatch(r"[0-9a-f]+", job) or not re.fullmatch(r"p\d+\.png", fname):
        raise HTTPException(404, "画像が見つかりません")
    path = OUT_DIR / f"render-{job}" / fname
    if not path.exists():
        raise HTTPException(404, "画像が見つかりません")
    return FileResponse(path, media_type="image/png")


# ---------- static ----------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "9130")))
