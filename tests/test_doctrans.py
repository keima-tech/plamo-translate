"""doctrans.py の純関数テスト（デリミタ保持・フェンススキップ・連結恒等性）"""
import pytest

import doctrans


# ---------- parse_numbered ----------
def test_parse_numbered_basic():
    out = "1\tHello\n2\tWorld"
    assert doctrans.parse_numbered(out, [0, 1]) == ["Hello", "World"]


def test_parse_numbered_multiline_continuation():
    out = "1\tline one\nstill one\n2\ttwo"
    assert doctrans.parse_numbered(out, [0, 1]) == ["line one\nstill one", "two"]


def test_parse_numbered_missing_returns_none():
    assert doctrans.parse_numbered("1\tHello", [0, 1]) is None


def test_parse_numbered_empty_value_becomes_none():
    assert doctrans.parse_numbered("1\t\n2\tb", [0, 1]) == [None, "b"]


def test_parse_numbered_various_delimiters():
    out = "1. dot\n2、Japanese\n3) paren\n4\ttab"
    assert doctrans.parse_numbered(out, [0, 1, 2, 3]) == ["dot", "Japanese", "paren", "tab"]


# ---------- _split_lines_fence ----------
def test_fence_split_concat_identity():
    text = "# 見出し\n\n本文です。\n\n```\ncode = 1\n```\n\n末尾"
    units = doctrans._split_lines_fence(text)
    assert "".join(u[0] + u[1] for u in units) == text


def test_fence_block_is_single_skip_unit():
    text = "before\n```py\nimport os\nprint(1)\n```\nafter"
    units = doctrans._split_lines_fence(text)
    fences = [u for u in units if len(u) > 2 and u[2]]
    assert len(fences) == 1
    assert "import os" in fences[0][0]
    assert fences[0][0].startswith("```py")
    assert fences[0][0].endswith("```")


def test_unclosed_fence_marked_skip():
    text = "a\n```\nunclosed"
    units = doctrans._split_lines_fence(text)
    assert "".join(u[0] + u[1] for u in units) == text
    assert any(len(u) > 2 and u[2] for u in units)


# ---------- _pack_parts ----------
def test_pack_parts_concat_identity_md():
    text = "# タイトル\n\n段落一。\n\n```\nfenced\n```\n\n段落二。"
    parts = doctrans._pack_parts(doctrans._split_lines_fence(text))
    assert "".join(p[1] for p in parts) == text


def test_pack_parts_concat_identity_html():
    text = "<p>一。</p>\n<p>二。</p>\n\n<div>三。</div>\n"
    parts = doctrans._pack_parts(doctrans._split_html_blocks(text))
    assert "".join(p[1] for p in parts) == text


def test_pack_parts_fence_passes_as_lit():
    text = "本文\n```\ncode line\n```\n後"
    parts = doctrans._pack_parts(doctrans._split_lines_fence(text))
    segs = [t for k, t in parts if k == "seg"]
    lits = "".join(t for k, t in parts if k == "lit")
    assert all("code line" not in s for s in segs)
    assert "code line" in lits


def test_pack_parts_splits_long_segment():
    text = "あ" * (doctrans.MAX_CHUNK + 100)
    parts = doctrans._pack_parts(doctrans._split_lines_fence(text))
    for k, t in parts:
        if k == "seg":
            assert len(t) <= doctrans.MAX_CHUNK
    assert "".join(t for _, t in parts) == text


# ---------- _decode_text ----------
def test_decode_utf8():
    assert doctrans._decode_text("日本語テキスト".encode("utf-8")) == "日本語テキスト"


def test_decode_cp932_fallback():
    raw = "日本語のテキストです".encode("cp932")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # 前提: cp932 バイトは utf-8 として不正
    assert doctrans._decode_text(raw) == "日本語のテキストです"


def test_decode_rejects_binary_magic():
    with pytest.raises(ValueError):
        doctrans._decode_text(b"PK\x03\x04zipzip")
    with pytest.raises(ValueError):
        doctrans._decode_text(b"%PDF-1.7 ...")


# ---------- md_open / html_open ラウンドトリップ ----------
def test_md_open_roundtrip_identity():
    text = "# 見出し\n\n本文その一。\n\n```\ncode\n```\n\n本文その二。"
    data = text.encode("utf-8")
    segs, save = doctrans.md_open(data)
    assert save().decode("utf-8") == text  # 未翻訳なら完全一致
    assert len(segs) >= 3  # 見出し・本文2つ（フェンスはセグメント外）
    segs[0]["set_"]("HEADING")
    assert "HEADING" in save().decode("utf-8")


def test_md_open_preserves_whitespace_around_segments():
    text = "<p>  spaced  </p>\n\n<p>two</p>"
    segs, save = doctrans.html_open(text.encode("utf-8"))
    assert save().decode("utf-8") == text
    segs[0]["set_"](segs[0]["get"]().strip())
    assert save().decode("utf-8") == text  # strip しても head/tail 余白は復元


def test_html_open_block_segments():
    text = "<p>一。</p><p>二。</p>"
    segs, save = doctrans.html_open(text.encode("utf-8"))
    assert [s["get"]() for s in segs] == ["<p>一。</p>", "<p>二。</p>"]
    segs[0]["set_"]("<p>ONE</p>")
    assert save().decode("utf-8") == "<p>ONE</p><p>二。</p>"


# ---------- pack_batches ----------
def test_pack_batches_limits():
    texts = ["x" * 100] * 100
    batches = doctrans.pack_batches(texts, max_chars=500, max_items=3)
    assert all(len(b) <= 3 for b in batches)
    assert sorted(i for b in batches for i in b) == list(range(100))
