#!/usr/bin/env python3
"""
yomiyasu_lint.py - 日本語文章のAIっぽさ（LLM-Slop）数値化・機械的静的検査スクリプト

Qiita 7万件の計量調査、統語構造復元論、AI語彙の出現頻度分析に基づく
決定論的リンター。標準ライブラリのみで動作。
"""

import os
import sys
import re
import argparse
import json
import math
import unicodedata
from typing import List, Dict, Any, Tuple, Optional, Union


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILES_PATH = os.path.join(SCRIPT_DIR, "profiles.json")

# プロファイルなし（default）の検査項目。従来のyomiyasuと同じ検査になる値にしてある。
# 用途プロファイル（scripts/profiles.json）は、この値を上書きする形で書く。
BASE_PROFILE: Dict[str, Any] = {
    "description": "プロファイルなし（従来のyomiyasuと同じ検査）",
    "emoji_max": 0,                 # 1文書で使ってよい絵文字の数
    "emoji_allowed": None,          # 使ってよい絵文字の一覧（None なら種類は問わない）
    "trailing_colon": "warn",       # 文末コロン: warn / info / off
    "list_ratio_warn": 0.25,        # 箇条書き比率の警告ライン（None で検査しない）
    "bold_per_1000_warn": 3.0,      # 太字頻度の警告ライン（None で検査しない）
    "sentence_end_repetition": True,
    # 以下は用途プロファイルで使う検査。default では行わない
    "sentence_length": None,        # {"mean": [下限, 上限] | None, "max": 字数 | None, "sd_min": 値 | None}
    "short_burst": False,           # 10字未満の文が3つ続くところを検出する
    "list_item_max": None,          # 箇条書き1項目の字数の上限
    "title_max": None,              # タイトル（# 見出し）の字数の上限
    "bullet_endings": False,        # 箇条書きの語尾（です・ます / 体言止め / 動詞止め）のそろい方を検査する
    "stiffness_level": 0,           # 硬さ辞書を使う強さ（0〜3）
    "humanize_slop": None,          # 人間らしさスロップの上限 {パターンID: 回数}。None で検査しない
    "subject_repetition": False,    # 同じ主語のくり返しを検出する
    "markdown_unrendered": "off",   # 表示されないMarkdown（**、見出し、表）: warn / info / off
    "x_weighted_max": None,         # Xの文字数上限（全角2・半角1で数える。280で全角140字）
    "hashtag_max": None,            # ハッシュタグの上限
    "placeholder_max": None,        # 書き手に埋めてもらう空欄「[ここに…]」の上限
}


def load_profiles(extra_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """同梱の profiles.json と、追加の設定ファイル（任意）からプロファイルを読み込む。
    "extends" で別のプロファイルを引き継げる。"_" で始まるキーは注記として読み飛ばす。"""
    profiles: Dict[str, Dict[str, Any]] = {"default": dict(BASE_PROFILE)}
    paths = [DEFAULT_PROFILES_PATH]
    if extra_path:
        if not os.path.exists(extra_path):
            raise FileNotFoundError(extra_path)
        paths.append(extra_path)
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for name, conf in data.items():
            if name.startswith("_"):
                continue
            parent = conf.get("extends", "default")
            if parent not in profiles:
                raise ValueError(f"プロファイル「{name}」の extends「{parent}」が見つかりません（先に定義してください）")
            merged = dict(profiles[parent])
            merged.update({k: v for k, v in conf.items() if k != "extends"})
            profiles[name] = merged
    return profiles


# 絵文字正規表現パターン（CJK統合漢字拡張などのサロゲートペア漢字を除外した厳密な絵文字範囲）
EMOJI_PATTERN = re.compile(
    r"[\U0001F600-\U0001F64F]"  # Emoticons
    r"|[\U0001F300-\U0001F5FF]"  # Misc Symbols and Pictographs
    r"|[\U0001F680-\U0001F6FF]"  # Transport and Map
    r"|[\U0001F700-\U0001F77F]"  # Alchemical Symbols
    r"|[\U0001F780-\U0001F7FF]"  # Geometric Shapes Extended
    r"|[\U0001F800-\U0001F8FF]"  # Supplemental Arrows-C
    r"|[\U0001F900-\U0001F9FF]"  # Supplemental Symbols and Pictographs
    r"|[\U0001FA00-\U0001FA6F]"  # Chess Symbols
    r"|[\U0001FA70-\U0001FAFF]"  # Symbols and Pictographs Extended-A
    r"|[\u2600-\u27BF]"          # Misc Symbols, Dingbats
    r"|[\u2300-\u23FF]"          # Misc Technical
    r"|[\u2B50-\u2B55]"
)

# 2026年最新AIスロップ語彙リスト
SLOP_WORDS = [
    # 質感を装う疑似具体語
    "手触り", "肌感", "肌感覚", "体温", "温度感", "熱量", "血の通った", "泥臭い", "泥臭さ",
    # 認知・評価を装う語
    "解像度", "腹落ち", "メンタルモデル", "本質的", "地に足のついた", "等身大",
    # 抽象比喩名詞
    "営み", "装置", "意思決定OS", "土台", "羅針盤", "起爆剤", "触媒",
    # 必殺技造語（体験の壮大化）
    "真理", "虚飾", "境地", "美学", "深淵", "冷徹", "禁欲的", "優美", "極致", "宿命",
    # 2026年急増語（文脈によるが要点検）
    "正本",
]

# 比喩動詞・AI偏愛動詞パターン
METAPHOR_VERB_PATTERNS = [
    (r"(地味に|よく|じわじわ)効[かきくけいた]", "比喩動詞「効く」の過剰使用"),
    (r"(データ|仕様|設計|環境|ビルド|システム|秩序)が(静かに)?壊れ", "比喩動詞「壊れる」"),
    (r"静かに(壊れ|落ち|失敗|沈黙)", "英語直訳「静かに壊れる (silently fail)」"),
    (r"黙って(無視|捨て|スキップ|破棄)", "英語直訳「黙って無視される」"),
    (r"側に倒[すしせ]", "判断を方向で表現する「〜側に倒す」"),
    (r"時間[をに]溶か[したす]", "比喩動詞「時間を溶かす」"),
    (r"(1つずつ|一つずつ)潰[していく]", "比喩動詞「潰す」"),
    (r"(実装|詳細|コード|設計|内部|仕組み|領域|本質)(に|まで|へ)踏み込[んむみま]", "比喩動詞「踏み込む」"),
    (r"動かしながら引き返[すし]", "比喩動詞「引き返す」"),
    (r"代わりに添え[るた]", "比喩動詞「添える」"),
    (r"(議論|意見|結論|方向性|価格|話題|検討)が[^。！？!?]*?収斂", "比喩動詞「収斂する」"),
    (r"した瞬間に?", "英語直訳「〜した瞬間 (the moment ...)」"),
    (r"(前提|基盤)が崩れ[るた]", "抽象比喩「前提が崩れる」"),
    (r"文化が醸成", "非生物主語「文化が醸成される」"),
    (r"プロセスが定着", "非生物主語「プロセスが定着する」"),
    (r"事例が残した", "非生物主語「事例が残した」"),
]

# メタフィラー・定型句
FILLER_PATTERNS = [
    (r"^(まず|ここで)?重要なのは、?", "前置フィラー「重要なのは」"),
    (r"^結論から言うと、?", "前置フィラー「結論から言うと」"),
    (r"^正直に言うと、?", "前置フィラー「正直に言うと」"),
    (r"^避けたいのは、?", "前置フィラー「避けたいのは」"),
    (r"いかがでした(でしょうか|か)?[？?。]?$", "定型クロージング「いかがでしたでしょうか」"),
    (r"ぜひ(参考|試し|活用)(に)?して(みて)?ください[！!。]?", "定型クロージング「ぜひ〜してみてください」"),
    (r"〜に他なりません", "過剰な自己ラベリング「〜に他なりません」"),
]

# ネガティブパラレリズム（AではなくB）
NEGATIVE_PARALLELISM_PATTERN = re.compile(r"([^。、]+)ではなく、?([^。、]+)")

# 硬さ辞書（references/humanize/stiffness.md）
# (開く強さ, 正規表現, 見つけた言い方, ふだんの言い方の例)。プロファイルの stiffness_level 以下の強さのものを検査する
STIFFNESS_PATTERNS = [
    (1, r"ことが可能", "〜することが可能です", "〜できます"),
    (1, r"(確認|作業|対応|調査|検証|設定|説明|連絡|共有|報告|議論|修正|変更|登録|分析|検討)を(行|おこな)[いうっわえ]",
     "〜を行う", "〜する（「確認を行う」→「確認する」）"),
    (1, r"という形(になります|になる|です|で進め)", "〜という形になります", "〜します / 〜です"),
    (1, r"ということになります", "〜ということになります", "〜です"),
    (1, r"次第(です|でございます)", "〜の次第です", "〜です"),
    (1, r"でございます", "〜でございます", "〜です"),
    (2, r"において", "〜において", "〜で"),
    (2, r"に関して|に関しまして", "〜に関して", "〜について"),
    (2, r"を要(する|します|しました|した|し、)", "〜を要する", "〜がかかる / 〜が要る"),
    (2, r"発生(する|します|した|しました|し|いたし)", "発生する", "起きる / 出る"),
    (2, r"迅速[にな]", "迅速に", "すぐに / 早めに"),
    (2, r"円滑[にな]", "円滑に", "スムーズに / 問題なく"),
    (2, r"担保", "担保する", "守る / 確保する / 保証する"),
    (2, r"に資する|に寄与", "〜に資する / 〜に寄与する", "〜に役立つ"),
    (2, r"の観点から", "〜の観点から", "〜から見ると / 〜を考えると"),
    (2, r"を活用", "〜を活用する", "〜を使う"),
    (2, r"を推進", "〜を推進する", "〜を進める"),
    (2, r"不可欠", "不可欠です", "欠かせません / 必要です"),
    (2, r"を図(る|り|ります|って)", "〜を図る", "〜する"),
    (2, r"が可能(です|となります|になります|だ|である)", "〜が可能です", "〜できます"),
    (2, r"となります", "〜となります", "〜です（変化を表す「〜となる」は残す）"),
    (3, r"したがって|すなわち|ならびに", "したがって / すなわち / ならびに", "なので / つまり / と"),
    (3, r"と考えております|と存じます", "〜と考えております", "〜と思っています"),
    (3, r"のほど、?(よろしく)?お願い(いたし|申し上げ)", "〜のほどよろしくお願いいたします", "〜をお願いします"),
]

# 1文書に何度も出ると硬くなる言い方（2回目から検出）。(開く強さ, 正規表現, 見つけた言い方, ふだんの言い方の例)
STIFFNESS_REPEAT_PATTERNS = [
    (1, r"させていただ[きくけい]", "〜させていただきます", "〜します（相手の許可をもらう場面だけ残す）"),
    (1, r"いただけますと幸いです|いただければ幸いです", "〜いただけますと幸いです", "〜をお願いします / 〜してください"),
]

# 人間らしさスロップ（references/humanize/humanize-slop.md）。(ID, 正規表現, 説明)
# プロファイルの humanize_slop に上限を書く。書いていないIDの上限は0回
HUMANIZE_SLOP_PATTERNS = [
    ("shoujiki", r"正直(に言うと|に言えば|なところ|、)", "「正直、」の前置き"),
    ("bucchake", r"ぶっちゃけ|マジで|ガチで", "演出としての口語（ぶっちゃけ、マジで、ガチで）"),
    ("nandesuyone", r"んですよね", "「〜なんですよね」"),
    ("janaidesuka", r"じゃないですか", "「〜じゃないですか」"),
    ("ttekanji", r"って感じ(です|でした|。|$)|的な(感じ|。|$)", "「〜って感じ」「〜的な」のぼかし"),
    ("exclaim", r"！|!(?![\[=])", "感嘆符「！」"),
    ("warai", r"（笑）|\(笑\)|(?<![一-龥ぁ-ん])笑(?=[。\s]|$)|ｗ{2,}|(?<![A-Za-z])w{3,}(?![A-Za-z.])", "「笑」「w」"),
    ("jitsuha", r"実は|意外と|ちなみに|ここだけの話", "「実は」「意外と」「ちなみに」"),
    ("rhetorical", r"ではないでしょうか", "修辞疑問「〜ではないでしょうか」"),
    ("ad_question", r"(で|に)(困って|悩んで)(い)?ませんか", "広告の問いかけ「〜で困っていませんか」"),
    ("greeting", r"(みなさん|皆さん|皆様)、?こんにちは", "定型の挨拶「みなさん、こんにちは」"),
    ("ikimashou", r"いきましょう[！!]", "定型の呼びかけ「〜していきましょう！」"),
    ("sns_bracket", r"【(保存版|必見|朗報|永久保存版?|悲報|拡散希望)】", "煽りの見出し【保存版】【必見】"),
    ("sns_title", r"(な件|してみた)[。！!]?$", "定型タイトル「〜な件」「〜してみた」"),
    ("thread_point", r"👇|🧵|スレッドで(解説|まとめ)", "スレッド誘導「👇」「スレッドで解説」"),
    ("fabricated", r"(先日|この前|以前)、?(私|僕|自分|筆者)も", "体験談の書き出し（元の文にある体験か確かめる）"),
]

# 書き手に埋めてもらう空欄
PLACEHOLDER_PATTERN = re.compile(r"\[ここに[^\]]*\]")

# 文頭の主語（「インフラチームは」「私は」）
SUBJECT_HEAD_PATTERN = re.compile(r"^([一-龥々ァ-ヶーA-Za-z0-9＆&・]{1,15}?)(では|は|が)、?")

# ハッシュタグ（行頭の「# 見出し」は除く）
HASHTAG_PATTERN = re.compile(r"(?<![\w#＃&])[#＃](?![#＃\s])[^\s#＃、。]+")


def get_frontmatter_line_count(lines: List[str]) -> int:
    """YAMLフロントマター（先頭の --- から 次の --- まで）の行数を返す"""
    if not lines or lines[0].strip() != "---":
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return idx + 1
    return 0


def extract_plain_sentences(text: str, min_len: int = 4, dot_bullets: bool = False) -> List[Tuple[int, str]]:
    """コードブロックや引用、箇条書きを除去し、地の文の段落文（行番号つき）を抽出する（min_len 字未満の断片は除く）。
    dot_bullets=True のときは「・」で始まる行も箇条書きとして除く（用途プロファイルで使う）"""
    lines = text.split("\n")
    sentences = []
    in_code_block = False
    fm_lines = get_frontmatter_line_count(lines)

    for idx, line in enumerate(lines, 1):
        if idx <= fm_lines:
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        # 空行、見出し、表行、画像記法、HTMLタグ、引用行、箇条書き行、インデントされたリスト継続行は地の文から除外
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith("|")
            or stripped.startswith("![")
            or stripped.startswith("[![")
            or stripped.startswith("<")
            or stripped.startswith(">")
            or re.match(r"^[-*+]\s|^\d+\.\s", stripped)
            or (dot_bullets and stripped.startswith("・"))
            or line.startswith("  ")
            or line.startswith("\t")
        ):
            continue

        # 文の区切り（。！？または行末）
        raw_sents = re.split(r"(?<=[。！？])", stripped)
        for s in raw_sents:
            s_clean = s.strip()
            if s_clean and len(s_clean) >= min_len:
                sentences.append((idx, s_clean))

    return sentences


def check_sentence_end_repetitions(sentences: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
    """3文以上連続する同一語尾の検知"""
    findings = []
    end_types = []

    for line_no, s in sentences:
        clean = re.sub(r"[。！？\s]+$", "", s)
        end_type = "その他"
        if clean.endswith("です"):
            end_type = "です"
        elif clean.endswith("ます"):
            end_type = "ます"
        elif clean.endswith("でした"):
            end_type = "でした"
        elif clean.endswith("ました"):
            end_type = "ました"
        elif clean.endswith("である"):
            end_type = "である"
        elif clean.endswith("だ"):
            end_type = "だ"
        elif clean.endswith("だろう"):
            end_type = "だろう"
        end_types.append((line_no, s, end_type))

    # 3連続チェック
    count = 1
    for i in range(1, len(end_types)):
        prev_line, prev_s, prev_type = end_types[i - 1]
        curr_line, curr_s, curr_type = end_types[i]

        if curr_type != "その他" and curr_type == prev_type:
            count += 1
            if count == 3:
                findings.append({
                    "rule": "sentence_end_repetition",
                    "line": curr_line,
                    "severity": "warn",
                    "message": f"同一文末「{curr_type}」が3回以上連続しています。文末のリズムを調整してください。",
                    "snippet": curr_s
                })
        else:
            count = 1

    return findings


def analyze_markdown_metrics(text: str, dot_bullets: bool = False) -> Dict[str, Any]:
    """太字頻度、箇条書き比率などの構造メトリクスを算出（引用文やコードブロックは除外）"""
    lines = text.split("\n")
    plain_lines = []
    in_code = False
    fm_lines = get_frontmatter_line_count(lines)
    for idx, l in enumerate(lines, 1):
        if idx <= fm_lines:
            continue
        stripped = l.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or stripped.startswith(">") or stripped.startswith("|") or stripped.startswith("![") or stripped.startswith("[![") or stripped.startswith("<"):
            continue
        plain_lines.append(l)

    total_lines = len([l for l in plain_lines if l.strip()])
    list_lines = 0
    for l in plain_lines:
        if re.match(r"^\s*([-*+]|\d+\.)\s+", l) or (dot_bullets and l.strip().startswith("・")):
            # 外部参照リンク（- [タイトル](http...)）は並列データのため思考リストから除外
            if not re.search(r"[-*+]\s+\[.*?\]\(https?://", l):
                list_lines += 1

    plain_content = "\n".join(plain_lines)
    bold_matches = re.findall(r"\*\*[^*]+\*\*", plain_content)
    bold_count = len(bold_matches)
    char_count = len(re.sub(r"\s+", "", plain_content))

    bold_per_1000 = (bold_count / char_count * 1000) if char_count > 0 else 0
    list_ratio = (list_lines / total_lines) if total_lines > 0 else 0

    return {
        "char_count": char_count,
        "total_lines": total_lines,
        "list_lines": list_lines,
        "list_ratio": round(list_ratio, 3),
        "bold_count": bold_count,
        "bold_per_1000": round(bold_per_1000, 2),
    }


# ---- 太字が表示されるか（GitHub などの Markdown）----
# GitHub の Markdown では、** のすぐ内側が記号（「」（）` など）で、すぐ外側が文字だと、** を太字の印として読まず、
# ** がそのまま表示される。新しい CommonMark（記号に Unicode の S も入る）でも、GitHub の GFM（P だけ）でも
# 太字になる形だけを「表示される」とみなす。直し方の案は、かっこの内側だけを太字にする → 句読点を太字の外に出す
# → 文字に接する側に半角スペースを入れる、の順に試す。
BOLD_ASCII_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
BOLD_BRACKETS = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】", "〔": "〕", "［": "］", "[": "]",
                 "〈": "〉", "《": "》", "“": "”", "‘": "’", "＜": "＞"}


def _bold_ws(ch: str) -> bool:
    return ch == "" or ch.isspace()


def _bold_punct_gfm(ch: str) -> bool:
    return ch != "" and (ch in BOLD_ASCII_PUNCT or unicodedata.category(ch).startswith("P"))


def _bold_punct_new(ch: str) -> bool:
    return ch != "" and unicodedata.category(ch)[0] in "PS"


def _bold_can_open(prev: str, nxt: str) -> bool:
    return all(not _bold_ws(nxt) and (not p(nxt) or _bold_ws(prev) or p(prev)) for p in (_bold_punct_gfm, _bold_punct_new))


def _bold_can_close(prev: str, nxt: str) -> bool:
    return all(not _bold_ws(prev) and (not p(prev) or _bold_ws(nxt) or p(nxt)) for p in (_bold_punct_gfm, _bold_punct_new))


def _bold_code_spans(line: str):
    """インラインコード（同じ数のバッククォートで閉じたもの）の範囲"""
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", line)]
    spans, k = [], 0
    while k < len(runs):
        s, e = runs[k]
        for m in range(k + 1, len(runs)):
            if runs[m][1] - runs[m][0] == e - s:
                spans.append((s, runs[m][1]))
                k = m
                break
        k += 1
    return spans


def _bold_pairs(line: str):
    code = _bold_code_spans(line)
    pos = [m.start() for m in re.finditer(r"(?<![*\\])\*\*(?!\*)", line)
           if not any(a <= m.start() < b for a, b in code)]
    return [(pos[k], pos[k + 1]) for k in range(0, len(pos) - 1, 2)]


def _bold_pair_ok(line: str, i: int, j: int) -> bool:
    ch = lambda p: line[p] if 0 <= p < len(line) else ""
    return _bold_can_open(ch(i - 1), ch(i + 2)) and _bold_can_close(ch(j - 1), ch(j + 2))


def _bold_close_of(s: str) -> int:
    """s の先頭のかっこに対応する閉じかっこの位置（なければ -1）"""
    o, c, depth = s[0], BOLD_BRACKETS[s[0]], 0
    for k, x in enumerate(s):
        if x == o:
            depth += 1
        elif x == c:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _bold_fix(line: str, i: int, j: int, k: int):
    """k 番目の太字（i と j の **）の直し方の案。(直したあとの部分, 直し方) を返す"""
    inner = line[i + 2:j]
    tries = []
    if len(inner) >= 3 and inner[0] in BOLD_BRACKETS and _bold_close_of(inner) == len(inner) - 1:
        tries.append((inner[0] + "**" + inner[1:-1] + "**" + inner[-1], "かっこの内側だけを太字にする"))
    if len(inner) >= 2 and inner[-1] in "。、．，！？!?":
        tries.append(("**" + inner[:-1] + "**" + inner[-1], "句読点を太字の外に出す"))
    ch = lambda p: line[p] if 0 <= p < len(line) else ""
    body = inner
    if _bold_ws(ch(i + 2)) or _bold_ws(ch(j - 1)):
        body = inner.strip()
    left = "" if _bold_can_open(ch(i - 1), body[:1]) else " "
    right = "" if _bold_can_close(body[-1:], ch(j + 2)) else " "
    tries.append((left + "**" + body + "**" + right, "太字の内側の空白を取る" if body != inner and not (left or right)
                  else "文字に接する側に半角スペースを入れる"))
    for middle, how in tries:
        cand = line[:i] + middle + line[j + 2:]
        pairs = _bold_pairs(cand)
        if k < len(pairs) and _bold_pair_ok(cand, *pairs[k]):
            return middle, how
    return None, "手で直す"


def bold_problems(text: str, skip_frontmatter: bool = True):
    """太字にならない ** の場所と、直し方の案。コードブロック・インラインコード・HTML の行・先頭の設定部分は見ない"""
    out, fence = [], None
    lines = text.split("\n")
    start = 0
    if skip_frontmatter and lines and lines[0].strip() == "---":
        for n in range(1, len(lines)):
            if lines[n].strip() == "---":
                start = n + 1
                break
    for no in range(start, len(lines)):
        line = lines[no].rstrip("\r")
        m = re.match(r"\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
            continue
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        if line.lstrip().startswith("<"):
            continue
        for k, (i, j) in enumerate(_bold_pairs(line)):
            if _bold_pair_ok(line, i, j):
                continue
            middle, how = _bold_fix(line, i, j, k)
            pre, post = line[max(0, i - 4):i], line[j + 2:j + 6]
            found = pre + _bold_short(line[i:j + 2]) + post
            suggest = pre + _bold_short(middle) + post if middle is not None else ""
            out.append({"line": no + 1, "found": found, "suggest": suggest, "how": how})
    return out


def _bold_short(s: str) -> str:
    """長い太字は、直すところ（両端）だけを見せる"""
    return s if len(s) <= 30 else s[:12] + "…" + s[-12:]


# ---- 用途プロファイルの検査 ----

def _content_lines(text: str):
    """コードブロックと先頭の設定部分を除いた (行番号, 行) を順に返す"""
    lines = text.split("\n")
    fm_lines = get_frontmatter_line_count(lines)
    in_code = False
    for line_no, line in enumerate(lines, 1):
        if line_no <= fm_lines:
            continue
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue
        yield line_no, line


def _is_example_line(stripped: str) -> bool:
    """引用・表・画像・HTMLの行（悪い例の引用などが多いため語彙の検査から外す）"""
    return (stripped.startswith(">") or stripped.startswith("|") or stripped.startswith("![")
            or stripped.startswith("[![") or stripped.startswith("<"))


def _scan_text(line: str) -> str:
    """インラインコード、URL、見出し記号、太字記号を除いた検査用の文字列"""
    s = re.sub(r"`[^`]+`", "", line.strip())
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"^#{1,6}\s+", "", s)
    return re.sub(r"\*\*|__", "", s)


def _sentence_len(s: str) -> int:
    """文の字数（空白、句点、感嘆符、Markdownの記号を除く）"""
    return len(re.sub(r"[\s。！？!?]", "", re.sub(r"\*\*|`", "", s)))


LIST_ITEM_PATTERN = re.compile(r"^(\s*)([-*+]\s+|\d+\.\s+|・)(.*)$")


def _ending_kind(item: str) -> str:
    """箇条書き1項目の語尾の種類: polite（です・ます）/ verb（動詞・形容詞止め）/ noun（体言止め）"""
    s = re.sub(r"[\s。、．.！？!?）)」』]+$", "", re.sub(r"\*\*|`", "", item))
    if re.search(r"(です|ます|ました|でした|ません|ください)$", s):
        return "polite"
    if re.search(r"([うくぐすつぬぶむるたいだ]|ない)$", s):
        return "verb"
    return "noun"


def x_weighted_length(text: str) -> int:
    """Xの文字数の数え方（全角2・半角1、URLは23）で数える"""
    text = re.sub(r"https?://\S+", "x" * 23, text)
    n = 0
    for ch in text:
        o = ord(ch)
        if o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037:
            n += 1
        elif 0xFE00 <= o <= 0xFE0F or o == 0x200D:
            continue
        else:
            n += 2
    return n


def _x_segments(text: str) -> List[Tuple[int, str]]:
    """「---」だけの行で区切った投稿ごとの (開始行, 本文)"""
    segments, start, buf = [], 1, []
    for idx, line in enumerate(text.split("\n") + ["---"], 1):
        if line.strip() == "---":
            segments.append((start, "\n".join(buf).strip()))
            start, buf = idx + 1, []
        else:
            buf.append(line)
    return [seg for seg in segments if seg[1]]


def _list_items(text: str) -> List[Tuple[int, int, str]]:
    """箇条書きの (行番号, 字下げ, 本文)。「・」で始まる行も含む"""
    items = []
    for line_no, line in _content_lines(text):
        m = LIST_ITEM_PATTERN.match(line)
        if m:
            items.append((line_no, len(m.group(1).replace("\t", "    ")), m.group(3).strip()))
    return items


def register_metrics(text: str, sentences: List[Tuple[int, str]]) -> Dict[str, Any]:
    """文の長さの平均・ばらつき・最大、漢字の割合（ひらがな・カタカナ・漢字に占める漢字）、箇条書き、見出し、Xの文字数"""
    lengths = [_sentence_len(s) for _, s in sentences]
    n = len(lengths)
    mean = sum(lengths) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in lengths) / n) if n else 0.0
    jp = kanji = 0
    for _, line in _content_lines(text):
        for ch in _scan_text(line):
            is_kanji = "一" <= ch <= "鿿" or ch == "々"
            if is_kanji or "぀" <= ch <= "ヿ":
                jp += 1
                kanji += is_kanji
    item_lengths = [_sentence_len(item) for _, _, item in _list_items(text)]
    headings = [(no, re.sub(r"^#{1,6}\s+", "", line.strip())) for no, line in _content_lines(text)
                if re.match(r"^\s*#{1,6}\s", line)]
    return {
        "sentence_count": n,
        "sentence_len_mean": round(mean, 1),
        "sentence_len_sd": round(sd, 1),
        "sentence_len_max": max(lengths) if lengths else 0,
        "kanji_ratio": round(kanji / jp, 3) if jp else 0.0,
        "list_item_count": len(item_lengths),
        "list_item_len_mean": round(sum(item_lengths) / len(item_lengths), 1) if item_lengths else 0.0,
        "list_item_len_max": max(item_lengths) if item_lengths else 0,
        "heading_len_max": max((_sentence_len(h) for _, h in headings), default=0),
        "x_weighted_lengths": [x_weighted_length(seg) for _, seg in _x_segments(text)],
    }


def _finding(rule: str, line: int, severity: str, message: str, snippet: str) -> Dict[str, Any]:
    return {"rule": rule, "line": line, "severity": severity, "message": message, "snippet": snippet}


def check_profile(text: str, sentences: List[Tuple[int, str]], prof: Dict[str, Any],
                  reg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """用途プロファイルで追加される検査。(指摘, 空欄の一覧) を返す"""
    findings: List[Dict[str, Any]] = []

    # 文の長さ
    sl = prof.get("sentence_length")
    if sl and reg["sentence_count"] >= 3:
        rng = sl.get("mean")
        if rng and not (rng[0] <= reg["sentence_len_mean"] <= rng[1]):
            findings.append(_finding(
                "sentence_length_mean", 1, "info",
                f"文の平均の長さ（{reg['sentence_len_mean']}字）が、この用途の目安（{rng[0]}〜{rng[1]}字）から外れています。",
                f"文の数: {reg['sentence_count']}"))
        sd_min = sl.get("sd_min")
        if sd_min and reg["sentence_count"] >= 6 and reg["sentence_len_sd"] < sd_min:
            findings.append(_finding(
                "sentence_length_flat", 1, "info",
                f"文の長さがそろいすぎています（ばらつき {reg['sentence_len_sd']}）。同じ長さの文が続くと機械的に読めます。",
                f"平均 {reg['sentence_len_mean']}字"))
        mx = sl.get("max")
        if mx:
            for line_no, s in [(no, s) for no, s in sentences if _sentence_len(s) > mx][:3]:
                findings.append(_finding(
                    "sentence_too_long", line_no, "warn",
                    f"1文が{_sentence_len(s)}字あります（この用途の上限: {mx}字）。つながりを後ろの文のつなぎ言葉で残せるなら、2文に分けます。",
                    s))

    # 短い文の連打（AIの劇画調）。「速い。」のような3字の文も数えるため、文を取り直す
    if prof.get("short_burst"):
        run: List[Tuple[int, str]] = []
        for item in extract_plain_sentences(text, min_len=2, dot_bullets=True) + [(0, "x" * 20)]:
            if _sentence_len(item[1]) < 10:
                run.append(item)
                continue
            if len(run) >= 3:
                findings.append(_finding(
                    "short_burst", run[2][0], "warn",
                    "10字未満の短い文が3つ以上続いています。言い切りを重ねて勢いを出す書き方はAIっぽく見えやすいので、つなげられる文はつなげます。",
                    " ".join(s for _, s in run[:4])))
            run = []

    # 同じ主語のくり返し
    if prof.get("subject_repetition"):
        recent: List[Optional[str]] = []
        for line_no, s in sentences:
            m = SUBJECT_HEAD_PATTERN.match(s)
            subj = m.group(1) if m else None
            if subj and subj in recent:
                findings.append(_finding(
                    "subject_repetition", line_no, "info",
                    f"主語「{subj}」が近くの文でくり返されています。誰が動くかが変わらないなら、2回目以降は省いても通じます。",
                    s))
            recent = (recent + [subj])[-2:]

    # 箇条書き（1項目の長さと語尾のそろい方）
    blocks: List[List[Tuple[int, str]]] = []
    prev_no, prev_indent = -2, None
    for line_no, indent, item in _list_items(text):
        if line_no == prev_no + 1 and indent == prev_indent and blocks:
            blocks[-1].append((line_no, item))
        else:
            blocks.append([(line_no, item)])
        prev_no, prev_indent = line_no, indent
    if prof.get("list_item_max"):
        for block in blocks:
            for line_no, item in block:
                if _sentence_len(item) > prof["list_item_max"]:
                    findings.append(_finding(
                        "list_item_too_long", line_no, "info",
                        f"箇条書きの1項目が{_sentence_len(item)}字あります（目安: {prof['list_item_max']}字以内）。1項目1行に収まるよう削ります。",
                        item))
    if prof.get("bullet_endings"):
        for block in blocks:
            kinds = [(_ending_kind(item), line_no, item) for line_no, item in block]
            polite = [k for k in kinds if k[0] == "polite"]
            if polite:
                findings.append(_finding(
                    "bullet_polite", polite[0][1], "warn",
                    "資料の箇条書きで「です・ます」が使われています。常体（体言止めか動詞止め）にそろえます。",
                    polite[0][2]))
            plain = {k[0] for k in kinds if k[0] != "polite"}
            if len(block) >= 2 and len(plain) > 1:
                findings.append(_finding(
                    "bullet_mixed_endings", block[0][0], "info",
                    "同じ階層の箇条書きで、体言止めと動詞止めが混ざっています。どちらかにそろえます。",
                    " / ".join(item for _, item in block[:4])))

    # タイトル（見出し）の字数
    if prof.get("title_max"):
        for line_no, line in _content_lines(text):
            if re.match(r"^\s*#{1,6}\s", line):
                title = re.sub(r"^\s*#{1,6}\s+", "", line.strip())
                if _sentence_len(title) > prof["title_max"]:
                    findings.append(_finding(
                        "title_too_long", line_no, "info",
                        f"タイトルが{_sentence_len(title)}字あります（目安: {prof['title_max']}字以内）。スライドで2行に収まるよう、主張だけを残します。",
                        title))

    # 表示されないMarkdown
    md_sev = prof.get("markdown_unrendered", "off")
    if md_sev in ("warn", "info"):
        for line_no, line in _content_lines(text):
            s = line.strip()
            kind = None
            if re.match(r"^#{1,6}\s", s):
                kind = "見出し（#）"
            elif s.startswith("|"):
                kind = "表（|）"
            elif "**" in re.sub(r"`[^`]+`", "", s):
                kind = "太字（**）"
            if kind:
                findings.append(_finding(
                    "markdown_unrendered", line_no, md_sev,
                    f"この用途ではMarkdownの{kind}が表示されず、記号のまま出ることがあります。",
                    s))

    # ハッシュタグ
    if prof.get("hashtag_max") is not None:
        tags = [(no, t) for no, line in _content_lines(text) if not re.match(r"^\s*#{1,6}\s", line)
                for t in HASHTAG_PATTERN.findall(re.sub(r"https?://\S+", "", line))]
        if len(tags) > prof["hashtag_max"]:
            findings.append(_finding(
                "too_many_hashtags", tags[0][0], "warn",
                f"ハッシュタグが{len(tags)}個あります（この用途の上限: {prof['hashtag_max']}個）。",
                " ".join(t for _, t in tags)))

    # Xの文字数（「---」だけの行で区切った投稿ごと）
    xmax = prof.get("x_weighted_max")
    if xmax:
        for seg_start, seg in _x_segments(text):
            w = x_weighted_length(seg)
            if w > xmax:
                findings.append(_finding(
                    "x_length", seg_start, "warn",
                    f"Xの文字数の上限を超えています（全角換算で約{math.ceil(w / 2)}字 / 上限{xmax // 2}字）。削るか、投稿を分けます。LinkedInなど長文を投稿できる場では無視してかまいません。",
                    seg.split("\n")[0].strip()))

    # 硬さ辞書
    level = prof.get("stiffness_level", 0) or 0
    if level:
        repeat_counts: Dict[str, int] = {}
        for line_no, line in _content_lines(text):
            s = line.strip()
            if not s or _is_example_line(s):
                continue
            st = _scan_text(s)
            for lv, pat, found, plain in STIFFNESS_PATTERNS:
                if lv <= level and re.search(pat, st):
                    findings.append(_finding(
                        "stiff_expression", line_no, "warn" if lv == 1 else "info",
                        f"硬い言い方「{found}」があります。この用途では「{plain}」のような言い方のほうが読みやすくなります。専門用語や、言い切りの強さが変わる場合は残します。",
                        s))
            for lv, pat, found, plain in STIFFNESS_REPEAT_PATTERNS:
                if lv > level:
                    continue
                for _ in re.finditer(pat, st):
                    repeat_counts[found] = repeat_counts.get(found, 0) + 1
                    if repeat_counts[found] == 2:
                        findings.append(_finding(
                            "stiff_repetition", line_no, "warn",
                            f"「{found}」が1文書に2回以上出ています。2回目からは「{plain}」のような言い方にします。",
                            s))

    # 人間らしさスロップ
    caps = prof.get("humanize_slop")
    if caps is not None:
        hits: Dict[str, List[Tuple[int, str]]] = {pid: [] for pid, _, _ in HUMANIZE_SLOP_PATTERNS}
        for line_no, line in _content_lines(text):
            s = line.strip()
            if not s or _is_example_line(s):
                continue
            st = _scan_text(s)
            for pid, pat, _ in HUMANIZE_SLOP_PATTERNS:
                for _ in re.finditer(pat, st):
                    hits[pid].append((line_no, s))
        for pid, _, desc in HUMANIZE_SLOP_PATTERNS:
            cap = caps.get(pid, 0)
            found = hits[pid]
            if len(found) <= cap:
                continue
            line_no, s = found[cap]
            if pid == "fabricated":
                findings.append(_finding(
                    "humanize_slop", line_no, "info",
                    f"{desc}があります。元の文にない体験や感想は足さず、書き手の言葉が要るなら「[ここに一言]」のような空欄にします。",
                    s))
            else:
                findings.append(_finding(
                    "humanize_slop", line_no, "warn",
                    f"{desc}が{len(found)}回あります（この用途の上限: {cap}回）。人が書いたように見せる演出として目立ちやすいので減らします。",
                    s))

    # 書き手に埋めてもらう空欄
    placeholders = [(no, m.group(0)) for no, line in _content_lines(text) for m in PLACEHOLDER_PATTERN.finditer(line)]
    pmax = prof.get("placeholder_max")
    if pmax is not None and len(placeholders) > pmax:
        findings.append(_finding(
            "too_many_placeholders", placeholders[0][0], "warn",
            f"書き手に埋めてもらう空欄が{len(placeholders)}か所あります（この用途の上限: {pmax}か所）。書き手の言葉が本当に要る場所だけに絞ります。",
            " ".join(p for _, p in placeholders)))

    return findings, placeholders


def lint_text(text: str, profile: Union[str, Dict[str, Any]] = "default",
              profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """文章全体を総合検査する。profile に用途プロファイル名（または設定の辞書）を渡すと、その用途の基準で検査する"""
    if isinstance(profile, dict):
        profile_name, prof = profile.get("name", "custom"), dict(BASE_PROFILE, **profile)
    else:
        profiles = profiles or load_profiles()
        if profile not in profiles:
            raise ValueError(f"プロファイル「{profile}」はありません。使えるもの: {', '.join(profiles)}")
        profile_name, prof = profile, profiles[profile]
    is_default = profile_name == "default"

    findings = []
    metrics = analyze_markdown_metrics(text, dot_bullets=not is_default)
    sentences = extract_plain_sentences(text, dot_bullets=not is_default)

    # 1. メトリクス異常の検査（地の文が十分ある場合に適用）
    if metrics["char_count"] > 300:
        bold_warn = prof.get("bold_per_1000_warn")
        if bold_warn is not None and metrics["bold_per_1000"] > bold_warn:
            findings.append({
                "rule": "excess_bold",
                "line": 1,
                "severity": "warn",
                "message": (f"太字の頻度（1,000字あたり {metrics['bold_per_1000']}個）が高すぎます（推奨: 2.5以下）。重要な要点のみに絞ってください。"
                            if is_default else
                            f"太字の頻度（1,000字あたり {metrics['bold_per_1000']}個）が、この用途の警告ライン（{bold_warn}）を超えています。重要な要点のみに絞ってください。"),
                "snippet": f"太字数: {metrics['bold_count']}回 / {metrics['char_count']}文字"
            })

        list_warn = prof.get("list_ratio_warn")
        if list_warn is not None and metrics["list_ratio"] > list_warn:
            findings.append({
                "rule": "excess_list",
                "line": 1,
                "severity": "warn",
                "message": (f"箇条書きの比率（{round(metrics['list_ratio']*100, 1)}%）が高すぎます（推奨: 20%以下）。思考や論理展開は地の文で記述してください。"
                            if is_default else
                            f"箇条書きの比率（{round(metrics['list_ratio']*100, 1)}%）が、この用途の警告ライン（{round(list_warn*100)}%）を超えています。思考や論理展開は地の文で記述してください。"),
                "snippet": f"リスト行: {metrics['list_lines']} / 全非空行: {metrics['total_lines']}"
            })

    # 2. 文末重複検査
    if prof.get("sentence_end_repetition", True):
        findings.extend(check_sentence_end_repetitions(sentences))

    # 2.5 太字が表示されるか（GitHub などの Markdown で ** がそのまま出るところ）
    for p in bold_problems(text):
        findings.append({
            "rule": "bold_not_rendered",
            "line": p["line"],
            "severity": "error",
            "message": f"太字の印（**）が記号に接していて、GitHub などでは太字にならず ** がそのまま表示されます。直し方: {p['how']}。",
            "snippet": f"{p['found']} → {p['suggest']}" if p["suggest"] else p["found"]
        })

    # 3. 語彙・構文パターン検査
    lines = text.split("\n")
    in_code = False
    fm_lines = get_frontmatter_line_count(lines)
    emoji_max = prof.get("emoji_max", 0) or 0
    emoji_allowed = prof.get("emoji_allowed")
    if emoji_allowed is not None:
        emoji_allowed = {e.replace("️", "") for e in emoji_allowed}
    emoji_seen = 0
    for line_no, line in enumerate(lines, 1):
        if line_no <= fm_lines:
            continue
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code:
            continue

        # 絵文字検知（プロファイルなしでは見出し・本文問わず禁止。用途プロファイルでは上限まで使える）
        emoji_matches = EMOJI_PATTERN.findall(line)
        if emoji_matches and emoji_max == 0 and emoji_allowed is None:
            findings.append({
                "rule": "emoji_prohibited",
                "line": line_no,
                "severity": "warn",
                "message": f"絵文字（{' '.join(emoji_matches[:3])}）が検出されました。AI特有の装飾を排し、平文で記述してください。",
                "snippet": line.strip()
            })
        elif emoji_matches:
            before = emoji_seen
            emoji_seen += len(emoji_matches)
            if emoji_seen > emoji_max:
                over = emoji_matches[max(0, emoji_max - before):]
                findings.append({
                    "rule": "emoji_prohibited",
                    "line": line_no,
                    "severity": "warn",
                    "message": f"絵文字（{' '.join(over[:3])}）が、この用途の上限（1文書に{emoji_max}個）を超えています。",
                    "snippet": line.strip()
                })
            if emoji_allowed is not None:
                odd = [e for e in emoji_matches if e not in emoji_allowed]
                if odd:
                    findings.append({
                        "rule": "emoji_not_allowed",
                        "line": line_no,
                        "severity": "info",
                        "message": f"絵文字（{' '.join(odd[:3])}）は、この用途で使う絵文字（{' '.join(sorted(emoji_allowed))}）に入っていません。意味を補う絵文字だけを使います。",
                        "snippet": line.strip()
                    })

        # 見出し行の余計な言い換え補足カッコ検知
        if stripped.startswith("#"):
            if re.search(r"（(素の出力|いわゆる|概要|詳細|感謝と設計への反映)）", stripped):
                findings.append({
                    "rule": "redundant_bracket",
                    "line": line_no,
                    "severity": "warn",
                    "message": "見出しに情報量の増えない補足カッコが含まれています。平文で簡潔に記述してください。",
                    "snippet": line.strip()
                })
            continue

        # 引用ブロック（>）やテーブル行（|）、画像、HTMLタグはアンチパターン例示等の可能性が高いため語彙スキャンをスキップ
        if stripped.startswith(">") or stripped.startswith("|") or stripped.startswith("![") or stripped.startswith("[![") or stripped.startswith("<"):
            continue

        # インラインコード（`...`）を除去したテキストを作成
        scan_text = re.sub(r"`[^`]+`", "", stripped)
        # 太字や強調などの装飾記号（**、*、__）を除去した正規化テキストで語彙・比喩を検査
        plain_text = re.sub(r"\*\*|\*|__", "", scan_text)

        # 和欧文間の不自然な半角空白検知（例: 「も yomiyasu で」「この README は」）
        if re.search(r"([ぁ-んァ-ヶ一-龥])\s+([a-zA-Z0-9_-]{2,})\s+([ぁ-ん])", scan_text):
            # リンク構文 [text](url) の一部でないことを確認
            if not re.search(r"\[.*?\]\(.*?\)", scan_text):
                findings.append({
                    "rule": "unnatural_halfwidth_space",
                    "line": line_no,
                    "severity": "warn",
                    "message": "英単語の前後に不要な半角空白が空けられています。日本語の助詞と自然に接続させてください。",
                    "snippet": line.strip()
                })

        # 文末コロン（全角「：」または半角「:」）検知
        colon_sev = prof.get("trailing_colon", "warn")
        if colon_sev != "off" and re.search(r"[：:]$", scan_text) and not scan_text.startswith("http"):
            findings.append({
                "rule": "trailing_colon",
                "line": line_no,
                "severity": colon_sev,
                "message": "文末にコロン（：）が使われています。英語直訳の記法を避け、平文の句点（。）で終えるか前置きを省いてください。",
                "snippet": line.strip()
            })

        # スロップ語彙
        for word in SLOP_WORDS:
            if word in plain_text:
                findings.append({
                    "rule": "slop_vocabulary",
                    "line": line_no,
                    "severity": "warn",
                    "message": f"AI頻出語彙「{word}」が含まれています。文脈上必要のない比喩や大げさな装飾であれば、ふだん使う自然な表現に置き換えてください。ただし、文字どおりの意味や必要な文脈を担っている場合は残してかまいません。",
                    "snippet": line.strip()
                })

        # 比喩動詞パターン
        kowareru_span = None
        for pattern, desc in METAPHOR_VERB_PATTERNS:
            if desc == "英語直訳「静かに壊れる (silently fail)」" and kowareru_span:
                # 「壊れる」と「静かに壊れる」が同一動詞に二重反応することを防止
                for m in re.finditer(pattern, plain_text):
                    span = (m.start(), m.end())
                    if kowareru_span[0] <= span[0] and span[1] <= kowareru_span[1]:
                        continue
                    findings.append({
                        "rule": "metaphor_verb",
                        "line": line_no,
                        "severity": "warn",
                        "message": f"{desc}が検出されました。不自然な比喩動詞であれば、ふだん使う動詞や客観的な表現に書き直してください。ただし、文字どおりの動作や状態変化を表している場合は無理に言い換える必要はありません。",
                        "snippet": line.strip()
                    })
                    break
                continue

            m = re.search(pattern, plain_text)
            if m:
                if desc == "比喩動詞「壊れる」":
                    kowareru_span = (m.start(), m.end())
                findings.append({
                    "rule": "metaphor_verb",
                    "line": line_no,
                    "severity": "warn",
                    "message": f"{desc}が検出されました。不自然な比喩動詞であれば、ふだん使う動詞や客観的な表現に書き直してください。ただし、文字どおりの動作や状態変化を表している場合は無理に言い換える必要はありません。",
                    "snippet": line.strip()
                })

        # フィラーパターン
        for pattern, desc in FILLER_PATTERNS:
            if re.search(pattern, plain_text):
                findings.append({
                    "rule": "meta_filler",
                    "line": line_no,
                    "severity": "warn",
                    "message": f"{desc}が検出されました。単なる前置きや不要な飾りであれば削り、本題から書いてください。ただし、「何が大事か」という評価や主張そのものを担っている場合は、述語に移すなどして意味を残してください。",
                    "snippet": line.strip()
                })

        # ネガティブパラレリズム
        if NEGATIVE_PARALLELISM_PATTERN.search(plain_text):
            if "ではなく、" in plain_text or "ではなく" in plain_text:
                findings.append({
                    "rule": "negative_parallelism",
                    "line": line_no,
                    "severity": "info",
                    "message": "「AではなくB」構文が検出されました。否定を外しても主張が変わらない場合は肯定文を検討してください。ただし、誤解の訂正や見方の切り替えなど意味・比重を担っている否定なら、無理に肯定化せずそのまま残してください。",
                    "snippet": line.strip()
                })

    # 4. 用途プロファイルの検査（文の長さ、硬さ、人間らしさスロップ、記号の扱いなど）
    register = None
    placeholders: List[Tuple[int, str]] = []
    if not is_default:
        register = register_metrics(text, sentences)
        extra, placeholders = check_profile(text, sentences, prof, register)
        findings.extend(extra)

    # スコア計算（100点満点からの減点方式: warn=5点, info=2点）
    penalty = sum(5 if f["severity"] in ("warn", "error") else 2 for f in findings)
    score = max(0, 100 - penalty)

    result = {
        "score": score,
        "is_clean": len(findings) == 0,
        "profile": profile_name,
        "metrics": metrics,
        "findings": findings
    }
    if register is not None:
        result["register_metrics"] = register
        result["placeholders"] = [{"line": no, "text": p} for no, p in placeholders]
    return result


def main():
    parser = argparse.ArgumentParser(description="日本語文章のAIっぽさ数値化リンター")
    parser.add_argument("file", nargs="?", help="検査対象のMarkdownファイルパス（指定なしの場合は標準入力）")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    parser.add_argument("--strict", action="store_true", help="警告が1件でもあれば非ゼロ（終了コード1）で終了")
    parser.add_argument("--profile", default="default",
                        help="用途プロファイル（proposal / article / sns / chat / chat_external など。指定なしは従来どおりの検査）")
    parser.add_argument("--profiles-file", help="自分で定義したプロファイルのJSON（同梱の profiles.json に追加・上書きする）")
    parser.add_argument("--list-profiles", action="store_true", help="使えるプロファイルの一覧を表示して終了")

    args = parser.parse_args()

    try:
        profiles = load_profiles(args.profiles_file)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Error loading profiles: {e}", file=sys.stderr)
        sys.exit(2)

    if args.list_profiles:
        for name, conf in profiles.items():
            print(f"{name}: {conf.get('description', '')}")
        sys.exit(0)

    if args.profile not in profiles:
        print(f"Error: プロファイル「{args.profile}」はありません。使えるもの: {', '.join(profiles)}", file=sys.stderr)
        sys.exit(2)

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"Error opening file {args.file}: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        content = sys.stdin.read()

    result = lint_text(content, args.profile, profiles)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.profile == "default":
        print("=" * 60)
        print(f"AIっぽさ 検査レポート (スコア: {result['score']}/100)")
        print("=" * 60)
        m = result["metrics"]
        print(f"・文字数: {m['char_count']} | 行数: {m['total_lines']}")
        print(f"・太字頻度: 1,000字あたり {m['bold_per_1000']} 個 (推奨: 2.0以下 / 警告: 3.0超)")
        print(f"・箇条書き比率: {round(m['list_ratio']*100, 1)}% (推奨: 15%以下 / 警告: 25%超)")
        print("-" * 60)
    else:
        prof = profiles[args.profile]
        print("=" * 60)
        print(f"AIっぽさ 検査レポート [{args.profile}: {prof.get('description', '')}] (スコア: {result['score']}/100)")
        print("=" * 60)
        m, r = result["metrics"], result["register_metrics"]
        print(f"・文字数: {m['char_count']} | 行数: {m['total_lines']}")
        sl = prof.get("sentence_length") or {}
        rng = sl.get("mean")
        if r["sentence_count"]:
            print(f"・文の長さ: 平均 {r['sentence_len_mean']}字 / ばらつき {r['sentence_len_sd']} / 最大 {r['sentence_len_max']}字"
                  + (f" (目安: 平均{rng[0]}〜{rng[1]}字)" if rng else ""))
        if r["list_item_count"]:
            print(f"・箇条書き: {r['list_item_count']}項目 / 平均 {r['list_item_len_mean']}字 / 最大 {r['list_item_len_max']}字"
                  + (f" (目安: 1項目{prof['list_item_max']}字以内)" if prof.get("list_item_max") else ""))
        if prof.get("title_max") and r["heading_len_max"]:
            print(f"・タイトル: 最大 {r['heading_len_max']}字 (目安: {prof['title_max']}字以内)")
        if prof.get("x_weighted_max"):
            print("・Xの文字数: " + " / ".join(f"全角換算 約{math.ceil(w / 2)}字" for w in r["x_weighted_lengths"])
                  + f" (上限: {prof['x_weighted_max'] // 2}字。URLは1本で約12字)")
        print(f"・漢字の割合: {round(r['kanji_ratio']*100, 1)}%")
        bw, lw = prof.get("bold_per_1000_warn"), prof.get("list_ratio_warn")
        print(f"・太字頻度: 1,000字あたり {m['bold_per_1000']} 個" + (f" (警告: {bw}超)" if bw is not None else " (この用途では検査しない)"))
        print(f"・箇条書き比率: {round(m['list_ratio']*100, 1)}%" + (f" (警告: {round(lw*100)}%超)" if lw is not None else " (この用途では検査しない)"))
        if result["placeholders"]:
            print(f"・書き手に埋めてもらう空欄: {len(result['placeholders'])}か所 ("
                  + ", ".join(f"L{p['line']} {p['text']}" for p in result["placeholders"]) + ")")
        print("-" * 60)

    if not args.json:
        if result["is_clean"]:
            print("[PASS] 設定された検査ルールによる指摘はありません。")
        else:
            print(f"[NOTICE] {len(result['findings'])} 件の改善推奨箇所が見つかりました。\n")
            for f in result["findings"]:
                sev = f"[{f['severity'].upper()}]"
                print(f"L{f['line']} {sev} {f['message']}")
                print(f"  > {f['snippet']}\n")

    if args.strict:
        warn_count = sum(1 for f in result["findings"] if f["severity"] in ("warn", "error"))
        if warn_count > 0:
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
