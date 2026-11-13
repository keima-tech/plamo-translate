"""main.py の純関数（チャンク分割・システムプロンプト・モデル選択）のテスト"""
import main
import pytest


# ---------- pick_default ----------
def test_pick_default_prefers_2b():
    names = ["hf.co/pfnet/plamo-3-translate-8b-GGUF:Q4_K_M",
             "hf.co/pfnet/plamo-3-translate-2b-GGUF:Q4_K_M"]
    assert main.pick_default(names) == names[1]


def test_pick_default_avoids_08b_false_match():
    # "-2b" が "0.8b" に誤マッチしないこと（ハイフン付きで一致させる仕様）
    names = ["plamo-3-translate-0.8b", "plamo-3-translate-8b"]
    assert main.pick_default(names) == "plamo-3-translate-8b"


def test_pick_default_empty():
    assert main.pick_default([]) is None


# ---------- split_chunks ----------
def test_split_short_text_single_chunk():
    assert main.split_chunks("こんにちは") == ["こんにちは"]


def test_split_empty_text():
    assert main.split_chunks("") == [""]


def test_split_long_plain_text_bounds_and_content():
    sents = [f"これは{i}番目の文です。" for i in range(600)]  # 約7文字×600 = 4200文字
    text = "".join(sents)
    chunks = main.split_chunks(text)
    assert len(chunks) >= 2
    assert all(len(c) <= main.MAX_CHUNK for c in chunks)
    joined = "".join(chunks)
    for s in sents:
        assert s in joined


def test_split_preserves_html_block_boundaries():
    blocks = [f"<p>{'あ'*100}{i}</p>" for i in range(60)]
    text = "\n".join(blocks)
    chunks = main.split_chunks(text)
    assert all(len(c) <= main.MAX_CHUNK for c in chunks)
    # 各チャンクはブロック終端で切れる（チャンク内で <p> と </p> の数が一致）
    for c in chunks:
        assert c.count("<p>") == c.count("</p>")


def test_split_hard_cut_for_single_huge_sentence():
    text = "あ" * (main.MAX_CHUNK * 2 + 500)
    chunks = main.split_chunks(text)
    assert all(len(c) <= main.MAX_CHUNK for c in chunks)
    assert "".join(chunks) == text


# ---------- build_system ----------
def test_build_system_basic():
    b = main.TranslateBody(text="_", target_lang="en")
    assert main.build_system(b) == "[plamo-translate]\ntarget_lang=en"


def test_build_system_source_and_glossary():
    b = main.TranslateBody(text="_", target_lang="ja", source_lang="en",
                           glossary=[{"src": "invoice", "dst": "請求書"}])
    sys_txt = main.build_system(b)
    assert "source_lang=en" in sys_txt
    assert "glossary=invoice => 請求書" in sys_txt


def test_build_system_form_only_for_ja_target():
    b = main.TranslateBody(text="_", target_lang="en", form="polite")
    assert "form=" not in main.build_system(b)
    b2 = main.TranslateBody(text="_", target_lang="ja", form="polite")
    assert "form=polite" in main.build_system(b2)
