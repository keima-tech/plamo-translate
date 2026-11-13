# -*- coding: utf-8 -*-
"""文書翻訳: docx/pptx/xlsx は書式保持で書き戻し、md/html/txt はデリミタ保持で再結合する"""
import io
import re

MAX_CHUNK = 2600  # 1セグメントの最大文字数（超過は文単位で分割）


# ---------- 抽出・書き戻しセグメント ----------
# segments: list[dict(get=callable->str, set_=callable(str))]

def _docx_paras(paras, segs):
    from docx.text.paragraph import Paragraph  # noqa: F401
    for p in paras:
        if p.text.strip():
            def mk(p=p):
                def set_(text):
                    runs = p.runs
                    if not runs:
                        p.add_run(text)
                        return
                    runs[0].text = text
                    for r in runs[1:]:
                        r.text = ""
                return set_
            segs.append({"get": (lambda p=p: p.text), "set_": mk()})


def _docx_tables(tables, segs):
    for t in tables:
        for row in t.rows:
            for c in row.cells:
                _docx_paras(c.paragraphs, segs)
                _docx_tables(c.tables, segs)  # 入れ子表


def docx_open(data):
    from docx import Document
    doc = Document(io.BytesIO(data))
    segs = []
    _docx_paras(doc.paragraphs, segs)
    _docx_tables(doc.tables, segs)
    for s in doc.sections:
        for hf in (s.header, s.footer):
            _docx_paras(hf.paragraphs, segs)
            _docx_tables(hf.tables, segs)
    def save():
        bio = io.BytesIO()
        doc.save(bio)
        return bio.getvalue()
    return segs, save


def _pptx_tf(tf, segs):
    for para in tf.paragraphs:
        if para.text.strip():
            def mk(para=para):
                def set_(text):
                    runs = para.runs
                    if not runs:
                        r = para.add_run()
                        r.text = text
                        return
                    runs[0].text = text
                    for r in runs[1:]:
                        r.text = ""
                return set_
            segs.append({"get": (lambda para=para: para.text), "set_": mk()})


def _pptx_shapes(shapes, segs):
    for sh in shapes:
        if hasattr(sh, "shapes"):  # グループ
            _pptx_shapes(sh.shapes, segs)
            continue
        if getattr(sh, "has_text_frame", False):
            _pptx_tf(sh.text_frame, segs)
        if getattr(sh, "has_table", False):
            for row in sh.table.rows:
                for c in row.cells:
                    _pptx_tf(c.text_frame, segs)


def pptx_open(data):
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    segs = []
    for slide in prs.slides:
        _pptx_shapes(slide.shapes, segs)
        if slide.has_notes_slide:
            _pptx_tf(slide.notes_slide.notes_text_frame, segs)
    def save():
        bio = io.BytesIO()
        prs.save(bio)
        return bio.getvalue()
    return segs, save


def xlsx_open(data):
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    segs = []
    for ws in wb.worksheets:
        if not ws.sheet_state == "visible":
            continue
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.strip() and not v.startswith("="):
                    def mk(cell=cell):
                        def set_(text):
                            cell.value = text
                        return set_
                    segs.append({"get": (lambda cell=cell: cell.value), "set_": mk()})
    def save():
        bio = io.BytesIO()
        wb.save(bio)
        return bio.getvalue()
    return segs, save


# ---------- テキスト系文書（md / html / txt）: デリミタ保持セグメント ----------

def _decode_text(data: bytes) -> str:
    if data[:4] == b"PK\x03\x04" or data[:4] == b"%PDF":
        raise ValueError("ZIP/PDF バイナリがテキスト拡張子で指定されました（拡張子を確認してください）")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp932")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
# 文末（句読点＋閉じ括弧/余白）または連続改行。キャプチャでデリミタを失わず分割できる
_SENT_END = re.compile(r"[。．.!?！？]+[」』\"'”）)』]*[ \t]*|\n+")
_HTML_BLOCK_END = re.compile(
    r"(</(?:p|div|h[1-6]|li|tr|table|thead|tbody|section|article|blockquote|pre|ul|ol|figcaption|dt|dd|summary|details|caption)>[\s]*)",
    re.I,
)


def _split_blank_lines(text: str):
    """空行で分割 -> [(unit, delim)]、連結すると原文と一致。"""
    parts = re.split(r"(\n[ \t]*\n[\t ]*)", text)
    units = []
    for i in range(0, len(parts), 2):
        units.append((parts[i], parts[i + 1] if i + 1 < len(parts) else ""))
    return units


def _split_lines_fence(text: str):
    """行単位（デリミタ=\n）。``` / ~~~ フェンスブロックは1ユニットにまとめ、
    3番目要素 True で「翻訳スキップ（lit 素通し）」として印を付ける。"""
    lines = text.split("\n")
    units: list = []
    in_fence = False
    buf: list = []
    for idx, line in enumerate(lines):
        d = "\n" if idx < len(lines) - 1 else ""
        if _FENCE_RE.match(line):
            if not in_fence:
                in_fence = True
                buf = [line]
                continue
            buf.append(line)
            units.append(("".join(b + "\n" for b in buf[:-1]) + buf[-1], d, True))
            in_fence = False
            buf = []
            continue
        if in_fence:
            buf.append(line)
            continue
        units.append((line, d))
    if in_fence:  # 閉じフェンスなしの不正終了もスキップ扱いで原文保持
        units.append(("".join(b + "\n" for b in buf[:-1]) + buf[-1], "", True))
    return units


def _split_html_blocks(text: str):
    """ブロック終端タグ（</p> 等）で分割、終端タグはセグメント末尾に含める。"""
    parts = _HTML_BLOCK_END.split(text)
    units = []
    for i in range(0, len(parts), 2):
        seg = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if seg:
            units.append((seg, ""))
    return units


def _best_cut(s: str, max_chars: int) -> int:
    cut = max((s.rfind(d, 0, max_chars) + len(d) for d in ("。", "\n", ". ", "、", " ")), default=-1)
    return cut if cut > max_chars // 2 else max_chars


def _split_long(s: str, max_chars: int):
    """長ユニットを文境界で分割、連結すると s と一致。"""
    pieces, last = [], 0
    for m in _SENT_END.finditer(s):
        pieces.append(s[last:m.end()])
        last = m.end()
    if last < len(s):
        pieces.append(s[last:])
    out = []
    for p in pieces:
        while len(p) > max_chars:
            cut = _best_cut(p, max_chars)
            out.append(p[:cut])
            p = p[cut:]
        if p:
            out.append(p)
    return out


def _pack_parts(units, max_chars: int = MAX_CHUNK):
    """units -> [("lit"|"seg", text)]。lit は原文デリミタのまま復元、seg が翻訳対象。"""
    parts: list = []
    lit = ""

    def emit_seg(s):
        nonlocal lit
        if lit:
            parts.append(("lit", lit))
            lit = ""
        parts.append(("seg", s))

    for unit in units:
        t, d = unit[0], unit[1]
        skip = len(unit) > 2 and unit[2]
        if skip or not t.strip():
            lit += t + d  # 翻訳スキップ（コードフェンス等）と空白は原文のまま素通し
            continue
        if len(t) <= max_chars:
            emit_seg(t)
            lit += d
            continue
        cur = ""
        for piece in _split_long(t, max_chars):
            if not piece.strip():
                lit += piece
                continue
            if cur and len(cur) + len(piece) <= max_chars:
                cur += piece
                continue
            if cur:
                emit_seg(cur)
            cur = piece
        if cur:
            emit_seg(cur)
            cur = ""
        lit += d
    if lit:
        parts.append(("lit", lit))
    return parts


def _text_opener(splitter):
    def opener(data: bytes):
        text = _decode_text(data)
        parts = _pack_parts(splitter(text))
        texts = [p[1] for p in parts]
        segs = []
        for i, p in enumerate(parts):
            if p[0] != "seg":
                continue
            # translate_all が strip するため、セグメント前後の余白（HTML の改行など）は set_ で復元
            head = p[1][:len(p[1]) - len(p[1].lstrip())]
            tail = p[1][len(p[1].rstrip()):]

            def mk(i=i, head=head, tail=tail):
                def set_(t):
                    texts[i] = head + t + tail
                return set_
            segs.append({"get": (lambda i=i: texts[i]), "set_": mk()})

        def save():
            return "".join(texts).encode("utf-8")
        return segs, save
    return opener


md_open = _text_opener(_split_lines_fence)
html_open = _text_opener(_split_html_blocks)

OPENERS = {
    ".docx": docx_open, ".pptx": pptx_open, ".xlsx": xlsx_open,
    ".md": md_open, ".markdown": md_open, ".txt": md_open,
    ".html": html_open, ".htm": html_open,
}


# ---------- 番号付きバッチ翻訳 ----------

def pack_batches(texts, max_chars=1200, max_items=25):
    batches, cur, curlen = [], [], 0
    for i, t in enumerate(texts):
        if cur and (curlen + len(t) > max_chars or len(cur) >= max_items):
            batches.append(cur)
            cur, curlen = [], 0
        cur.append(i)
        curlen += len(t)
    if cur:
        batches.append(cur)
    return batches


_NUM = re.compile(r"^\s*(\d+)[\t.、\)]\s?(.*)$")


def parse_numbered(out, idxs):
    """'N\ttext' 形式の応答を idxs 順に分解。1つでも欠けたら None（個訳フォールバック用）"""
    m, cur = {}, None
    for line in out.splitlines():
        mm = _NUM.match(line)
        if mm:
            cur = int(mm.group(1))
            m[cur] = mm.group(2)
        elif cur is not None:
            m[cur] += "\n" + line
    if any(i + 1 not in m for i in idxs):
        return None
    return [m[i + 1].strip() or None for i in idxs]


async def translate_all(segs, translate_fn):
    """segs をバッチ翻訳して書き戻す。
    進歩 yield: (done, total_batches, pairs) — pairs はそのバッチで確定した
    [[idx, 原文, 訳文], ...]。訳文なし（翻訳失敗）は空文字で返し、原文は書き換えない。"""
    texts = [str(s["get"]()).strip() for s in segs]
    batches = pack_batches(texts)
    for bi, b in enumerate(batches):
        prompt = "\n".join(f"{n + 1}\t{texts[n]}" for n in b)
        got: dict[int, str] = {}
        try:
            parsed = parse_numbered(await translate_fn(prompt), b)
        except Exception:
            parsed = None
        if parsed is None:  # バッチ失敗 → 個々翻訳にフォールバック
            for n in b:
                try:
                    t = await translate_fn(texts[n])
                except Exception:
                    t = ""
                if t:
                    got[n] = t
                    segs[n]["set_"](t)
        else:
            for n, t in zip(b, parsed):
                if t:
                    got[n] = t
                    segs[n]["set_"](t)
        pairs = [[n, texts[n], got.get(n, "")] for n in b]
        yield bi + 1, len(batches), pairs
