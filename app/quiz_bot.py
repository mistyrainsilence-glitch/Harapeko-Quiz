"""
Discord クイズBot
- 出題チャンネルに問題を投稿し、「回答する」ボタン → 入力フォームで回答（他の人には見えない）
- 締切後、自動で正誤判定して集計チャンネル（運営のみ閲覧可）に正解者一覧を投稿
- 全問終了後、正解数ランキングを集計

必要: Python 3.10+ / discord.py 2.7+

フォルダ構成（このファイルは app フォルダに置く）:
  (ルート)/start.bat, README.md
  (ルート)/app/quiz_bot.py, requirements.txt
  (ルート)/data/quiz.db          … 自動で作成
  (ルート)/samples/questions_sample.xml など

環境変数:
  DISCORD_TOKEN      Botのトークン
  GUILD_ID           サーバーID
  RESULT_CHANNEL_ID  集計結果を投稿するチャンネルID（運営だけが見られるチャンネル）
  STAFF_ROLE         運営ロール名（省略時「クイズ運営」）
"""
import asyncio
import json
import os
import socket
import sqlite3
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET

import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
RESULT_CHANNEL_ID = int(os.environ["RESULT_CHANNEL_ID"])
STAFF_ROLE = os.getenv("STAFF_ROLE", "クイズ運営")
# フォルダの場所（このファイルの1つ上がルートフォルダ）
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
DB_PATH = os.getenv("DB_PATH") or os.path.join(DATA_DIR, "quiz.db")


def migrate_old_db():
    """以前の構成（ルートフォルダや app フォルダに quiz.db）から data フォルダへ移動する"""
    if os.getenv("DB_PATH"):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        return
    for old in (os.path.join(ROOT_DIR, "quiz.db"), os.path.join(APP_DIR, "quiz.db")):
        if os.path.exists(old):
            os.replace(old, DB_PATH)
            print(f"データファイルを移動しました: {old} → {DB_PATH}")
            return


migrate_old_db()
LOCK_PORT = int(os.getenv("LOCK_PORT", "47823"))  # 二重起動の検出に使うポート番号

NO_MENTIONS = discord.AllowedMentions.none()

# ───────────────────────── データベース ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS questions(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  number      INTEGER UNIQUE NOT NULL,   -- 第n問
  text        TEXT NOT NULL,
  answers     TEXT NOT NULL,             -- 正解のJSON配列（複数可）
  time_limit  INTEGER NOT NULL,          -- 回答時間（秒）
  status      TEXT NOT NULL,             -- pending / open / closed
  channel_id  INTEGER,
  message_id  INTEGER,
  asked_at    REAL,
  deadline    REAL
);
CREATE TABLE IF NOT EXISTS responses(
  question_id INTEGER NOT NULL,
  user_id     INTEGER NOT NULL,
  user_name   TEXT NOT NULL,
  answer      TEXT NOT NULL,
  answered_at REAL NOT NULL,
  is_correct  INTEGER,                   -- 1=正解 0=不正解 NULL=未判定
  PRIMARY KEY(question_id, user_id)
);
""")
# 問題形式の列を追加（以前のバージョンで作った quiz.db もそのまま使えるように）
_cols = {r["name"] for r in db.execute("PRAGMA table_info(questions)")}
if "qtype" not in _cols:
    db.execute("ALTER TABLE questions ADD COLUMN qtype TEXT NOT NULL DEFAULT 'text'")  # text / ox / choice
if "choices" not in _cols:
    db.execute("ALTER TABLE questions ADD COLUMN choices TEXT")  # 4択の選択肢（JSON配列）
if "note" not in _cols:
    db.execute("ALTER TABLE questions ADD COLUMN note TEXT")  # 正解発表のときの補足
if "revealed" not in _cols:
    db.execute("ALTER TABLE questions ADD COLUMN revealed INTEGER NOT NULL DEFAULT 0")  # 正解発表済み
if "closed_at" not in _cols:
    db.execute("ALTER TABLE questions ADD COLUMN closed_at REAL")  # 締め切った時刻
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")  # Botの設定
db.commit()

db.execute("""CREATE TABLE IF NOT EXISTS panels(
  message_id      INTEGER PRIMARY KEY,   -- 操作パネルのメッセージ
  channel_id      INTEGER NOT NULL,      -- 操作パネルを置いたチャンネル
  quiz_channel_id INTEGER NOT NULL       -- 出題先のチャンネル
)""")
db.commit()

CHOICE_LABELS = ["A", "B", "C", "D"]
OX_LABELS = ["○", "×"]


def normalize(s: str) -> str:
    """表記ゆれ吸収: 全角半角・大文字小文字・空白・カタカナ/ひらがなの違いを無視"""
    s = unicodedata.normalize("NFKC", s).lower()
    s = "".join(s.split())
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def parse_answers(s: str) -> list[str]:
    return [a.strip() for a in s.split("|") if a.strip()]


def to_ox(s: str) -> str:
    """○×の表記ゆれを統一（o / O / 〇 / まる → ○、x / X / ✕ / ばつ → ×）"""
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    if s in ("○", "〇", "o", "◯", "まる", "maru", "true"):
        return "○"
    if s in ("×", "x", "✕", "✖", "ばつ", "batsu", "false"):
        return "×"
    raise ValueError(f"○×の正解は ○ か × で指定してください: {s}")


def get_choices(q) -> list[str]:
    return json.loads(q["choices"]) if q["choices"] else []


def describe(q, label: str) -> str:
    """回答・正解の表示用。4択なら『B：選択肢の文』の形にする"""
    if q["qtype"] == "choice":
        choices = get_choices(q)
        if label in CHOICE_LABELS and CHOICE_LABELS.index(label) < len(choices):
            return f"{label}：{choices[CHOICE_LABELS.index(label)]}"
    return label


def save_response(qid: int, user, answer: str):
    db.execute(
        """INSERT INTO responses(question_id, user_id, user_name, answer, answered_at)
           VALUES(?, ?, ?, ?, ?)
           ON CONFLICT(question_id, user_id) DO UPDATE SET
             answer=excluded.answer, answered_at=excluded.answered_at,
             user_name=excluded.user_name""",
        (qid, user.id, user.display_name, answer, time.time()))
    db.commit()


def is_accepting(q) -> bool:
    return q is not None and q["status"] == "open" and time.time() <= q["deadline"]


def insert_question(text: str, answers: list[str], seconds: int,
                    qtype: str = "text", choices: list[str] | None = None,
                    number: int | None = None, note: str | None = None) -> tuple[int, int]:
    n = number or next_number()
    cur = db.execute(
        "INSERT INTO questions(number, text, answers, time_limit, status, qtype, choices, note) "
        "VALUES(?,?,?,?,'pending',?,?,?)",
        (n, text, json.dumps(answers, ensure_ascii=False), seconds, qtype,
         json.dumps(choices, ensure_ascii=False) if choices else None, note or None))
    return cur.lastrowid, n


def get_setting(key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str | None):
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def auto_reveal_seconds() -> int | None:
    """オート正解発表の秒数（OFFなら None）"""
    v = get_setting("auto_reveal")
    return int(v) if v not in (None, "") else None


def is_staff(user) -> bool:
    return isinstance(user, discord.Member) and any(r.name == STAFF_ROLE for r in user.roles)


def open_questions():
    return db.execute("SELECT * FROM questions WHERE status='open' ORDER BY number").fetchall()


MAX_FILE_BYTES = 1_000_000  # 問題リストは1MBまで


def clean_text(s: str | None) -> str:
    """XMLの字下げ（行頭の空白）を取り除く。改行はそのまま残す"""
    return "\n".join(line.strip() for line in (s or "").strip().splitlines())


def parse_xml(raw: bytes) -> list[dict]:
    """XMLの問題リストを読み込み、JSONと同じ形のリストにする"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"XMLの書き方に誤りがあります（{e.position[0]}行目付近）。"
                         "タグの閉じ忘れや、& < > の書き方を確認してください")
    items = []
    for i, q in enumerate(root.iter("question"), 1):
        qtype = q.get("type", "text")
        text = clean_text(q.findtext("text"))
        if not text:
            raise ValueError(f"{i}問目: <text>（問題文）がありません")
        answers = [clean_text(a.text) for a in q.findall("answer") if clean_text(a.text)]
        if not answers:
            raise ValueError(f"{i}問目: <answer>（正解）がありません")
        item = {"type": qtype, "question": text, "answers": answers}
        choices = [clean_text(c.text) for c in q.findall("choice") if clean_text(c.text)]
        if qtype == "choice":
            if not choices:
                raise ValueError(f"{i}問目: 選択式には <choice>（選択肢）が必要です")
            item["choices"] = choices
        seconds = (q.get("seconds") or "").strip()
        if seconds:
            if not seconds.isdigit() or not 5 <= int(seconds) <= 3600:
                raise ValueError(f"{i}問目: seconds は 5〜3600 の数字で指定してください")
            item["seconds"] = int(seconds)
        note = clean_text(q.findtext("note"))
        if note:
            item["note"] = note
        items.append(item)
    if not items:
        raise ValueError("<question> が1つもありません")
    return items


def parse_question_file(filename: str, raw: bytes) -> list[dict]:
    """問題リストのファイル（XML または JSON）を読み込む"""
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("ファイルが大きすぎます（1MBまで）")
    head = raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if filename.lower().endswith(".xml") or head == b"<":
        return parse_xml(raw)
    return json.loads(raw.decode("utf-8-sig"))


def load_questions(data) -> tuple[int, int]:
    """問題リスト（JSONを読み込んだもの）を登録し、(最初の問題番号, 問題数) を返す"""
    if not isinstance(data, list) or not data:
        raise ValueError("問題が1問も入っていません")
    n = next_number()
    try:
        for i, item in enumerate(data):
            qtype = item.get("type", "text")
            answers = item["answers"]
            answers = parse_answers(answers) if isinstance(answers, str) else list(answers)
            choices = None
            if qtype == "ox":
                answers, choices = [to_ox(a) for a in answers], OX_LABELS
            elif qtype == "choice":
                choices = item["choices"]
                answers = [str(a).strip().upper() for a in answers]
                if not 2 <= len(choices) <= 4 or \
                        any(a not in CHOICE_LABELS[:len(choices)] for a in answers):
                    raise ValueError(f"{i + 1}問目: 選択肢は2〜4個、正解は A〜D で指定してください")
            elif qtype != "text":
                raise ValueError(f"{i + 1}問目: type は text / ox / choice のいずれかです")
            default_sec = 60 if qtype == "text" else 30
            insert_question(item["question"], answers, int(item.get("seconds", default_sec)),
                            qtype, choices, number=n + i, note=item.get("note"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return n, len(data)


def do_reset():
    for task in bot.timers.values():
        task.cancel()
    bot.timers.clear()
    cancel_auto_reveals()
    stop_auto_ask()
    db.execute("DELETE FROM responses")
    db.execute("DELETE FROM questions")
    db.commit()


def q_by_id(qid: int):
    return db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()


def q_by_number(n: int):
    return db.execute("SELECT * FROM questions WHERE number=?", (n,)).fetchone()


def next_number() -> int:
    return db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM questions").fetchone()[0]


def next_pending():
    return db.execute(
        "SELECT * FROM questions WHERE status='pending' ORDER BY number LIMIT 1"
    ).fetchone()


async def send_long(channel, text: str):
    """2000文字制限を超えないよう分割して送信"""
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 1900:
            await channel.send(buf, allowed_mentions=NO_MENTIONS)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        await channel.send(buf, allowed_mentions=NO_MENTIONS)


async def get_channel(cid: int):
    return bot.get_channel(cid) or await bot.fetch_channel(cid)


# ───────────────────────── 回答ボタン & 入力フォーム ─────────────────────────
class AnswerModal(discord.ui.Modal):
    def __init__(self, qid: int, number: int):
        super().__init__(title=f"第{number}問 回答")
        self.qid = qid
        self.number = number
        self.answer = discord.ui.TextInput(label="回答", max_length=200)
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_accepting(q_by_id(self.qid)):
            await interaction.response.send_message(
                "締切を過ぎたため、回答は受け付けられませんでした。", ephemeral=True)
            return
        ans = self.answer.value.strip()
        save_response(self.qid, interaction.user, ans)
        await interaction.response.send_message(
            f"第{self.number}問の回答「{discord.utils.escape_markdown(ans)}」を受け付けました。\n"
            "（締切までは、もう一度ボタンを押すと回答を変更できます）",
            ephemeral=True)


class AnswerButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"quiz_answer:(?P<qid>[0-9]+)"):
    """Bot再起動後もボタンが動くよう DynamicItem を使用"""

    def __init__(self, qid: int):
        super().__init__(discord.ui.Button(
            label="回答する", emoji="✏️", style=discord.ButtonStyle.primary,
            custom_id=f"quiz_answer:{qid}"))
        self.qid = qid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["qid"]))

    async def callback(self, interaction: discord.Interaction):
        q = q_by_id(self.qid)
        if q is None or q["status"] != "open":
            await interaction.response.send_message(
                "この問題の回答受付は終了しています。", ephemeral=True)
            return
        await interaction.response.send_modal(AnswerModal(self.qid, q["number"]))


class ChoiceButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"quiz_choice:(?P<qid>[0-9]+):(?P<idx>[0-9])"):
    """○×・4択用のボタン。押すとその選択肢で回答（他の人には見えない）"""

    def __init__(self, qid: int, idx: int, qtype: str = "choice", text: str = ""):
        if qtype == "ox":
            label = OX_LABELS[idx]
            style = discord.ButtonStyle.success if idx == 0 else discord.ButtonStyle.danger
        else:
            label = f"{CHOICE_LABELS[idx]}：{text}"[:80] if text else CHOICE_LABELS[idx]
            style = discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label, style=style,
                                           custom_id=f"quiz_choice:{qid}:{idx}"))
        self.qid = qid
        self.idx = idx

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["qid"]), int(match["idx"]))

    async def callback(self, interaction: discord.Interaction):
        q = q_by_id(self.qid)
        if not is_accepting(q):
            await interaction.response.send_message(
                "この問題の回答受付は終了しています。", ephemeral=True)
            return
        label = OX_LABELS[self.idx] if q["qtype"] == "ox" else CHOICE_LABELS[self.idx]
        save_response(self.qid, interaction.user, label)
        await interaction.response.send_message(
            f"第{q['number']}問の回答「{describe(q, label)}」を受け付けました。\n"
            "（締切までは、別のボタンを押すと回答を変更できます）",
            ephemeral=True)


# ───────────────────────── 出題・締切・集計 ─────────────────────────
def schedule_close(qid: int, deadline: float):
    if qid in bot.timers:
        return

    async def runner():
        await asyncio.sleep(max(0, deadline - time.time()))
        await close_question(qid)

    bot.timers[qid] = asyncio.create_task(runner())


async def open_question(qid: int, channel, by: str | None = None) -> float:
    q = q_by_id(qid)
    now = time.time()
    deadline = now + q["time_limit"]
    embed = discord.Embed(title=f"第{q['number']}問", description=q["text"],
                          color=discord.Color.blue())
    embed.add_field(name="締切", value=f"<t:{int(deadline)}:R>（<t:{int(deadline)}:T>）")
    view = discord.ui.View(timeout=None)
    if q["qtype"] == "ox":
        embed.title += "（○×問題）"
        for i in range(2):
            view.add_item(ChoiceButton(qid, i, "ox"))
        how = "○ か × のボタンを押して回答してください。"
    elif q["qtype"] == "choice":
        embed.title += "（4択問題）" if len(get_choices(q)) == 4 else "（選択問題）"
        for i, text in enumerate(get_choices(q)):
            embed.add_field(name=CHOICE_LABELS[i], value=text, inline=False)
            view.add_item(ChoiceButton(qid, i, "choice", text))
        how = "選択肢のボタンを押して回答してください。"
    else:
        view.add_item(AnswerButton(qid))
        how = "「回答する」ボタンから回答してください。"
    embed.set_footer(text=how + "回答は他の参加者には見えません。")
    msg = await channel.send(embed=embed, view=view)
    db.execute(
        """UPDATE questions SET status='open', channel_id=?, message_id=?,
           asked_at=?, deadline=? WHERE id=?""",
        (channel.id, msg.id, now, deadline, qid))
    db.commit()
    schedule_close(qid, deadline)
    await refresh_panels()
    return deadline


async def close_question(qid: int, by: str | None = None):
    q = q_by_id(qid)
    if q is None or q["status"] != "open":
        return
    # 状態更新と自動判定（await を挟まないので二重実行されない）
    db.execute("UPDATE questions SET status='closed', closed_at=? WHERE id=?", (time.time(), qid))
    correct = {normalize(a) for a in json.loads(q["answers"])}
    rows = db.execute("SELECT user_id, answer FROM responses WHERE question_id=?",
                      (qid,)).fetchall()
    for r in rows:
        db.execute("UPDATE responses SET is_correct=? WHERE question_id=? AND user_id=?",
                   (int(normalize(r["answer"]) in correct), qid, r["user_id"]))
    db.commit()

    task = bot.timers.pop(qid, None)
    if task and task is not asyncio.current_task():
        task.cancel()

    # 出題メッセージを「締切済み」に更新
    try:
        ch = await get_channel(q["channel_id"])
        msg = await ch.fetch_message(q["message_id"])
        embed = msg.embeds[0]
        embed.color = discord.Color.dark_grey()
        embed.set_field_at(0, name="締切", value="締め切りました")
        await msg.edit(embed=embed, view=None)
        await ch.send(f"⏰ 第{q['number']}問の回答を締め切りました（回答 {len(rows)}人）")
    except discord.HTTPException:
        pass

    await post_result(q["number"])
    sec = auto_reveal_seconds()
    if sec is not None:
        schedule_auto_reveal(qid, time.time() + sec)
    await refresh_panels()


def set_note(qid: int, note: str | None):
    db.execute("UPDATE questions SET note=? WHERE id=?", ((note or "").strip() or None, qid))
    db.commit()


# 正解発表に表示する項目（設定ボタンで表示・非表示を切り替え）
REVEAL_ITEMS = {
    # key: (チェックボックスの名前, 説明)
    "question": ("問題文", None),
    "choices": ("選択肢", "選択式の問題のみ"),
    "answer": ("正解", None),
    "note": ("補足", "補足が登録されている問題のみ"),
    "counts": ("正解人数・回答人数", None),
    "breakdown": ("選択肢ごとの回答人数", "○×・選択式の問題のみ"),
    "correct_names": ("正解者の名前", "回答が早い順に表示"),
    "correct_time": ("正解者の回答時間", "正解者の名前と一緒に表示"),
    "wrong_names": ("不正解者の名前", None),
    "wrong_answers": ("不正解者の回答", "不正解者の名前と一緒に表示"),
}
DEFAULT_REVEAL_ITEMS = ["question", "choices", "answer", "note", "counts",
                        "correct_names", "correct_time"]


def reveal_items() -> set[str]:
    v = get_setting("reveal_items")
    return set(json.loads(v)) if v else set(DEFAULT_REVEAL_ITEMS)


def build_reveal(q, items: set[str] | None = None) -> str:
    """出題チャンネルに投稿する正解発表の文面（表示する項目は設定に従う）"""
    items = reveal_items() if items is None else items
    rows = db.execute("SELECT * FROM responses WHERE question_id=? ORDER BY answered_at",
                      (q["id"],)).fetchall()
    ok = [r for r in rows if r["is_correct"]]
    ng = [r for r in rows if not r["is_correct"]]
    esc = discord.utils.escape_markdown

    lines = [f"## 📢 第{q['number']}問の正解発表"]
    if "question" in items:
        lines.append(f"**問題**：{q['text']}")
    if "choices" in items and q["qtype"] == "choice":
        lines += [f"　{CHOICE_LABELS[i]}：{c}" for i, c in enumerate(get_choices(q))]
    if "answer" in items:
        lines.append(f"**正解**：{describe(q, json.loads(q['answers'])[0])}")
    if "note" in items and q["note"]:
        lines.append(f"**補足**：{q['note']}")
    if "counts" in items:
        lines.append(f"正解者は **{len(ok)}人**（回答 {len(rows)}人）でした！")
    if "breakdown" in items and q["qtype"] in ("ox", "choice"):
        labels = OX_LABELS if q["qtype"] == "ox" else CHOICE_LABELS[:len(get_choices(q))]
        lines.append("**内訳**：" + "　".join(
            f"{l}：{sum(r['answer'] == l for r in rows)}人" for l in labels))

    if "correct_names" in items:
        lines += ["", "### ⭕ 正解者（回答が早い順）"]
        if "correct_time" in items:
            lines += [f"- {esc(r['user_name'])}（{r['answered_at'] - q['asked_at']:.1f}秒）" for r in ok]
        else:
            lines += [f"- {esc(r['user_name'])}" for r in ok]
        if not ok:
            lines.append("- なし")
    if "wrong_names" in items or "wrong_answers" in items:
        lines += ["", "### ❌ 不正解者"]
        if "wrong_answers" in items:
            lines += [f"- {esc(r['user_name'])}：{esc(describe(q, r['answer']))}" for r in ng]
        else:
            lines += [f"- {esc(r['user_name'])}" for r in ng]
        if not ng:
            lines.append("- なし")
    return "\n".join(lines)


def build_result(number: int) -> str:
    q = q_by_number(number)
    rows = db.execute("SELECT * FROM responses WHERE question_id=? ORDER BY answered_at",
                      (q["id"],)).fetchall()
    ok = [r for r in rows if r["is_correct"]]
    ng = [r for r in rows if not r["is_correct"]]
    esc = discord.utils.escape_markdown
    lines = [
        f"## 第{number}問 集計結果",
        f"問題: {q['text']}",
        f"正解: {' / '.join(describe(q, a) for a in json.loads(q['answers']))}",
        f"回答 {len(rows)}人 ／ 正解 {len(ok)}人",
    ]
    if q["note"]:
        lines.insert(3, f"補足: {q['note']}")
    if q["qtype"] in ("ox", "choice"):
        labels = OX_LABELS if q["qtype"] == "ox" else CHOICE_LABELS[:len(get_choices(q))]
        counts = "　".join(f"{l}: {sum(r['answer'] == l for r in rows)}人" for l in labels)
        lines.append(f"内訳: {counts}")
    lines += ["", "### ⭕ 正解者（回答が早い順）"]
    lines += [f"- {esc(r['user_name'])}（{esc(describe(q, r['answer']))}／"
              f"{r['answered_at'] - q['asked_at']:.1f}秒）" for r in ok] or ["- なし"]
    lines += ["", "### ❌ 不正解者（修正は /quiz accept か /quiz judge）"]
    lines += [f"- {esc(r['user_name'])}：{esc(describe(q, r['answer']))}" for r in ng] or ["- なし"]
    return "\n".join(lines)


async def post_result(number: int):
    await send_long(await get_channel(RESULT_CHANNEL_ID), build_result(number))


def build_ranking() -> str:
    qs = db.execute("SELECT id, number, asked_at FROM questions WHERE status='closed' "
                    "ORDER BY number").fetchall()
    users: dict[int, dict] = {}
    for q in qs:
        for r in db.execute("SELECT * FROM responses WHERE question_id=?", (q["id"],)):
            u = users.setdefault(r["user_id"], {"name": r["user_name"], "marks": {},
                                                "score": 0, "time": 0.0})
            u["name"] = r["user_name"]
            u["marks"][q["number"]] = "○" if r["is_correct"] else "×"
            if r["is_correct"]:
                u["score"] += 1
                u["time"] += r["answered_at"] - q["asked_at"]

    ranked = sorted(users.values(), key=lambda u: (-u["score"], u["time"]))
    lines = [f"## 🏆 ランキング（全{len(qs)}問）",
             "成績欄: ○=正解 ×=不正解 －=未回答（第1問から順）", ""]
    rank, prev = 0, None
    for i, u in enumerate(ranked, 1):
        if u["score"] != prev:          # 同点は同順位
            rank, prev = i, u["score"]
        marks = "".join(u["marks"].get(q["number"], "－") for q in qs)
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "")
        lines.append(f"{medal}**{rank}位** {discord.utils.escape_markdown(u['name'])}　"
                     f"{u['score']}問正解　{marks}")
    if not ranked:
        lines.append("（まだ集計対象の回答がありません）")
    return "\n".join(lines)


def ready_for_next() -> bool:
    """次の問題を出題できる状態か（受付中の問題がなく、直前の問題の正解発表が済んでいる）"""
    if open_questions():
        return False
    last = last_closed_question()
    return last is None or bool(last["revealed"])


def auto_ask_running() -> bool:
    return bot.auto_ask_task is not None and not bot.auto_ask_task.done()


async def auto_ask_loop(quiz_channel_id: int, interval: int):
    """オート出題：出題できる状態になったら interval 秒待って次の問題を出題する。
    正解発表は、オート正解発表がONなら自動、OFFなら運営が手動で行う。"""
    first = True
    try:
        while True:
            if not ready_for_next():
                bot.auto_ask_next_at = None
                first = False
                await asyncio.sleep(1)
                continue
            if next_pending() is None:
                break
            wait = min(interval, 3) if first else interval  # 開始直後は3秒以内に最初の問題
            first = False
            bot.auto_ask_next_at = time.time() + wait
            await refresh_panels()
            while time.time() < bot.auto_ask_next_at and ready_for_next():
                await asyncio.sleep(0.5)
            if not ready_for_next():
                continue  # 待っている間に手動で出題された場合など
            q = next_pending()
            if q is None:
                break
            bot.auto_ask_next_at = None
            await open_question(q["id"], await get_channel(quiz_channel_id))
            await log_to_panels(f"🤖 第{q['number']}問［{TYPE_NAMES[q['qtype']]}］を"
                                f" <#{quiz_channel_id}> に自動で出題しました")
    except asyncio.CancelledError:
        bot.auto_ask_next_at = None
        raise
    except Exception as e:
        print("オート出題エラー:", repr(e))
        bot.auto_ask_task = None
        bot.auto_ask_next_at = None
        await refresh_panels()
        await log_to_panels(f"⚠️ オート出題でエラーが発生したため停止しました: {e}")
        return
    bot.auto_ask_task = None
    bot.auto_ask_next_at = None
    await refresh_panels()
    await log_to_panels("🤖 すべての問題を出題したため、オート出題を終了しました")


def start_auto_ask(quiz_channel_id: int, interval: int):
    stop_auto_ask()
    bot.auto_ask_interval = interval
    bot.auto_ask_task = asyncio.create_task(auto_ask_loop(quiz_channel_id, interval))


def stop_auto_ask():
    if bot.auto_ask_task and not bot.auto_ask_task.done():
        bot.auto_ask_task.cancel()
    bot.auto_ask_task = None
    bot.auto_ask_next_at = None


# ───────────────────────── Bot本体 ─────────────────────────
class QuizBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.timers: dict[int, asyncio.Task] = {}
        self.auto_ask_task: asyncio.Task | None = None   # オート出題
        self.auto_ask_interval: int = 0
        self.auto_ask_next_at: float | None = None
        self.sticky_tasks: dict[int, asyncio.Task] = {}   # パネル移動の待ちタスク
        self.sticky_locks: dict[int, asyncio.Lock] = {}   # パネル移動の同時実行防止
        self.sticky_force: set[int] = set()               # 必ず投稿し直すチャンネル
        self.reveal_timers: dict[int, asyncio.Task] = {}  # オート正解発表の待ちタスク
        self.reveal_due: dict[int, float] = {}            # オート正解発表の予定時刻

    async def setup_hook(self):
        self.add_dynamic_items(AnswerButton, ChoiceButton, PanelButton)
        guild = discord.Object(GUILD_ID)
        self.tree.add_command(quiz, guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        # 再起動時、受付中だった問題の締切タイマーを復元
        for q in db.execute("SELECT * FROM questions WHERE status='open'").fetchall():
            schedule_close(q["id"], q["deadline"])
        # オート正解発表の予約を復元
        sec = auto_reveal_seconds()
        last = last_closed_question()
        if sec is not None and last is not None and not last["revealed"] and not open_questions():
            due = (last["closed_at"] or time.time()) + sec
            schedule_auto_reveal(last["id"], max(due, time.time() + 5))
        await refresh_panels(with_view=True)
        # パネルを一番下に移動しておく
        for row in db.execute("SELECT DISTINCT channel_id FROM panels").fetchall():
            schedule_repost(row["channel_id"], force=True)  # 最新の形式で投稿し直す
        print(f"ログインしました: {self.user}")

    async def on_message(self, message: discord.Message):
        # 操作パネルのチャンネルに新しいメッセージが来たら、パネルを一番下に移動する
        if message.guild is None or is_panel_message(message):
            return
        if db.execute("SELECT 1 FROM panels WHERE channel_id=?", (message.channel.id,)).fetchone():
            schedule_repost(message.channel.id)


bot = QuizBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        return
    print("コマンドエラー:", repr(error))
    msg = f"エラーが発生しました: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


class QuizGroup(app_commands.Group):
    """運営ロールを持つ人だけが使えるコマンド群"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message(
            f"このコマンドは「{STAFF_ROLE}」ロールの人だけが使えます。", ephemeral=True)
        return False


quiz = QuizGroup(name="quiz", description="クイズ運営用コマンド", guild_only=True)


@quiz.command(name="ask", description="問題をその場で出題します")
@app_commands.describe(question="問題文（\\n で改行）",
                       answer="正解。複数の正解を認める場合は | 区切り（例: 富士山|ふじさん）",
                       seconds="回答時間（秒）", note="正解発表のときに表示する補足（任意）")
async def ask(interaction: discord.Interaction, question: str, answer: str,
              seconds: app_commands.Range[int, 10, 3600] = 60, note: str | None = None):
    answers = parse_answers(answer)
    if not answers:
        await interaction.response.send_message("正解を入力してください。", ephemeral=True)
        return
    qid, n = insert_question(question.replace("\\n", "\n"), answers, seconds,
                             note=note and note.replace("\\n", "\n"))
    db.commit()
    await interaction.response.send_message(
        f"第{n}問を出題します（正解: {' / '.join(answers)}／{seconds}秒）", ephemeral=True)
    await open_question(qid, interaction.channel, interaction.user.display_name)


@quiz.command(name="ox", description="○×問題を出題します")
@app_commands.describe(question="問題文（\\n で改行）", answer="正解", seconds="回答時間（秒）",
                       note="正解発表のときに表示する補足（任意）")
@app_commands.choices(answer=[app_commands.Choice(name="○", value="○"),
                              app_commands.Choice(name="×", value="×")])
async def ox(interaction: discord.Interaction, question: str, answer: str,
             seconds: app_commands.Range[int, 5, 3600] = 30, note: str | None = None):
    qid, n = insert_question(question.replace("\\n", "\n"), [answer], seconds, "ox", OX_LABELS,
                             note=note and note.replace("\\n", "\n"))
    db.commit()
    await interaction.response.send_message(
        f"第{n}問（○×）を出題します（正解: {answer}／{seconds}秒）", ephemeral=True)
    await open_question(qid, interaction.channel, interaction.user.display_name)


@quiz.command(name="choice", description="4択問題を出題します（C・Dを省略すると2択・3択）")
@app_commands.describe(question="問題文（\\n で改行）", a="選択肢A", b="選択肢B",
                       c="選択肢C", d="選択肢D", answer="正解の記号", seconds="回答時間（秒）",
                       note="正解発表のときに表示する補足（任意）")
@app_commands.choices(answer=[app_commands.Choice(name=l, value=l) for l in CHOICE_LABELS])
async def choice(interaction: discord.Interaction, question: str, a: str, b: str,
                 answer: str, c: str | None = None, d: str | None = None,
                 seconds: app_commands.Range[int, 5, 3600] = 30, note: str | None = None):
    choices = [x for x in (a, b, c, d) if x]
    if (c is None and d is not None) or CHOICE_LABELS.index(answer) >= len(choices):
        await interaction.response.send_message(
            "選択肢は A から順に入力し、正解には存在する選択肢を指定してください。", ephemeral=True)
        return
    qid, n = insert_question(question.replace("\\n", "\n"), [answer], seconds, "choice", choices,
                             note=note and note.replace("\\n", "\n"))
    db.commit()
    await interaction.response.send_message(
        f"第{n}問（選択問題）を出題します（正解: {answer}：{choices[CHOICE_LABELS.index(answer)]}"
        f"／{seconds}秒）", ephemeral=True)
    await open_question(qid, interaction.channel, interaction.user.display_name)


@quiz.command(name="load", description="問題リスト（XML または JSON ファイル）を登録します")
async def load(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        n, count = load_questions(parse_question_file(file.filename, await file.read()))
    except Exception as e:
        await interaction.followup.send(
            f"読み込みに失敗しました（形式を確認してください）: {e}", ephemeral=True)
        return
    await interaction.followup.send(
        f"{count}問を登録しました（第{n}問〜第{n + count - 1}問）。\n"
        "`/quiz next` で1問ずつ、`/quiz autorun` で全問自動出題できます。", ephemeral=True)
    await refresh_panels()


@quiz.command(name="next", description="登録済みの問題から次の1問を出題します")
async def next_cmd(interaction: discord.Interaction):
    q = next_pending()
    if q is None:
        await interaction.response.send_message("未出題の問題がありません。", ephemeral=True)
        return
    await interaction.response.send_message(f"第{q['number']}問を出題します。", ephemeral=True)
    await open_question(q["id"], interaction.channel, interaction.user.display_name)


@quiz.command(name="autorun", description="オート出題を開始します（登録済みの問題を順番に出題）")
@app_commands.describe(interval="正解発表から次の出題までの秒数")
async def autorun(interaction: discord.Interaction,
                  interval: app_commands.Range[int, 0, 600] = 10):
    if auto_ask_running():
        await interaction.response.send_message("すでにオート出題中です。", ephemeral=True)
        return
    if next_pending() is None:
        await interaction.response.send_message("未出題の問題がありません。", ephemeral=True)
        return
    start_auto_ask(interaction.channel.id, interval)
    await interaction.response.send_message("オート出題を開始しました。止めるときは `/quiz stop`。",
                                            ephemeral=True)
    await refresh_panels()


@quiz.command(name="stop", description="オート出題を停止します（出題中の問題は締切まで受け付けます）")
async def stop(interaction: discord.Interaction):
    if auto_ask_running():
        stop_auto_ask()
        await interaction.response.send_message("オート出題を停止しました。", ephemeral=True)
        await refresh_panels()
    else:
        await interaction.response.send_message("オート出題は実行されていません。", ephemeral=True)


@quiz.command(name="close", description="受付中の問題を今すぐ締め切ります")
async def close(interaction: discord.Interaction, number: int):
    q = q_by_number(number)
    if q is None or q["status"] != "open":
        await interaction.response.send_message(f"第{number}問は受付中ではありません。",
                                                ephemeral=True)
        return
    await interaction.response.send_message(f"第{number}問を締め切ります。", ephemeral=True)
    await close_question(q["id"], interaction.user.display_name)


@quiz.command(name="accept", description="別解を正解に追加し、その回答をした人を全員正解にします")
async def accept(interaction: discord.Interaction, number: int, answer: str):
    q = q_by_number(number)
    if q is None:
        await interaction.response.send_message(f"第{number}問はありません。", ephemeral=True)
        return
    answers = json.loads(q["answers"]) + [answer]
    db.execute("UPDATE questions SET answers=? WHERE id=?",
               (json.dumps(answers, ensure_ascii=False), q["id"]))
    changed = 0
    if q["status"] == "closed":
        for r in db.execute("SELECT user_id, answer FROM responses WHERE question_id=?",
                            (q["id"],)).fetchall():
            if normalize(r["answer"]) == normalize(answer):
                db.execute("UPDATE responses SET is_correct=1 WHERE question_id=? AND user_id=?",
                           (q["id"], r["user_id"]))
                changed += 1
    db.commit()
    await interaction.response.send_message(
        f"第{number}問の正解に「{answer}」を追加しました（{changed}人を正解に変更）。\n"
        f"`/quiz result {number}` で集計を出し直せます。", ephemeral=True)


@quiz.command(name="judge", description="特定の人の正誤を手動で修正します")
async def judge(interaction: discord.Interaction, number: int, user: discord.Member,
                correct: bool):
    q = q_by_number(number)
    cur = db.execute("UPDATE responses SET is_correct=? WHERE question_id=? AND user_id=?",
                     (int(correct), q["id"] if q else -1, user.id))
    db.commit()
    if cur.rowcount == 0:
        await interaction.response.send_message("該当する回答が見つかりません。", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"第{number}問の {user.display_name} さんを「{'正解' if correct else '不正解'}」にしました。",
            ephemeral=True)


@quiz.command(name="result", description="指定した問題の集計結果を集計チャンネルに出し直します")
async def result(interaction: discord.Interaction, number: int):
    q = q_by_number(number)
    if q is None or q["status"] != "closed":
        await interaction.response.send_message(f"第{number}問はまだ締め切られていません。",
                                                ephemeral=True)
        return
    await interaction.response.send_message("集計チャンネルに投稿します。", ephemeral=True)
    await post_result(number)


@quiz.command(name="reveal", description="このチャンネルで正解を発表します")
@app_commands.describe(number="問題番号",
                       note="補足（入力すると登録済みの補足を書き換えます。\\n で改行）")
async def reveal(interaction: discord.Interaction, number: int,
                 note: str | None = None):
    q = q_by_number(number)
    if q is None or q["status"] != "closed":
        await interaction.response.send_message(f"第{number}問はまだ締め切られていません。",
                                                ephemeral=True)
        return
    if note is not None:
        set_note(q["id"], note.replace("\\n", "\n"))
        q = q_by_id(q["id"])
    await interaction.response.send_message("発表しました。", ephemeral=True)
    await send_long(interaction.channel, build_reveal(q))
    db.execute("UPDATE questions SET revealed=1 WHERE id=?", (q["id"],))
    db.commit()
    task = bot.reveal_timers.pop(q["id"], None)
    bot.reveal_due.pop(q["id"], None)
    if task:
        task.cancel()
    await refresh_panels()


@quiz.command(name="ranking", description="正解数ランキングを集計します")
@app_commands.describe(public="ON にするとこのチャンネルで発表（OFFなら集計チャンネルへ）")
async def ranking(interaction: discord.Interaction, public: bool = False):
    await interaction.response.send_message("ランキングを投稿します。", ephemeral=True)
    target = interaction.channel if public else await get_channel(RESULT_CHANNEL_ID)
    await send_long(target, build_ranking())


@quiz.command(name="reset", description="全データを消去します（確認のため RESET と入力）")
async def reset(interaction: discord.Interaction, confirm: str):
    if confirm != "RESET":
        await interaction.response.send_message("confirm に RESET と入力すると消去します。",
                                                ephemeral=True)
        return
    do_reset()
    await interaction.response.send_message("全データを消去しました。", ephemeral=True)
    await refresh_panels()


# ───────────────────────── 操作パネル（運営用ボタン） ─────────────────────────
TYPE_NAMES = {"text": "自由入力", "ox": "○×", "choice": "選択式"}
PANEL_TITLE = "🎛️ クイズ操作パネル"
STICKY_DELAY = 2       # パネルのチャンネルに投稿があってから、パネルを一番下へ移動するまでの秒数
NOTICE_SECONDS = 15    # 本人にだけ見えるお知らせを自動で消すまでの秒数

PANEL_BUTTONS = {
    # action: (ラベル, 絵文字, 色)
    "ask_text": ("自由入力で出題", "📝", discord.ButtonStyle.primary),
    "ask_ox": ("○×で出題", "⭕", discord.ButtonStyle.primary),
    "ask_choice": ("選択式で出題", "🔤", discord.ButtonStyle.primary),
    "next": ("次の問題", "⏭️", discord.ButtonStyle.primary),
    "close": ("締切", "⏹️", discord.ButtonStyle.secondary),
    "auto_ask": ("オート出題：OFF", "🤖", discord.ButtonStyle.secondary),
    "reveal": ("正解発表", "📢", discord.ButtonStyle.success),
    "auto_reveal": ("オート正解発表：OFF", "⏱️", discord.ButtonStyle.secondary),
    "settings": ("表示設定", "⚙️", discord.ButtonStyle.secondary),
    "load": ("ロード", "📂", discord.ButtonStyle.secondary),
    "reset": ("リセット", "🗑️", discord.ButtonStyle.danger),
    "announce": ("結果発表（最終順位）", "🏆", discord.ButtonStyle.secondary),
}

# パネルのグループ分け：(見出し, [ボタンの行, ...])
PANEL_GROUPS = [
    ("📝 出題", [["ask_text", "ask_ox", "ask_choice"], ["next", "close", "auto_ask"]]),
    ("📢 正解発表", [["reveal", "auto_reveal", "settings"]]),
    ("🗂️ 問題の管理", [["load", "reset"]]),
    ("🏆 最終結果", [["announce"]]),
]


def build_status_text(quiz_channel_id: int) -> str:
    """操作パネル上部の進行状況（残り問題数・次の操作など）"""
    total = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM questions WHERE status='closed'").fetchone()[0]
    pending = db.execute("SELECT COUNT(*) FROM questions WHERE status='pending'").fetchone()[0]
    opened = open_questions()
    last = last_closed_question()

    lines = [f"## {PANEL_TITLE}",
             f"-# 出題先: <#{quiz_channel_id}>　｜　ボタンは「{STAFF_ROLE}」ロールの人だけが使えます",
             f"**登録済み** {total}問　／　**出題済み** {done}問　／　**残り {pending}問**"]
    for q in opened:
        lines.append(f"⏳ **回答受付中**：第{q['number']}問［{TYPE_NAMES[q['qtype']]}］"
                     f"回答 {answer_count(q['id'])}人・締切 <t:{int(q['deadline'])}:R>")
    if auto_ask_running():
        lines.append(f"🤖 **オート出題中**（正解発表の{bot.auto_ask_interval}秒後に次の問題を出題）")

    if opened:
        guide = "回答受付中です。締切を待つか「⏹️ 締切」を押してください"
    elif last is not None and not last["revealed"]:
        due = bot.reveal_due.get(last["id"])
        if due:
            guide = f"第{last['number']}問の正解は <t:{int(due)}:R> に自動で発表されます"
        else:
            guide = (f"第{last['number']}問の「📢 正解発表」をしてください"
                     "（発表するまで次の問題は出題できません）")
    elif auto_ask_running():
        nxt = next_pending()
        if nxt is not None and bot.auto_ask_next_at:
            guide = f"第{nxt['number']}問が <t:{int(bot.auto_ask_next_at)}:R> に自動で出題されます"
        else:
            guide = "まもなく次の問題が自動で出題されます"
    elif pending:
        guide = "「⏭️ 次の問題」・出題ボタン・「🤖 オート出題」のいずれかで出題してください"
    elif total:
        guide = "全問終了です。「🏆 結果発表」で順位を発表できます"
    else:
        guide = "出題ボタンか「📂 ロード」で問題を用意してください"
    lines.append(f"👉 **次の操作**：{guide}")
    return "\n".join(lines)


def build_upcoming_text() -> str | None:
    """これから出題する問題の一覧（先頭10問）"""
    pending = db.execute("SELECT * FROM questions WHERE status='pending' ORDER BY number").fetchall()
    if not pending:
        return None
    lines = ["**📋 これから出題する問題**"]
    for q in pending[:10]:
        text = q["text"].replace("\n", " ")
        text = text if len(text) <= 30 else text[:30] + "…"
        lines.append(f"-# 第{q['number']}問［{TYPE_NAMES[q['qtype']]}］{discord.utils.escape_markdown(text)}")
    if len(pending) > 10:
        lines.append(f"-# …ほか{len(pending) - 10}問")
    return "\n".join(lines)


def answer_count(qid: int) -> int:
    return db.execute("SELECT COUNT(*) FROM responses WHERE question_id=?", (qid,)).fetchone()[0]


def panel_state() -> dict[str, bool]:
    """今押せるボタン（True=押せる）。出題 → 締切 → 正解発表 の順にしか進めないようにする"""
    busy = busy_reason() is not None
    opened = bool(open_questions())
    last = last_closed_question()
    has_pending = next_pending() is not None
    has_closed = last is not None
    return {
        "ask_text": not busy, "ask_ox": not busy, "ask_choice": not busy,
        "next": not busy and has_pending,
        "close": opened,
        "reveal": not opened and has_closed and not last["revealed"],
        "auto_reveal": True,
        "auto_ask": auto_ask_running() or has_pending,
        "settings": True,
        "announce": has_closed,
        "load": True,
        "reset": True,
    }


def make_panel_button(action: str, quiz_channel_id: int, state: dict) -> "PanelButton":
    button = PanelButton(action, quiz_channel_id)
    button.item.disabled = not state[action]
    if action == "auto_reveal" and (sec := auto_reveal_seconds()) is not None:
        button.item.label = f"オート正解発表：ON（{sec}秒後）"
        button.item.style = discord.ButtonStyle.success
    if action == "auto_ask" and auto_ask_running():
        button.item.label = f"オート出題：ON（{bot.auto_ask_interval}秒後）"
        button.item.style = discord.ButtonStyle.success
    return button


def make_panel_view(quiz_channel_id: int) -> discord.ui.LayoutView:
    """操作パネル（状況表示＋グループ分けしたボタン）"""
    state = panel_state()
    container = discord.ui.Container(accent_colour=discord.Colour.gold())
    container.add_item(discord.ui.TextDisplay(build_status_text(quiz_channel_id)))
    if upcoming := build_upcoming_text():
        container.add_item(discord.ui.TextDisplay(upcoming))
    for title, rows in PANEL_GROUPS:
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        container.add_item(discord.ui.TextDisplay(f"### {title}"))
        for row in rows:
            action_row = discord.ui.ActionRow()
            for action in row:
                action_row.add_item(make_panel_button(action, quiz_channel_id, state))
            container.add_item(action_row)
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(
        "-# 出題・締切・発表などのたびに自動で更新され、常にチャンネルの一番下に表示されます"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def iter_custom_ids(components):
    for c in components or []:
        if cid := getattr(c, "custom_id", None):
            yield cid
        yield from iter_custom_ids(getattr(c, "children", None))
        if (acc := getattr(c, "accessory", None)) is not None:
            yield from iter_custom_ids([acc])


def is_panel_message(message: discord.Message) -> bool:
    if bot.user is None or message.author.id != bot.user.id:
        return False
    if message.embeds and message.embeds[0].title == PANEL_TITLE:  # 以前の形式のパネル
        return True
    return any(cid.startswith("quiz_panel:") for cid in iter_custom_ids(message.components))


async def refresh_panels(with_view: bool = True):
    """すべての操作パネルの表示とボタンの状態を最新にする"""
    for row in db.execute("SELECT * FROM panels").fetchall():
        try:
            ch = await get_channel(row["channel_id"])
            await ch.get_partial_message(row["message_id"]).edit(
                view=make_panel_view(row["quiz_channel_id"]))
        except discord.NotFound:
            db.execute("DELETE FROM panels WHERE message_id=?", (row["message_id"],))
            db.commit()
        except discord.HTTPException as e:
            # 以前の形式のパネルは書き換えられないので、新しく投稿し直す
            print("パネル更新エラー（投稿し直します）:", repr(e))
            schedule_repost(row["channel_id"], force=True)


def schedule_repost(channel_id: int, force: bool = False):
    """パネルのチャンネルに投稿があったら、少し待ってからパネルを一番下に移動する。
    続けて投稿があった場合は、最後の投稿から STICKY_DELAY 秒後に1回だけ移動する。"""
    if force:
        bot.sticky_force.add(channel_id)
    task = bot.sticky_tasks.get(channel_id)
    if task and not task.done():
        task.cancel()

    async def runner():
        await asyncio.sleep(STICKY_DELAY)
        force_now = channel_id in bot.sticky_force
        bot.sticky_force.discard(channel_id)
        await asyncio.shield(repost_panels(channel_id, force_now))

    bot.sticky_tasks[channel_id] = asyncio.create_task(runner())


async def repost_panels(channel_id: int, force: bool = False):
    """パネルを投稿し直して一番下に移動し、古いパネルを削除する"""
    lock = bot.sticky_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        try:
            ch = await get_channel(channel_id)
            for row in db.execute("SELECT * FROM panels WHERE channel_id=?", (channel_id,)).fetchall():
                if not force and ch.last_message_id == row["message_id"]:
                    continue  # すでに一番下
                new = await ch.send(view=make_panel_view(row["quiz_channel_id"]))
                db.execute("UPDATE panels SET message_id=? WHERE message_id=?",
                           (new.id, row["message_id"]))
                db.commit()
                try:
                    await ch.get_partial_message(row["message_id"]).delete()
                except discord.NotFound:
                    pass
            await remove_stray_panels(ch)
        except discord.HTTPException as e:
            print("パネル移動エラー:", repr(e))


async def remove_stray_panels(ch):
    """登録されていない余分な操作パネル（重複して残ったもの）を削除する"""
    keep = {r["message_id"] for r in
            db.execute("SELECT message_id FROM panels WHERE channel_id=?", (ch.id,)).fetchall()}
    async for m in ch.history(limit=50):
        if m.id not in keep and is_panel_message(m):
            try:
                await m.delete()
            except discord.HTTPException:
                pass


async def notice(interaction: discord.Interaction, text: str, **kwargs):
    """押した本人にだけ見えるお知らせ（しばらくすると自動で消える）"""
    await interaction.response.send_message(text, ephemeral=True,
                                            delete_after=NOTICE_SECONDS, **kwargs)


async def log(channel, text: str):
    """操作の記録をパネルのチャンネルに残す（その後パネルは自動で一番下に移動）"""
    await channel.send(text, allowed_mentions=NO_MENTIONS)


def busy_reason() -> str | None:
    """新しく出題できない理由（受付中の問題がある・自動出題中）"""
    if auto_ask_running():
        return "オート出題中です。手動で出題するときは「🤖 オート出題」を停止してください。"
    opened = open_questions()
    if opened:
        return f"第{opened[0]['number']}問の回答受付中です。締め切られてから出題してください。"
    last = last_closed_question()
    if last is not None and not last["revealed"]:
        due = bot.reveal_due.get(last["id"])
        if due:
            return (f"第{last['number']}問の正解発表を待っています"
                    f"（<t:{int(due)}:R>に自動で発表されます）。")
        return f"第{last['number']}問の正解発表がまだです。「📢 正解発表」を押してから出題してください。"
    return None


def build_question(qtype: str, text: str, choices_text: str, answer_text: str,
                   seconds_text: str) -> tuple[list[str], list[str] | None, int]:
    """出題ウィンドウの入力内容をチェックして (正解, 選択肢, 秒数) を返す"""
    if not text.strip():
        raise ValueError("問題文を入力してください。")
    choices = None
    if qtype == "text":
        answers = parse_answers(answer_text)
        if not answers:
            raise ValueError("正解を入力してください。")
    elif qtype == "ox":
        if not answer_text:
            raise ValueError("正解（○ か ×）を選んでください。")
        answers = [to_ox(answer_text)]
        choices = OX_LABELS
    else:
        choices = [c.strip() for c in choices_text.splitlines() if c.strip()]
        if not 2 <= len(choices) <= 4:
            raise ValueError("選択式は、選択肢を1行に1つずつ、2〜4行で入力してください。")
        label = unicodedata.normalize("NFKC", answer_text).strip().upper()
        if not label:
            raise ValueError("正解を選んでください。")
        if label not in CHOICE_LABELS[:len(choices)]:
            raise ValueError(f"正解は {'・'.join(CHOICE_LABELS[:len(choices)])} のどれかを選んでください。")
        answers = [label]
    default_sec = 60 if qtype == "text" else 30
    seconds_text = unicodedata.normalize("NFKC", seconds_text).strip()
    if not seconds_text:
        seconds = default_sec
    elif seconds_text.isdigit() and 5 <= int(seconds_text) <= 3600:
        seconds = int(seconds_text)
    else:
        raise ValueError("回答時間は 5〜3600 の数字（秒）で入力してください。")
    return answers, choices, seconds


class AskModal(discord.ui.Modal):
    """出題ウィンドウ（形式ごとに入力欄が変わる）"""

    def __init__(self, qtype: str, quiz_channel_id: int, prev: dict | None = None):
        super().__init__(title=f"{TYPE_NAMES[qtype]}の問題を出題")
        prev = prev or {}
        self.qtype = qtype
        self.quiz_channel_id = quiz_channel_id

        self.text = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=2000,
                                         default=prev.get("text"))
        self.add_item(discord.ui.Label(text="問題文", component=self.text))

        self.choices = None
        if qtype == "choice":
            self.choices = discord.ui.TextInput(
                style=discord.TextStyle.paragraph, max_length=400,
                placeholder="森鴎外\n夏目漱石\n芥川龍之介\n太宰治", default=prev.get("choices"))
            self.add_item(discord.ui.Label(text="選択肢", description="1行に1つずつ、2〜4行（上から A・B・C・D）",
                                           component=self.choices))

        if qtype == "text":
            self.answer = discord.ui.TextInput(max_length=200, placeholder="富士山|ふじさん",
                                               default=prev.get("answer"))
            self.add_item(discord.ui.Label(
                text="正解", description="複数の正解を認めるときは | で区切る", component=self.answer))
        else:
            labels = OX_LABELS if qtype == "ox" else CHOICE_LABELS
            self.answer = discord.ui.Select(placeholder="正解を選んでください", options=[
                discord.SelectOption(label=l, value=l, default=(prev.get("answer") == l))
                for l in labels])
            self.add_item(discord.ui.Label(text="正解", component=self.answer))

        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False,
                                         max_length=1000, placeholder="例：〜〜のため。",
                                         default=prev.get("note"))
        self.add_item(discord.ui.Label(
            text="補足（任意）", description="正解発表のときに表示されます。正解の理由などを書けます",
            component=self.note))

        default_sec = 60 if qtype == "text" else 30
        self.seconds = discord.ui.TextInput(required=False, max_length=4,
                                            placeholder=f"空欄なら{default_sec}秒",
                                            default=prev.get("seconds"))
        self.add_item(discord.ui.Label(text="回答時間（秒）", component=self.seconds))

    def answer_value(self) -> str:
        if isinstance(self.answer, discord.ui.Select):
            return self.answer.values[0] if self.answer.values else ""
        return self.answer.value

    async def on_submit(self, interaction: discord.Interaction):
        choices_text = self.choices.value if self.choices else ""
        prev = {"text": self.text.value, "choices": choices_text, "answer": self.answer_value(),
                "note": self.note.value, "seconds": self.seconds.value}
        try:
            reason = busy_reason()
            if reason:
                raise ValueError(reason)
            answers, choices, seconds = build_question(
                self.qtype, self.text.value, choices_text, self.answer_value(), self.seconds.value)
        except ValueError as e:
            # 入力し直せるよう、このお知らせは自動では消さない
            await interaction.response.send_message(
                f"⚠️ {e}", view=RetryAskView(self.qtype, self.quiz_channel_id, prev), ephemeral=True)
            return
        qid, n = insert_question(self.text.value.strip(), answers, seconds, self.qtype, choices,
                                 note=self.note.value.strip() or None)
        db.commit()
        await interaction.response.defer()
        await open_question(qid, await get_channel(self.quiz_channel_id))
        await log(interaction.channel,
                  f"📝 {interaction.user.display_name} さんが第{n}問［{TYPE_NAMES[self.qtype]}］を"
                  f" <#{self.quiz_channel_id}> に出題しました（{seconds}秒）")


class RetryAskView(discord.ui.View):
    """入力ミスのとき、入力内容を残したまま出題ウィンドウを開き直すボタン"""

    def __init__(self, qtype: str, quiz_channel_id: int, prev: dict):
        super().__init__(timeout=600)
        self.qtype = qtype
        self.quiz_channel_id = quiz_channel_id
        self.prev = prev

    @discord.ui.button(label="入力し直す", emoji="✏️", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AskModal(self.qtype, self.quiz_channel_id, self.prev))


class LoadModal(discord.ui.Modal):
    """問題リスト（XML・JSON）のアップロードウィンドウ"""

    def __init__(self):
        super().__init__(title="問題リストを読み込む")
        self.file = discord.ui.FileUpload(max_values=1)
        self.add_item(discord.ui.Label(
            text="問題リスト（XMLファイル）",
            description="questions_sample.xml と同じ形式のファイルを選んでください（JSONも可）",
            component=self.file))

    async def on_submit(self, interaction: discord.Interaction):
        if not self.file.values:
            await notice(interaction, "ファイルが選ばれていません。")
            return
        att = self.file.values[0]
        await interaction.response.defer()  # ファイルの読み込みに時間がかかることがあるため先に応答
        try:
            n, count = load_questions(parse_question_file(att.filename, await att.read()))
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ 読み込みに失敗しました（形式を確認してください）: {e}", ephemeral=True)
            return
        await refresh_panels()
        await log(interaction.channel,
                  f"📂 {interaction.user.display_name} さんが「{att.filename}」から"
                  f"{count}問を登録しました（第{n}問〜第{n + count - 1}問）")


async def log_to_panels(text: str):
    """操作の記録を、すべての操作パネルのチャンネルに残す"""
    for row in db.execute("SELECT DISTINCT channel_id FROM panels").fetchall():
        try:
            await log(await get_channel(row["channel_id"]), text)
        except discord.HTTPException as e:
            print("記録の投稿エラー:", repr(e))


async def reveal_question(qid: int, who: str | None = None) -> bool:
    """正解を発表する（who=None ならオート正解発表）。発表したら True"""
    q = q_by_id(qid)
    if q is None or q["status"] != "closed" or q["revealed"]:
        return False
    db.execute("UPDATE questions SET revealed=1 WHERE id=?", (qid,))  # 二重発表の防止
    db.commit()
    task = bot.reveal_timers.pop(qid, None)
    bot.reveal_due.pop(qid, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    await send_long(await get_channel(q["channel_id"]), build_reveal(q))
    await refresh_panels()
    if who:
        await log_to_panels(f"📢 {who} さんが第{q['number']}問の正解を <#{q['channel_id']}> で発表しました")
    else:
        await log_to_panels(f"⏱️ 第{q['number']}問の正解を <#{q['channel_id']}> で自動発表しました")
    return True


def schedule_auto_reveal(qid: int, when: float):
    """指定した時刻に正解を自動で発表する"""
    old = bot.reveal_timers.pop(qid, None)
    if old:
        old.cancel()

    async def runner():
        await asyncio.sleep(max(0, when - time.time()))
        try:
            await reveal_question(qid)
        except Exception as e:
            print("オート正解発表エラー:", repr(e))
        finally:
            bot.reveal_timers.pop(qid, None)
            bot.reveal_due.pop(qid, None)

    bot.reveal_due[qid] = when
    bot.reveal_timers[qid] = asyncio.create_task(runner())


def cancel_auto_reveals():
    for task in bot.reveal_timers.values():
        task.cancel()
    bot.reveal_timers.clear()
    bot.reveal_due.clear()


class AutoRevealModal(discord.ui.Modal):
    """オート正解発表の設定ウィンドウ"""

    def __init__(self):
        super().__init__(title="オート正解発表の設定")
        self.seconds = discord.ui.TextInput(max_length=3, placeholder="例：10",
                                            default=get_setting("auto_reveal_last", "10"))
        self.add_item(discord.ui.TextDisplay(
            "ONにすると、問題が締め切られてから設定した秒数が経つと、自動で正解が発表されます。"))
        self.add_item(discord.ui.Label(
            text="締切から正解発表までの秒数", description="0〜600 の数字（0 なら締切と同時に発表）",
            component=self.seconds))

    async def on_submit(self, interaction: discord.Interaction):
        text = unicodedata.normalize("NFKC", self.seconds.value).strip()
        if not text.isdigit() or not 0 <= int(text) <= 600:
            await notice(interaction, "⚠️ 秒数は 0〜600 の数字で入力してください。")
            return
        sec = int(text)
        set_setting("auto_reveal", str(sec))
        set_setting("auto_reveal_last", str(sec))
        await interaction.response.defer()
        # 締切後で、まだ正解発表していない問題があれば、今から数えて発表を予約
        last = last_closed_question()
        if last is not None and not last["revealed"] and not open_questions():
            schedule_auto_reveal(last["id"], time.time() + sec)
        await refresh_panels()
        await log(interaction.channel,
                  f"⏱️ {interaction.user.display_name} さんがオート正解発表をONにしました"
                  f"（締切の{sec}秒後に発表）")


class SettingsModal(discord.ui.Modal):
    """正解発表の表示設定ウィンドウ"""

    def __init__(self):
        super().__init__(title="正解発表の表示設定")
        current = reveal_items()
        self.items = discord.ui.CheckboxGroup(
            required=False, min_values=0, max_values=len(REVEAL_ITEMS),
            options=[discord.CheckboxGroupOption(label=label, value=key, description=desc,
                                                 default=key in current)
                     for key, (label, desc) in REVEAL_ITEMS.items()])
        self.add_item(discord.ui.Label(
            text="正解発表に表示する項目", description="チェックを入れた項目が表示されます",
            component=self.items))

    async def on_submit(self, interaction: discord.Interaction):
        chosen = [k for k in REVEAL_ITEMS if k in self.items.values]
        set_setting("reveal_items", json.dumps(chosen))
        await interaction.response.defer()
        names = "、".join(REVEAL_ITEMS[k][0] for k in chosen) or "なし（見出しのみ）"
        await log(interaction.channel,
                  f"⚙️ {interaction.user.display_name} さんが正解発表の表示設定を変更しました"
                  f"（表示する項目：{names}）")


class AutoAskModal(discord.ui.Modal):
    """オート出題の設定ウィンドウ"""

    def __init__(self, quiz_channel_id: int):
        super().__init__(title="オート出題の設定")
        self.quiz_channel_id = quiz_channel_id
        count = db.execute("SELECT COUNT(*) FROM questions WHERE status='pending'").fetchone()[0]
        reveal = ("オート正解発表がONなので、正解発表も自動で行われます。"
                  if auto_reveal_seconds() is not None else
                  "オート正解発表はOFFなので、正解発表は「📢 正解発表」を押して行ってください。"
                  "発表すると、設定した秒数後に次の問題が出題されます。")
        self.add_item(discord.ui.TextDisplay(
            f"未出題の{count}問を、上から順番に自動で出題します。\n{reveal}"))
        self.seconds = discord.ui.TextInput(max_length=3, placeholder="例：10",
                                            default=get_setting("auto_ask_last", "10"))
        self.add_item(discord.ui.Label(
            text="正解発表から次の出題までの秒数",
            description="0〜600 の数字（最初の問題は数秒後に出題されます）",
            component=self.seconds))

    async def on_submit(self, interaction: discord.Interaction):
        text = unicodedata.normalize("NFKC", self.seconds.value).strip()
        if not text.isdigit() or not 0 <= int(text) <= 600:
            await notice(interaction, "⚠️ 秒数は 0〜600 の数字で入力してください。")
            return
        if next_pending() is None:
            await notice(interaction, "未出題の問題がありません。")
            return
        if auto_ask_running():
            await notice(interaction, "すでにオート出題中です。")
            return
        sec = int(text)
        set_setting("auto_ask_last", str(sec))
        await interaction.response.defer()
        start_auto_ask(self.quiz_channel_id, sec)
        await refresh_panels()
        await log(interaction.channel,
                  f"🤖 {interaction.user.display_name} さんがオート出題を開始しました"
                  f"（正解発表の{sec}秒後に次の問題）")


def last_closed_question():
    return db.execute("SELECT * FROM questions WHERE status='closed' "
                      "ORDER BY asked_at DESC, number DESC LIMIT 1").fetchone()


class ConfirmView(discord.ui.View):
    """押し間違い防止の確認ダイアログ（押した本人にだけ表示）"""

    def __init__(self, ok_label: str, ok_emoji: str, on_ok):
        super().__init__(timeout=60)
        self.on_ok = on_ok
        self.ok.label = ok_label
        self.ok.emoji = ok_emoji
        self.message: discord.InteractionMessage | None = None

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def ok(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        # Discordには3秒以内に応答する必要があるので、先に「処理中」と返してから処理する
        await interaction.response.edit_message(content="⏳ 処理しています…", view=None)
        try:
            result = await self.on_ok(interaction)
        except Exception as e:
            print("確認ダイアログの処理でエラー:", repr(e))
            result = f"⚠️ エラーが発生しました: {e}"
        try:
            msg = await interaction.edit_original_response(content=result)
            await msg.delete(delay=5)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="キャンセルしました。", view=None,
                                                delete_after=5)

    async def on_timeout(self):
        # 60秒間どちらも押されなかったら、ダイアログを消す
        if self.message:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


async def ask_confirm(interaction: discord.Interaction, text: str, ok_label: str,
                      ok_emoji: str, on_ok):
    view = ConfirmView(ok_label, ok_emoji, on_ok)
    await interaction.response.send_message(text, view=view, ephemeral=True)
    view.message = await interaction.original_response()


class PanelButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"quiz_panel:(?P<action>[a-z_]+):(?P<ch>[0-9]+)"):
    def __init__(self, action: str, quiz_channel_id: int):
        label, emoji, style = PANEL_BUTTONS[action]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style,
            custom_id=f"quiz_panel:{action}:{quiz_channel_id}"))
        self.action = action
        self.quiz_channel_id = quiz_channel_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        action = match["action"]
        if action not in PANEL_BUTTONS:  # 古いバージョンのパネルのボタン
            action = "ask_text"
        return cls(action, int(match["ch"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await notice(interaction, f"このボタンは「{STAFF_ROLE}」ロールの人だけが使えます。")
        return False

    async def callback(self, interaction: discord.Interaction):
        handler = getattr(self, f"do_{self.action}")
        await handler(interaction)

    async def open_ask(self, interaction: discord.Interaction, qtype: str):
        if reason := busy_reason():
            await notice(interaction, f"⚠️ {reason}")
            return
        await interaction.response.send_modal(AskModal(qtype, self.quiz_channel_id))

    async def do_ask_text(self, interaction: discord.Interaction):
        await self.open_ask(interaction, "text")

    async def do_ask_ox(self, interaction: discord.Interaction):
        await self.open_ask(interaction, "ox")

    async def do_ask_choice(self, interaction: discord.Interaction):
        await self.open_ask(interaction, "choice")

    async def do_next(self, interaction: discord.Interaction):
        if reason := busy_reason():
            await notice(interaction, f"⚠️ {reason}")
            return
        q = next_pending()
        if q is None:
            await notice(interaction, "未出題の問題がありません。"
                                      "「ロード」で問題リストを登録するか、「出題」を使ってください。")
            return
        await interaction.response.defer()
        await open_question(q["id"], await get_channel(self.quiz_channel_id))
        await log(interaction.channel,
                  f"⏭️ {interaction.user.display_name} さんが第{q['number']}問"
                  f"［{TYPE_NAMES[q['qtype']]}］を <#{self.quiz_channel_id}> に出題しました")

    async def do_close(self, interaction: discord.Interaction):
        opened = open_questions()
        if not opened:
            await notice(interaction, "回答受付中の問題はありません。")
            return
        targets = [q["id"] for q in opened]
        detail = "\n".join(
            f"・第{q['number']}問［{TYPE_NAMES[q['qtype']]}］回答 {answer_count(q['id'])}人／"
            f"締切 <t:{int(q['deadline'])}:R>" for q in opened)

        async def on_ok(i: discord.Interaction) -> str:
            closed = []
            for qid in targets:
                q = q_by_id(qid)
                if q and q["status"] == "open":
                    closed.append(f"第{q['number']}問（回答 {answer_count(qid)}人）")
                    await close_question(qid)
            if not closed:
                return "すでに締め切られていました。"
            await log(i.channel, f"⏹️ {i.user.display_name} さんが{'、'.join(closed)}を締め切りました")
            return "⏹️ 締め切りました。"

        await ask_confirm(interaction,
                          f"⚠️ **今すぐ締め切りますか？**\n{detail}\n"
                          "締め切ると、まだ回答していない人は回答できなくなります。",
                          "締め切る", "⏹️", on_ok)

    async def do_reveal(self, interaction: discord.Interaction):
        opened = open_questions()
        if opened:
            await notice(interaction, f"⚠️ 第{opened[0]['number']}問の回答受付中です。"
                                      "締め切られてから正解発表をしてください。")
            return
        q = last_closed_question()
        if q is None:
            await notice(interaction, "まだ締め切られた問題がありません。")
            return
        if q["revealed"]:
            await notice(interaction, f"第{q['number']}問の正解はすでに発表済みです。"
                                      f"もう一度発表する場合は `/quiz reveal number:{q['number']}` を使ってください。")
            return
        await interaction.response.defer()
        await reveal_question(q["id"], interaction.user.display_name)

    async def do_auto_ask(self, interaction: discord.Interaction):
        if not auto_ask_running():
            if next_pending() is None:
                await notice(interaction, "未出題の問題がありません。「📂 ロード」で問題リストを登録してください。")
                return
            await interaction.response.send_modal(AutoAskModal(self.quiz_channel_id))
            return

        async def on_ok(i: discord.Interaction) -> str:
            stop_auto_ask()
            await refresh_panels()
            await log(i.channel, f"🤖 {i.user.display_name} さんがオート出題を停止しました")
            return "🤖 オート出題を停止しました。"

        await ask_confirm(interaction,
                          "⚠️ **オート出題を停止しますか？**\n"
                          f"現在の設定：正解発表の{bot.auto_ask_interval}秒後に次の問題を出題\n"
                          "出題中の問題は、停止しても締切まで回答を受け付けます。",
                          "停止する", "🤖", on_ok)

    async def do_settings(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SettingsModal())

    async def do_auto_reveal(self, interaction: discord.Interaction):
        sec = auto_reveal_seconds()
        if sec is None:
            await interaction.response.send_modal(AutoRevealModal())
            return

        async def on_ok(i: discord.Interaction) -> str:
            set_setting("auto_reveal", "")
            cancel_auto_reveals()
            await refresh_panels()
            await log(i.channel, f"⏱️ {i.user.display_name} さんがオート正解発表を停止しました")
            return "⏱️ オート正解発表を停止しました。"

        pending = ""
        last = last_closed_question()
        if last is not None and last["id"] in bot.reveal_due:
            pending = (f"\n停止すると、第{last['number']}問の自動発表（<t:{int(bot.reveal_due[last['id']])}:R>）も"
                       "取り消されます。その場合は「📢 正解発表」で発表してください。")
        await ask_confirm(interaction,
                          f"⚠️ **オート正解発表を停止しますか？**\n"
                          f"現在の設定：締切の{sec}秒後に自動で正解発表{pending}",
                          "停止する", "⏱️", on_ok)

    async def do_announce(self, interaction: discord.Interaction):
        if db.execute("SELECT COUNT(*) FROM questions WHERE status='closed'").fetchone()[0] == 0:
            await notice(interaction, "まだ締め切られた問題がありません。")
            return
        await interaction.response.defer()
        await send_long(await get_channel(self.quiz_channel_id), build_ranking())
        note = "（受付中の問題は集計に含まれていません）" if open_questions() else ""
        await log(interaction.channel,
                  f"🏆 {interaction.user.display_name} さんが <#{self.quiz_channel_id}> で"
                  f"結果を発表しました{note}")

    async def do_load(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LoadModal())

    async def do_reset(self, interaction: discord.Interaction):
        total = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        answers = db.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

        async def on_ok(i: discord.Interaction) -> str:
            do_reset()
            await refresh_panels()
            await log(i.channel, f"🗑️ {i.user.display_name} さんが全データ（問題・回答・集計）を"
                                 "リセットしました")
            return "🗑️ リセットしました。"

        await ask_confirm(interaction,
                          f"⚠️ **本当にリセットしますか？**\n"
                          f"登録済みの問題 {total}問と、回答 {answers}件がすべて削除されます。"
                          f"**元に戻すことはできません。**",
                          "本当にリセットする", "🗑️", on_ok)


REQUIRED_PERMS = {
    "view_channel": "チャンネルを見る",
    "send_messages": "メッセージを送信",
    "embed_links": "埋め込みリンク",
    "read_message_history": "メッセージ履歴を読む",
}


def missing_permissions(channel) -> list[str]:
    """Botに足りない権限の名前一覧（足りていれば空）"""
    me = channel.guild.me
    perms = channel.permissions_for(me)
    return [name for attr, name in REQUIRED_PERMS.items() if not getattr(perms, attr)]


@quiz.command(name="panel", description="このチャンネルに運営用の操作パネルを設置します")
@app_commands.describe(channel="問題を出題するチャンネル")
async def panel(interaction: discord.Interaction, channel: discord.TextChannel):
    # Botに必要な権限があるか、先に確認する
    problems = []
    for where, ch in (("このチャンネル", interaction.channel), (f"出題先の {channel.mention}", channel)):
        missing = missing_permissions(ch)
        if missing:
            problems.append(f"・{where}：{'、'.join(missing)}")
    result_ch = bot.get_channel(RESULT_CHANNEL_ID)
    if result_ch is None:
        problems.append("・集計チャンネル：見つかりません（start.bat の RESULT_CHANNEL_ID を確認してください）")
    elif missing := missing_permissions(result_ch):
        problems.append(f"・集計チャンネル {result_ch.mention}：{'、'.join(missing)}")
    if problems:
        await interaction.response.send_message(
            "⚠️ Botの権限が足りないため、パネルを設置できません。\n"
            + "\n".join(problems) + "\n\n"
            "チャンネルの編集 →「権限」→「メンバーまたはロールを追加」でBotを追加し、"
            "上の権限を許可してから、もう一度実行してください。",
            ephemeral=True)
        return

    text = "操作パネルを設置しました。"
    # 誤って全員が見られるチャンネルに置いていないか確認
    if interaction.guild and \
            interaction.channel.permissions_for(interaction.guild.default_role).view_channel:
        text += ("\n⚠️ このチャンネルは全員が閲覧できる設定です。ボタンは運営しか押せませんが、"
                 "パネルの内容（これから出題する問題の一部など）は見えてしまいます。"
                 "運営だけが見られるチャンネルに設置し直すことをおすすめします。")
    await interaction.response.send_message(text, ephemeral=True, delete_after=60)

    # このチャンネルにある古いパネルは削除（1チャンネルに1つ）
    for row in db.execute("SELECT message_id FROM panels WHERE channel_id=?",
                          (interaction.channel.id,)).fetchall():
        try:
            await interaction.channel.get_partial_message(row["message_id"]).delete()
        except discord.HTTPException:
            pass
        db.execute("DELETE FROM panels WHERE message_id=?", (row["message_id"],))
    db.commit()

    msg = await interaction.channel.send(view=make_panel_view(channel.id))
    db.execute("INSERT INTO panels(message_id, channel_id, quiz_channel_id) VALUES(?,?,?)",
               (msg.id, interaction.channel.id, channel.id))
    db.commit()


def ensure_single_instance() -> socket.socket:
    """同じPCでBotが2つ起動しないようにする（2つ目は起動せずに終了する）"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("=" * 60)
        print("⚠️ このBotはすでに起動しています。")
        print("   2つ同時に動かすと、パネルやメッセージが二重に投稿されます。")
        print("   ほかにBotの黒い画面が開いていないか確認してください。")
        print("   再起動したいときは、先に今のBotを停止してから起動してください。")
        print("=" * 60)
        sys.exit(1)
    return sock


if __name__ == "__main__":
    _instance_lock = ensure_single_instance()  # Botが動いている間、持ち続ける
    bot.run(TOKEN)
