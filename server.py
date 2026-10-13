"""les-sammen —— 一起读的书房。

把 pdf / epub / txt 放上书架，在网页阅读器里读；划一句、点「一起读这页」、在书页旁的小窗里说话，
就会送给你的 AI（claude -p 接着你的会话 / Claude API / 你自己的 bridge 或 SDK 脚本），
它的回复贴回那一页、那条线旁边。同时是一个 MCP 服务：AI 能翻书、搜书、记进度、看你划过的线。
一个文件 + SQLite，配置在 config.json。
"""
import asyncio
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

BASE = Path(__file__).parent  # 代码、页面（shelf.html / les.html）、assets/ 在这


def _load_config() -> dict:
    p = Path(os.environ.get("LES_CONFIG") or BASE / "config.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


CONF = _load_config()
DATA = (BASE / (CONF.get("data_dir") or "data")).resolve()  # 库、原件、页面图都在这下面
DB_PATH = DATA / "bibliotek.db"
BOOKS_DIR = DATA / "books"  # 上架的原件（pdf / epub / txt）
SIDER_DIR = DATA / "sider"  # pdf 每页切成 jpg（sider/<id>/s-001.jpg）；封面也放这（cover.* / mine.*）
HOST = CONF.get("host") or "127.0.0.1"
PORT = int(os.environ.get("LES_PORT") or CONF.get("port") or 18050)
AI = CONF.get("ai_name") or "Claude"          # 页面上 AI 叫什么
READER = CONF.get("reader") or "她"            # 送给 AI 的那行里怎么称呼读书的人：「第64页 她划了：…」
COMP = CONF.get("companion") or {}             # 陪读怎么接 AI，见 config.example.json 和 README
TEXT_EXT = (".epub", ".txt")
BOOK_EXT = (".pdf",) + TEXT_EXT
MAX_UPLOAD = int(CONF.get("max_upload_mb") or 200) * 1024 * 1024
RENDER_DPI = 110  # pdf 切页面图的清晰度
TZ = timezone(timedelta(hours=float(CONF.get("tz_hours", 8))))

mcp = FastMCP(CONF.get("mcp_name") or "bibliotek", instructions=f"""
书房（les-sammen）——{READER}的书架和阅读器。书按页存在库里（epub / txt 按手机一屏分好页，页码跟阅读器里看到的一样）。

用法约定：
- 先 shelf 看书架（按分类分组，每本带 [id] 和读到第几页），别一上来就大段取内容；
- 取内容用 read_pages(book_id, start, end)，一次最多 10 页，够用就少取；
- toc 看目录，find 在书里搜关键词、返回命中页码；mark(book_id, page) 记读到哪；
- {READER}在阅读器里划线、点「一起读这页」、在书页旁的小窗里说话，会以「[书房]《书名》（书 N）」开头送进对话，
  回的话会贴回那一页、那条线旁边，{READER}在书里看——所以回得短，像坐在旁边小声说；
  整页原文一般不会自动给（「一起读」除外），要看就 read_pages 自己翻；marks(book_id) 看{READER}划过哪些线；
- 生词本 vocab_add / vocab，错题本 mistake_add / mistakes，练习题 exercise_add / exercises；
- 导入服务器上的书用 import_book(path)；txt 认不出章节可以 relayout(book_id, chapter_titles=...) 重排。
""")



def now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


_MIGRATED = False


def db() -> sqlite3.Connection:
    global _MIGRATED
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS books(
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      author TEXT NOT NULL DEFAULT '',
      filename TEXT NOT NULL,
      pages INTEGER NOT NULL,
      progress INTEGER NOT NULL DEFAULT 0,
      added_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pages(
      book_id INTEGER NOT NULL REFERENCES books(id),
      page_no INTEGER NOT NULL,
      text TEXT NOT NULL,
      PRIMARY KEY(book_id, page_no)
    );
    CREATE TABLE IF NOT EXISTS vocab(
      id INTEGER PRIMARY KEY,
      word TEXT NOT NULL,
      meaning TEXT NOT NULL DEFAULT '',
      example TEXT NOT NULL DEFAULT '',
      book_id INTEGER NOT NULL DEFAULT 0,
      page_no INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT '',
      added_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mistakes(
      id INTEGER PRIMARY KEY,
      question TEXT NOT NULL,
      wrong TEXT NOT NULL DEFAULT '',
      right TEXT NOT NULL,
      note TEXT NOT NULL DEFAULT '',
      cleared INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS exercises(
      id INTEGER PRIMARY KEY,
      question TEXT NOT NULL,
      answer TEXT NOT NULL DEFAULT '',
      her_answer TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
      note TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    -- epub/txt 的排版：每页一串段落 [{t:h|p, s:文字, c:1=接上页}]，阅读器照这个画；pages.text 是同一页的纯文字给 AI 读
    CREATE TABLE IF NOT EXISTS layout(
      book_id INTEGER NOT NULL,
      page_no INTEGER NOT NULL,
      blocks TEXT NOT NULL,
      PRIMARY KEY(book_id, page_no)
    );
    CREATE TABLE IF NOT EXISTS chapters(
      book_id INTEGER NOT NULL,
      idx INTEGER NOT NULL,
      title TEXT NOT NULL,
      page_no INTEGER NOT NULL,
      PRIMARY KEY(book_id, idx)
    );
    -- 读者在阅读器里划的线：第几页第几段、段内起止
    CREATE TABLE IF NOT EXISTS marks(
      id INTEGER PRIMARY KEY,
      book_id INTEGER NOT NULL,
      page_no INTEGER NOT NULL,
      block INTEGER NOT NULL DEFAULT -1,
      start INTEGER NOT NULL DEFAULT 0,
      end INTEGER NOT NULL DEFAULT 0,
      quote TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    -- 书页旁的小窗：读者说的（say / mark / together）和 AI 回的（reply）；turn_id 是送进对话的那一轮
    CREATE TABLE IF NOT EXISTS talk(
      id INTEGER PRIMARY KEY,
      book_id INTEGER NOT NULL,
      page_no INTEGER NOT NULL DEFAULT 0,
      who TEXT NOT NULL,
      kind TEXT NOT NULL,
      text TEXT NOT NULL,
      ref INTEGER,
      turn_id INTEGER,
      ts REAL NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS talk_book ON talk(book_id, id);
    CREATE TABLE IF NOT EXISTS kv(
      key TEXT PRIMARY KEY,
      value TEXT
    );
    CREATE TABLE IF NOT EXISTS turns(
      id INTEGER PRIMARY KEY,
      book_id INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'sending',
      sent TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      done_at TEXT
    );
    """)
    if not _MIGRATED:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
        if "kind" not in cols:  # 10-13 起书分两种：pdf（看切好的页面图）/ text（epub、txt，阅读器排字）
            conn.execute("ALTER TABLE books ADD COLUMN kind TEXT NOT NULL DEFAULT 'pdf'")
            conn.commit()
        if "category" not in cols:  # 分门别类，每类一层小书架；旧库升级时 pdf 算课本、epub/txt 算阅读
            conn.execute("ALTER TABLE books ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE books SET category = CASE kind WHEN 'text' THEN '阅读' ELSE '课本' END WHERE category=''")
            conn.commit()
        _MIGRATED = True
    return conn


def _extract(src: Path):
    """按页抽文字。返回 (页数, [(页码, 文字)], 没文字的页数, 元数据)；打不开就抛。"""
    from pypdf import PdfReader
    reader = PdfReader(str(src))
    n = len(reader.pages)
    texts, empty = [], 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            txt = (page.extract_text() or "").strip()
        except Exception:
            txt = ""
        if not txt:
            empty += 1
        texts.append((i, txt))
    meta = {"title": "", "author": ""}
    try:
        m = reader.metadata
        if m:
            meta["title"] = (m.title or "").strip()
            meta["author"] = (m.author or "").strip()
    except Exception:
        pass
    return n, texts, empty, meta


def _guess_title(meta_title: str, fallback: str) -> str:
    """PDF 元数据里的标题常是垃圾（untitled / Microsoft Word - xxx），像样才用，否则用文件名。"""
    s = (meta_title or "").strip()
    if not s or len(s) > 120 or re.search(r"(?i)untitled|microsoft word|\.docx?$|\.pdf$|^doc\d*$", s):
        return fallback
    return s


# ---------------- epub / txt：抽成段落，按手机一屏分页 ----------------
# 页码是服务器分好的固定页（AI read_pages 和读者在阅读器里看到的是同一页）。
# 一屏按 412×915 的手机、正文 18px 估：一行约 18 个汉字，一页按 24 行估（上下两栏平时收着，纸页占满；估算偏保守，412 宽的手机上实际排二十一二行）；字放大了阅读器里那页就能上下滑。
LINE_UNITS = 17.6
PAGE_LINES = 24.0
PARA_GAP = 0.3

_NUM = "[0-9０-９零〇一二两三四五六七八九十百千万]+"
_SEP = r"[\s:：·.、—-]"
_CH_RE = re.compile(  # 章/节/卷后面可以直接跟标题；回/集/部/篇/幕/话后面要隔开，免得「第一回合就输了」被当成章节
    rf"^(?:第{_NUM}[章节卷].{{0,30}}|第{_NUM}[回集部篇幕话](?:{_SEP}.{{0,30}})?"
    rf"|(?:序章|序言|序|楔子|引子|引言|前言|后记|尾声|终章|番外)(?:{_SEP}.{{0,30}})?"
    rf"|(?:Chapter|CHAPTER|Kapittel|KAPITTEL|Part|PART)\s*[0-9IVXLCivxlc]+(?:{_SEP}.{{0,40}})?)$")
# 汉字、全角符号、中文标点
_CJK = "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x2e80, 0x9fff), (0xf900, 0xfaff), (0xff00, 0xffef), (0x3000, 0x303f)))
_SENT = re.compile(r".+?(?:[。！？!?；;…]+[」』”’\"）)]*|\.(?=\s)|$)", re.S)


def _decode_txt(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _clean(s: str) -> str:
    # 排版换行夹在两个汉字中间的直接去掉（英文的换行算一个空格）；书里本来就有的空格留着
    s = re.sub(rf"(?<=[{_CJK}])[ \t]*\n\s*(?=[{_CJK}])", "", s.replace("\r", ""))
    s = re.sub(r"\s+", " ", s.replace(chr(0xa0), " "))  # 不换行空格当普通空格
    return s.strip(" " + chr(0x3000) + chr(0xfeff))  # 去掉全角空格缩进和 BOM


_PUNCT = re.compile(r"[，。！？、；：,.!?;:\"“”'‘’()（）…—《》<>\[\]「」『』0-9]")
_ENDS = ("。", "！", "？", "…", "”", "」", ".", "!", "?")
_SIGN = ("敬上", "笔", "爸爸", "老爸", "安好")  # 信尾落款，别当小标题


def _bare(s: str, n: int = 14) -> bool:
    """像标题的短行：不长、一个标点都没有。"""
    return len(s) <= n and not _PUNCT.search(s)


def _read_txt(path: Path, chapter_titles=None, first_title: str = "") -> dict:
    """txt：认 utf-8 / gbk / big5。认章节三种办法：
    - 有「第一章」「Chapter 3」这类行就按它分；
    - 没有的话，没标点的短行、下一行是「……引语……」的当章名（《苏菲的世界》这种排法）；
    - chapter_titles 给了就按这串章名依次认（同名的小标题只认第一次出现的那个当章）。
    后两种情况下，夹在两段正文中间、没标点的短行当小标题（s）。first_title 给开头没写章名的第一章补一个。"""
    text = _decode_txt(path.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    lines = [x for x in (_clean(l) for l in text.split("\n")) if x]
    marked = sum(1 for x in lines if len(x) <= 40 and _CH_RE.match(x) and not x.endswith(_ENDS))
    mode = "titles" if chapter_titles else ("marked" if marked >= 2 else "bare")
    wanted = list(chapter_titles or [])
    chapters, cur, author = [], ([("h", first_title)] if first_title else []), ""
    for i, x in enumerate(lines):
        prev = lines[i - 1] if i else ""
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not chapters and not author and len(x) <= 40 and re.match(r"^(作者|著者|Author|Forfatter)\s*[:：]", x, re.I):
            author = re.split(r"[:：]", x, 1)[1].strip()
        if mode == "marked":
            is_ch = len(x) <= 40 and bool(_CH_RE.match(x)) and not x.endswith(_ENDS)
        elif mode == "titles":
            is_ch = x in wanted
            if is_ch:
                del wanted[:wanted.index(x) + 1]
        else:
            is_ch = _bare(x) and nxt.startswith(("…", "..."))
        if is_ch:
            if cur:
                chapters.append(cur)
            cur = [("h", x)]
        elif cur and cur[-1][0] == "h" and len(x) <= 80 and (x.startswith(("…", "...")) or (x.endswith("……") and len(x) <= 30)):
            cur.append(("e", x))  # 章名底下那句引语
        elif mode != "marked" and _bare(x, 12) and len(prev) >= 16 and len(nxt) >= 16 and not x.endswith(_SIGN):
            cur.append(("s", x))
        else:
            cur.append(("p", x))
    if cur:
        chapters.append(cur)
    return {"title": "", "author": author, "chapters": chapters, "cover": None}


class _Blocks(HTMLParser):
    """xhtml → [(h|p, 文字)]。块级标签断段，h1–h6 是标题，<br> 也断。"""
    BLOCK = {"p", "div", "li", "blockquote", "pre", "section", "article", "tr", "td", "th", "dd", "dt",
             "figcaption", "header", "footer", "aside", "table", "ul", "ol", "dl", "body", "nav"}
    HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP = {"head", "script", "style", "title", "svg", "math", "rt", "rp"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.skip, self.hdepth = [], [], 0, 0

    def flush(self):
        s = _clean("".join(self.buf))
        self.buf = []
        if s:
            self.out.append(("h" if self.hdepth else "p", s))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in ("br", "hr") or tag in self.BLOCK or tag in self.HEAD:
            self.flush()
        if tag in self.HEAD:
            self.hdepth += 1

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in self.BLOCK or tag in self.HEAD:
            self.flush()
        if tag in self.HEAD:
            self.hdepth = max(0, self.hdepth - 1)

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def _read_epub(path: Path) -> dict:
    """epub：按 OPF 的 spine 顺序一章一章抽段落；带出书名、作者、封面图。不往磁盘解压任何东西。"""
    z = zipfile.ZipFile(path)
    infos = z.infolist()
    if len(infos) > 20000 or sum(i.file_size for i in infos) > 800 * 1024 * 1024:
        raise ValueError("epub 解开太大了")
    names = {i.filename for i in infos}
    local = lambda t: t.rsplit("}", 1)[-1]
    cont = ET.fromstring(z.read("META-INF/container.xml"))
    rootfile = next(e.get("full-path") for e in cont.iter() if local(e.tag) == "rootfile")
    opf_dir = posixpath.dirname(rootfile)
    opf = ET.fromstring(z.read(rootfile))
    meta = {"title": "", "author": ""}
    manifest, spine, cover_id = {}, [], None
    for e in opf.iter():
        tag = local(e.tag)
        if tag == "title" and not meta["title"] and (e.text or "").strip():
            meta["title"] = e.text.strip()
        elif tag == "creator" and not meta["author"] and (e.text or "").strip():
            meta["author"] = e.text.strip()
        elif tag == "meta" and e.get("name") == "cover":
            cover_id = e.get("content")
        elif tag == "item" and e.get("id"):
            manifest[e.get("id")] = (e.get("href") or "", e.get("media-type") or "", e.get("properties") or "")
        elif tag == "itemref" and e.get("linear", "yes") != "no":
            spine.append(e.get("idref"))

    def resolve(href):
        return posixpath.normpath(posixpath.join(opf_dir, urllib.parse.unquote(href.split("#")[0])))

    chapters = []
    for idref in spine:
        it = manifest.get(idref)
        if not it:
            continue
        p = resolve(it[0])
        if p not in names or not ("html" in it[1] or p.lower().endswith((".xhtml", ".html", ".htm"))):
            continue
        parser = _Blocks()
        parser.feed(z.read(p).decode("utf-8", "replace"))
        parser.flush()
        if parser.out:
            chapters.append(parser.out)
    cover = None
    cid = next((k for k, v in manifest.items() if "cover-image" in v[2]), None) or cover_id
    if not (cid in manifest and manifest[cid][1].startswith("image/")):
        cid = next((k for k, v in manifest.items()
                    if v[1].startswith("image/") and "cover" in (k + v[0]).lower()), None)
    if cid:
        p = resolve(manifest[cid][0])
        if p in names:
            ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(manifest[cid][1], ".jpg")
            cover = (z.read(p), ext)
    return {**meta, "chapters": chapters, "cover": cover}


def _w(ch: str) -> float:
    if ch == " ":
        return 0.3
    return 1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.55


def _units(s: str) -> float:
    return sum(_w(c) for c in s)


def _need(t: str, s: str, cont: bool = False) -> float:
    if t == "h":
        return math.ceil(_units(s) / (LINE_UNITS * 0.8)) * 1.35 + 1.2
    if t == "s":  # 章里的小标题
        return math.ceil(_units(s) / (LINE_UNITS * 0.9)) * 1.2 + 0.8
    return math.ceil((_units(s) + (0 if cont else 2.0)) / LINE_UNITS) + PARA_GAP


def _split_to_fit(s: str, left: float, cont: bool, page_empty: bool):
    """一段放不下：按句子切出能塞进这页剩下 left 行的前半截。一句都塞不下且这页不空 → ("", s) 整段挪下一页。"""
    budget = math.floor(left - PARA_GAP) * LINE_UNITS - (0 if cont else 2.0)
    acc, u = "", 0.0
    for m in _SENT.finditer(s):
        x = m.group(0)
        if not x:
            continue
        xu = _units(x)
        if u + xu > budget:
            break
        acc += x
        u += xu
    if not acc:
        if not page_empty:
            return "", s
        i = 0
        for i, ch in enumerate(s):  # 一句话比一整页还长：按字硬切
            u += _w(ch)
            if u > budget:
                break
        acc = s[:max(1, i)]
    return acc, s[len(acc):].lstrip()


def _paginate(chapters):
    """[[(h|p, 文字)]…] → (pages: [[{t,s,c?}]…], 章节: [(标题, 页码)])。每章另起一页。"""
    pages, toc = [], []
    cur, used = [], 0.0

    def turn():
        nonlocal cur, used
        if cur:
            pages.append(cur)
        cur, used = [], 0.0

    for blocks in chapters:
        turn()
        title = next((s for t, s in blocks if t == "h"), None)
        if title:
            toc.append((title[:80], len(pages) + 1))
        for t, s in blocks:
            if t in ("h", "s"):
                need = _need(t, s)
                if used and used + need + 4 > PAGE_LINES:  # 标题别孤零零落在页底，底下至少留几行正文
                    turn()
                cur.append({"t": t, "s": s})
                used += need
                continue
            cont = False
            while s:
                need = _need("p", s, cont)
                left = PAGE_LINES - used
                if need <= left:
                    cur.append({"t": t, "s": s, **({"c": 1} if cont else {})})
                    used += need
                    break
                if left < 2.5:
                    turn()
                    continue
                head, rest = _split_to_fit(s, left, cont, not cur)
                if not head:
                    turn()
                    continue
                cur.append({"t": t, "s": head, **({"c": 1} if cont else {})})
                turn()
                s, cont = rest, True
    turn()
    return pages, toc


def _read_text_book(path: Path) -> dict:
    book = _read_epub(path) if path.suffix.lower() == ".epub" else _read_txt(path)
    pages, toc = _paginate(book["chapters"])
    if not pages:
        raise ValueError("里面没抽出文字")
    book["pages"], book["toc"] = pages, toc
    return book


def _category(c: str, kind: str) -> str:
    c = re.sub(r"\s+", " ", str(c or "")).strip()[:12]
    return c or ("阅读" if kind == "text" else "课本")


def _shelve_text(dest: Path, title: str, author: str, book: dict, category: str = "") -> int:
    conn = db()
    pages = book["pages"]
    cur = conn.execute(
        "INSERT INTO books(title, author, filename, pages, added_at, kind, category) VALUES(?,?,?,?,?, 'text', ?)",
        (title, author, dest.name, len(pages), now(), _category(category, "text")))
    book_id = cur.lastrowid
    conn.executemany("INSERT INTO pages(book_id, page_no, text) VALUES(?,?,?)",
                     [(book_id, i, "\n".join(b["s"] for b in blocks)) for i, blocks in enumerate(pages, 1)])
    conn.executemany("INSERT INTO layout(book_id, page_no, blocks) VALUES(?,?,?)",
                     [(book_id, i, json.dumps(blocks, ensure_ascii=False)) for i, blocks in enumerate(pages, 1)])
    conn.executemany("INSERT INTO chapters(book_id, idx, title, page_no) VALUES(?,?,?,?)",
                     [(book_id, i, t, p) for i, (t, p) in enumerate(book["toc"])])
    conn.commit()
    conn.close()
    if book.get("cover"):
        data, ext = book["cover"]
        out = SIDER_DIR / str(book_id)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"cover{ext}").write_bytes(data)
    return book_id


def _relayout(book_id: int, book: dict) -> str:
    """重排一本已经上架的 epub/txt（认章节的办法改了之后用）。划线按原句在新页里找回去，找不到的留在原页码。"""
    conn = db()
    old = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not old or old["kind"] != "text":
        conn.close()
        return "只能重排 epub / txt"
    pages = book["pages"]
    texts = ["\n".join(b["s"] for b in blocks) for blocks in pages]

    def find(snippet, fallback):
        snippet = (snippet or "")[:24]
        for k, t in enumerate(texts, 1):
            if snippet and snippet in t:
                return k
        return min(fallback, len(pages))

    prog_text = conn.execute("SELECT text FROM pages WHERE book_id=? AND page_no=?", (book_id, old["progress"] or 1)).fetchone()
    progress = find(prog_text["text"].split("\n")[0] if prog_text else "", old["progress"]) if old["progress"] else 0
    moved = []
    for m in conn.execute("SELECT * FROM marks WHERE book_id=?", (book_id,)).fetchall():
        p = find(m["quote"], m["page_no"])
        blocks = pages[p - 1]
        bi = next((i for i, b in enumerate(blocks) if m["quote"] in b["s"]), -1)
        if bi >= 0:
            a = blocks[bi]["s"].index(m["quote"])
            moved.append((p, bi, a, a + len(m["quote"]), m["id"]))
    for t in ("pages", "layout", "chapters"):
        conn.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
    conn.executemany("INSERT INTO pages(book_id, page_no, text) VALUES(?,?,?)",
                     [(book_id, i, t) for i, t in enumerate(texts, 1)])
    conn.executemany("INSERT INTO layout(book_id, page_no, blocks) VALUES(?,?,?)",
                     [(book_id, i, json.dumps(b, ensure_ascii=False)) for i, b in enumerate(pages, 1)])
    conn.executemany("INSERT INTO chapters(book_id, idx, title, page_no) VALUES(?,?,?,?)",
                     [(book_id, i, t, p) for i, (t, p) in enumerate(book["toc"])])
    conn.executemany("UPDATE marks SET page_no=?, block=?, start=?, end=? WHERE id=?", moved)
    conn.execute("UPDATE books SET pages=?, progress=? WHERE id=?", (len(pages), progress, book_id))
    conn.commit()
    conn.close()
    return f"重排好了：{len(pages)} 页、{len(book['toc'])} 章，进度 p{progress}"


COVER_EXT = (".jpg", ".png", ".webp", ".gif")


def _cover_url(book_id: int, kind: str) -> str:
    """封面：读者自己换的（mine.*）> epub 里带的（cover.*）> pdf 第一页。带 ?v= 修改时间，换了图不吃缓存。"""
    d = SIDER_DIR / str(book_id)
    for name in ("mine", "cover"):
        for ext in COVER_EXT:
            f = d / f"{name}{ext}"
            if f.is_file():
                return f"/sider/{book_id}/{name}{ext}?v={int(f.stat().st_mtime)}"
    if kind == "pdf" and (d / "s-001.jpg").is_file():
        return f"/sider/{book_id}/s-001.jpg"
    return ""


def _img_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"GIF8":
        return ".gif"
    return ""


def _unique_dest(name: str) -> Path:
    """书房里的归档路径：去掉危险字符，重名就加 -2、-3。"""
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", Path(name).name).strip() or "book.pdf"
    if not safe.lower().endswith(BOOK_EXT):
        safe += ".pdf"
    ext = Path(safe).suffix
    dest = BOOKS_DIR / safe
    k = 2
    while dest.exists():
        dest = BOOKS_DIR / f"{Path(safe).stem}-{k}{ext}"
        k += 1
    return dest


def _shelve(dest: Path, title: str, author: str, n: int, texts, category: str = "") -> int:
    conn = db()
    cur = conn.execute(
        "INSERT INTO books(title, author, filename, pages, added_at, category) VALUES(?,?,?,?,?,?)",
        (title, author, dest.name, n, now(), _category(category, "pdf")))
    book_id = cur.lastrowid
    conn.executemany("INSERT INTO pages(book_id, page_no, text) VALUES(?,?,?)",
                     [(book_id, i, txt) for i, txt in texts])
    conn.commit()
    conn.close()
    return book_id


def _render_pages(book_id: int, pdf: Path, n: int) -> None:
    """把每页切成 jpg：sider/<id>/s-001.jpg…（3 位补零，跟 les.html 对上）。
    一页一页来，切好的先能翻，shelf.json 的 ready 就是切了几页。"""
    out = SIDER_DIR / str(book_id)
    out.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        if (out / f"s-{i:03d}.jpg").is_file():
            continue
        try:
            subprocess.run(["nice", "-n", "10", "pdftoppm", "-jpeg", "-r", str(RENDER_DPI),
                            "-f", str(i), "-l", str(i), "-singlefile", str(pdf), str(out / f"s-{i:03d}")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        except Exception:
            pass


def _render_in_background(book_id: int, pdf: Path, n: int) -> None:
    threading.Thread(target=_render_pages, args=(book_id, pdf, n), daemon=True).start()


def _ready_pages(book_id: int) -> int:
    d = SIDER_DIR / str(book_id)
    return len(list(d.glob("s-*.jpg"))) if d.is_dir() else 0


def _withdraw(book_id: int) -> str:
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return f"书架上没有 [{book_id}]"
    for t in ("pages", "layout", "chapters", "marks", "talk", "turns"):
        conn.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
    conn.execute("DELETE FROM books WHERE id=?", (book_id,))
    conn.commit()
    conn.close()
    pdf = BOOKS_DIR / book["filename"]
    if pdf.is_file():
        pdf.unlink()
    shutil.rmtree(SIDER_DIR / str(book_id), ignore_errors=True)  # 页面图也一起清
    return f"下架了《{book['title']}》"


def _save_upload(fobj, dest: Path) -> int:
    fobj.seek(0)
    with dest.open("wb") as w:
        shutil.copyfileobj(fobj, w, 1 << 20)
    return dest.stat().st_size


def _cat_rank(c: str, rows) -> tuple:
    """分类的先后：课本、阅读在前，自己加的按第一本上架的顺序排。"""
    fixed = {"课本": 0, "阅读": 1}
    if c in fixed:
        return (fixed[c], 0)
    return (2, min(r["id"] for r in rows if r["category"] == c))


@mcp.tool()
def shelf() -> str:
    """看书架：按分类（课本 / 阅读 / 自己加的）分组，每本一行，[id] 书名 · 作者 · 格式 · 共几页 · 读到第几页。"""
    conn = db()
    rows = conn.execute("SELECT * FROM books ORDER BY id").fetchall()
    n_vocab = conn.execute("SELECT COUNT(*) FROM vocab").fetchone()[0]
    conn.close()
    if not rows:
        return "书架还是空的。用 import_book 导入第一本。"
    out, seen = [], None
    for r in sorted(rows, key=lambda r: (_cat_rank(r["category"], rows), r["id"])):
        if r["category"] != seen:
            seen = r["category"]
            out.append(f"—— {seen} ——")
        prog = f"读到 p{r['progress']}" if r["progress"] else "还没开始读"
        author = f" · {r['author']}" if r["author"] else ""
        fmt = Path(r["filename"]).suffix.lstrip(".").lower() or "pdf"
        out.append(f"[{r['id']}] {r['title']}{author} · {fmt} · 共 {r['pages']} 页 · {prog}")
    out.append(f"生词本：{n_vocab} 个词")
    return "\n".join(out)


@mcp.tool()
def import_book(path: str, title: str = "", author: str = "", category: str = "") -> str:
    """导入一本书：path 是服务器上的 pdf / epub / txt 路径，原件归档到书房。
    pdf 按页抽文字入库，后台一页页切成图给阅读器（大书要几分钟，切好的先能翻），扫描版抽不出文字会如实报告；
    epub / txt 抽成段落、按手机一屏分好页，阅读器里能划线。title 不填就用书里的书名或文件名。
    category 是书架上的分类（课本 / 阅读 / 自己起的名字），不填 pdf 进课本、epub/txt 进阅读。"""
    src = Path(path.strip()).expanduser()
    if not src.is_file():
        return f"没找到文件：{src}"
    if src.suffix.lower() in TEXT_EXT:
        try:
            book = _read_text_book(src)
        except Exception as e:
            return f"这本 {src.suffix} 读不出来：{e}"
        if src.parent.resolve() == BOOKS_DIR.resolve():
            dest = src
        else:
            dest = _unique_dest(src.name)
            dest.write_bytes(src.read_bytes())
        t = title.strip() or book["title"] or src.stem
        book_id = _shelve_text(dest, t, author.strip() or book["author"], book, category)
        return f"上架了。[{book_id}] {t} · 分成 {len(book['pages'])} 页 · {len(book['toc'])} 章"
    try:
        n, texts, empty, meta = _extract(src)
    except Exception as e:
        return f"这个 PDF 打不开：{e}"
    if n and empty > n * 0.8:
        return (f"抽了一遍：{n} 页里 {empty} 页没有文字层，八成是扫描版。"
                "先不入库——扫描版要过 OCR 才有用，先过一遍 OCR 再来。")
    if src.parent.resolve() == BOOKS_DIR.resolve():
        dest = src
    else:
        dest = _unique_dest(src.name)
        dest.write_bytes(src.read_bytes())
    t = title.strip() or src.stem
    book_id = _shelve(dest, t, author.strip(), n, texts, category)
    _render_in_background(book_id, dest, n)
    note = f"（其中 {empty} 页没有文字，可能是图或空页）" if empty else ""
    return f"上架了。[{book_id}] {t} · 共 {n} 页{note}，正在后台切页面图。"


@mcp.tool()
def read_pages(book_id: int, start: int, end: int = 0) -> str:
    """取一本书的第 start 到 end 页原文（end 默认等于 start，一次最多 10 页）。"""
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return f"书架上没有 [{book_id}]"
    end = end or start
    if end < start:
        start, end = end, start
    end = min(end, start + 9, book["pages"])
    rows = conn.execute(
        "SELECT page_no, text FROM pages WHERE book_id=? AND page_no BETWEEN ? AND ?"
        " ORDER BY page_no", (book_id, start, end)).fetchall()
    conn.close()
    if not rows:
        return f"《{book['title']}》没有 p{start}–p{end}（全书共 {book['pages']} 页）"
    out = [f"《{book['title']}》 p{start}–p{end}"]
    for r in rows:
        out.append(f"--- p{r['page_no']} ---\n{r['text'] or '（这页没有文字）'}")
    return "\n".join(out)


@mcp.tool()
def toc(book_id: int) -> str:
    """看一本书的目录（pdf 取自带书签，没有书签的取不到；epub / txt 取认出来的章节）。"""
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    conn.close()
    if not book:
        return f"书架上没有 [{book_id}]"
    if book["kind"] == "text":
        conn = db()
        rows = conn.execute("SELECT title, page_no FROM chapters WHERE book_id=? ORDER BY idx", (book_id,)).fetchall()
        conn.close()
        if not rows:
            return f"《{book['title']}》没认出章节"
        return f"《{book['title']}》目录\n" + "\n".join(f"{r['title']} · p{r['page_no']}" for r in rows[:200])
    pdf = BOOKS_DIR / book["filename"]
    if not pdf.is_file():
        return "原件不在书房里了，目录取不到"
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf))
        out = []

        def walk(items, depth):
            for it in items:
                if isinstance(it, list):
                    walk(it, depth + 1)
                else:
                    try:
                        pno = reader.get_destination_page_number(it) + 1
                        out.append(f"{'  ' * depth}{it.title} · p{pno}")
                    except Exception:
                        pass

        walk(reader.outline, 0)
    except Exception as e:
        return f"目录读不出来：{e}"
    if not out:
        return f"《{book['title']}》的 PDF 没带书签，翻页用 find 搜关键词定位吧"
    return f"《{book['title']}》目录\n" + "\n".join(out[:120])


@mcp.tool()
def find(book_id: int, query: str, limit: int = 10) -> str:
    """在一本书里搜关键词，返回命中页码和前后一小段。"""
    q = query.strip()
    if not q:
        return "关键词不能为空"
    conn = db()
    book = conn.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return f"书架上没有 [{book_id}]"
    rows = conn.execute(
        "SELECT page_no, text FROM pages WHERE book_id=? AND text LIKE ? ORDER BY page_no LIMIT ?",
        (book_id, f"%{q}%", max(1, min(limit, 30)))).fetchall()
    conn.close()
    if not rows:
        return f"《{book['title']}》里没搜到「{q}」"
    out = [f"《{book['title']}》搜「{q}」："]
    for r in rows:
        pos = r["text"].lower().find(q.lower())
        snippet = r["text"][max(0, pos - 40):pos + len(q) + 40].replace("\n", " ")
        out.append(f"p{r['page_no']} · …{snippet}…")
    return "\n".join(out)


@mcp.tool()
def mark(book_id: int, page: int) -> str:
    """记进度：读到第几页了。下次 shelf 能看到，接着读。"""
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return f"书架上没有 [{book_id}]"
    page = max(0, min(page, book["pages"]))
    conn.execute("UPDATE books SET progress=? WHERE id=?", (page, book_id))
    conn.commit()
    conn.close()
    return f"记住了，《{book['title']}》读到 p{page}"


@mcp.tool()
def vocab_add(word: str, meaning: str = "", example: str = "",
              book_id: int = 0, page_no: int = 0, note: str = "") -> str:
    """往生词本记一个词：word 必填，meaning 释义、example 原文例句、
    book_id/page_no 出处、note 备注都可选。"""
    word = word.strip()
    if not word:
        return "word 不能是空的"
    conn = db()
    dup = conn.execute("SELECT id FROM vocab WHERE word=?", (word,)).fetchone()
    cur = conn.execute(
        "INSERT INTO vocab(word, meaning, example, book_id, page_no, note, added_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (word, meaning.strip(), example.strip(), book_id, page_no, note.strip(), now()))
    conn.commit()
    conn.close()
    extra = f"（之前记过一次 [v{dup['id']}]，又遇到了说明是重点）" if dup else ""
    return f"记进生词本了。[v{cur.lastrowid}] {word}{extra}"


@mcp.tool()
def vocab(query: str = "", limit: int = 30) -> str:
    """翻生词本：不带参数看最近记的，query 按词或释义过滤。"""
    conn = db()
    if query.strip():
        q = f"%{query.strip()}%"
        rows = conn.execute(
            "SELECT * FROM vocab WHERE word LIKE ? OR meaning LIKE ? ORDER BY id DESC LIMIT ?",
            (q, q, max(1, min(limit, 100)))).fetchall()
    else:
        rows = conn.execute("SELECT * FROM vocab ORDER BY id DESC LIMIT ?",
                            (max(1, min(limit, 100)),)).fetchall()
    conn.close()
    if not rows:
        return "生词本里还没有词。" if not query.strip() else f"没记过「{query}」"
    out = []
    for r in rows:
        line = f"[v{r['id']}] {r['word']}"
        if r["meaning"]:
            line += f" — {r['meaning']}"
        if r["example"]:
            line += f"\n    „{r['example']}“"
        src = []
        if r["book_id"]:
            src.append(f"书[{r['book_id']}]")
        if r["page_no"]:
            src.append(f"p{r['page_no']}")
        if src:
            line += f"  ({' '.join(src)})"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def vocab_erase(vocab_id: int) -> str:
    """从生词本删掉一个词（比如已经烂熟了）。"""
    conn = db()
    row = conn.execute("SELECT word FROM vocab WHERE id=?", (vocab_id,)).fetchone()
    if not row:
        conn.close()
        return f"生词本里没有 [v{vocab_id}]"
    conn.execute("DELETE FROM vocab WHERE id=?", (vocab_id,))
    conn.commit()
    conn.close()
    return f"划掉了 [v{vocab_id}] {row['word']}，算你学会了"


@mcp.tool()
def mistake_add(question: str, right: str, wrong: str = "", note: str = "") -> str:
    """往错题本记一条：question 题目/句子，wrong 读者的答案，right 正确答案，note 讲解要点。"""
    question = question.strip()
    if not question or not right.strip():
        return "question 和 right 都不能是空的"
    conn = db()
    cur = conn.execute(
        "INSERT INTO mistakes(question, wrong, right, note, created_at) VALUES(?,?,?,?,?)",
        (question, wrong.strip(), right.strip(), note.strip(), now()))
    conn.commit()
    conn.close()
    return f"记进错题本了。[f{cur.lastrowid}]"


@mcp.tool()
def mistakes(cleared: bool = False, limit: int = 30) -> str:
    """翻错题本。默认只看还没掌握的；cleared=True 看已划掉的故纸堆。"""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM mistakes WHERE cleared=? ORDER BY id DESC LIMIT ?",
        (1 if cleared else 0, max(1, min(limit, 100)))).fetchall()
    conn.close()
    if not rows:
        return "故纸堆是空的。" if cleared else "错题本是空的，很好。"
    out = []
    for r in rows:
        line = f"[f{r['id']}] {r['question']}"
        if r["wrong"]:
            line += f"\n    答：{r['wrong']}"
        line += f"\n    正解：{r['right']}"
        if r["note"]:
            line += f"\n    要点：{r['note']}"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def mistake_clear(mistake_id: int) -> str:
    """掌握了就把这条错题划掉（进故纸堆，不删）。"""
    conn = db()
    row = conn.execute("SELECT question FROM mistakes WHERE id=?", (mistake_id,)).fetchone()
    if not row:
        conn.close()
        return f"错题本里没有 [f{mistake_id}]"
    conn.execute("UPDATE mistakes SET cleared=1 WHERE id=?", (mistake_id,))
    conn.commit()
    conn.close()
    return f"划掉了 [f{mistake_id}]，这题会了"


@mcp.tool()
def exercise_add(question: str, answer: str = "", note: str = "") -> str:
    """出一道练习题：question 题目，answer 参考答案（可先不填），note 提示。"""
    question = question.strip()
    if not question:
        return "question 不能是空的"
    conn = db()
    cur = conn.execute(
        "INSERT INTO exercises(question, answer, note, created_at) VALUES(?,?,?,?)",
        (question, answer.strip(), note.strip(), now()))
    conn.commit()
    conn.close()
    return f"出好了。[x{cur.lastrowid}]"


@mcp.tool()
def exercises(status: str = "open", limit: int = 30) -> str:
    """翻练习题。status=open(没做的)/done(做过的)/all。"""
    status = status.strip().lower()
    conn = db()
    if status == "all":
        rows = conn.execute("SELECT * FROM exercises ORDER BY id DESC LIMIT ?",
                            (max(1, min(limit, 100)),)).fetchall()
    else:
        if status not in ("open", "done"):
            conn.close()
            return "status 要是 open/done/all"
        rows = conn.execute("SELECT * FROM exercises WHERE status=? ORDER BY id DESC LIMIT ?",
                            (status, max(1, min(limit, 100)))).fetchall()
    conn.close()
    if not rows:
        return "没有这类练习题。"
    out = []
    for r in rows:
        line = f"[x{r['id']}]({r['status']}) {r['question']}"
        if r["status"] == "done":
            line += f"\n    答：{r['her_answer'] or '（空）'}"
            if r["answer"]:
                line += f"\n    参考：{r['answer']}"
        if r["note"]:
            line += f"\n    提示：{r['note']}"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
def exercise_done(exercise_id: int, her_answer: str = "") -> str:
    """做完一道题：记下读者的答案，标成 done。答案对错的点评在对话里给，不存这。"""
    conn = db()
    row = conn.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
    if not row:
        conn.close()
        return f"没有 [x{exercise_id}] 这道题"
    conn.execute("UPDATE exercises SET her_answer=?, status='done' WHERE id=?",
                 (her_answer.strip(), exercise_id))
    conn.commit()
    conn.close()
    ref = f"参考答案：{row['answer']}" if row["answer"] else "这题没存参考答案"
    return f"记下了。[x{exercise_id}] 答：{her_answer.strip() or '（空）'}。{ref}"


@mcp.tool()
def exercise_erase(exercise_id: int) -> str:
    """删一道练习题。"""
    conn = db()
    row = conn.execute("SELECT question FROM exercises WHERE id=?", (exercise_id,)).fetchone()
    if not row:
        conn.close()
        return f"没有 [x{exercise_id}] 这道题"
    conn.execute("DELETE FROM exercises WHERE id=?", (exercise_id,))
    conn.commit()
    conn.close()
    return f"删了 [x{exercise_id}]"


@mcp.tool()
def relayout(book_id: int, chapter_titles: str = "", first_title: str = "") -> str:
    """重排一本 txt / epub。txt 认不出章节时用：chapter_titles 填这本书的章名（空格或换行分开，按顺序），
    first_title 给开头没写章名的第一章补一个名字。进度和划线会在新页里找回去。"""
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    conn.close()
    if not book or book["kind"] != "text":
        return "只能重排 epub / txt"
    path = BOOKS_DIR / book["filename"]
    if not path.is_file():
        return "原件不在了，重排不了"
    titles = [t for t in re.split(r"[\s,，、]+", chapter_titles) if t]
    if path.suffix.lower() == ".epub":
        b = _read_epub(path)
    else:
        b = _read_txt(path, chapter_titles=titles or None, first_title=first_title.strip())
    b["pages"], b["toc"] = _paginate(b["chapters"])
    if not b["pages"]:
        return "重排出来是空的，没动"
    return _relayout(book_id, b)


@mcp.tool()
def withdraw(book_id: int) -> str:
    """把一本书下架：删掉书和它的所有页面、页面图（生词本里出自它的词保留）。
    删之前先 shelf 确认，原件也会从书房移除。"""
    return _withdraw(book_id)


@mcp.tool()
def rename_book(book_id: int, title: str = "", author: str = "", category: str = "") -> str:
    """改书名/作者/分类（网页上传的书标题默认是文件名，不像样就用这个改）。留空的不动。
    category 是书架上的分类：课本 / 阅读 / 自己起的名字。"""
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return f"书架上没有 [{book_id}]"
    t, a = title.strip() or book["title"], author.strip() or book["author"]
    c = _category(category, book["kind"]) if category.strip() else book["category"]
    conn.execute("UPDATE books SET title=?, author=?, category=? WHERE id=?", (t, a, c, book_id))
    conn.commit()
    conn.close()
    return f"改好了。[{book_id}] {t}" + (f" · {a}" if a else "") + f" · {c}"


@mcp.custom_route("/shelf.json", methods=["GET"])
async def shelf_json(request: Request) -> JSONResponse:
    conn = db()
    books = [dict(r) for r in conn.execute("SELECT * FROM books ORDER BY id")]
    n_marks = dict(conn.execute("SELECT book_id, COUNT(*) FROM marks GROUP BY book_id").fetchall())
    for b in books:
        # 切好的页面图数，< pages 就是还在排版；epub/txt 不切图，一上架就能读
        b["ready"] = b["pages"] if b["kind"] == "text" else _ready_pages(b["id"])
        b["format"] = Path(b["filename"]).suffix.lstrip(".").lower() or "pdf"
        b["cover"] = _cover_url(b["id"], b["kind"])
        b["marks"] = n_marks.get(b["id"], 0)
    words = [dict(r) for r in conn.execute("SELECT * FROM vocab ORDER BY id DESC")]
    feil = [dict(r) for r in conn.execute("SELECT * FROM mistakes ORDER BY id DESC")]
    ovelser = [dict(r) for r in conn.execute("SELECT * FROM exercises ORDER BY id DESC")]
    conn.close()
    return JSONResponse({"books": books, "vocab": words,
                         "mistakes": feil, "exercises": ovelser})


def _page(name: str) -> HTMLResponse:
    """页面里 <!--BOOT--> 换成配置（书房叫什么、AI 叫什么、返回链接、背景图、要不要学习那几间）。"""
    boot = {"title": CONF.get("title") or "书房", "subtitle": CONF.get("subtitle") or "Bibliotek",
            "ai": AI, "reader": READER, "back": CONF.get("back_url") or "",
            "bg": CONF.get("background") or "/assets/bg.webp", "study": bool(CONF.get("study_rooms")),
            "alphabet": CONF.get("alphabet") or "abcdefghijklmnopqrstuvwxyz", "mode": MODE}
    html = (BASE / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<!--BOOT-->", "<script>window.BOOT=" + json.dumps(boot, ensure_ascii=False)
                                     .replace("</", "<\\/") + "</script>"), headers={"Cache-Control": "no-store"})


def _static(root: Path, rel: str, cache: str):
    """自己发静态文件（不靠 nginx）：防路径穿越。"""
    f = (root / rel).resolve()
    if not str(f).startswith(str(root.resolve()) + os.sep) or not f.is_file():
        return Response(status_code=404)
    return FileResponse(f, headers={"Cache-Control": cache})


@mcp.custom_route("/", methods=["GET"])
async def home_page(request: Request) -> HTMLResponse:
    return _page("shelf.html")


@mcp.custom_route("/les", methods=["GET"])
async def les_page(request: Request) -> HTMLResponse:
    return _page("les.html")


@mcp.custom_route("/assets/{path:path}", methods=["GET"])
async def assets(request: Request):
    return _static(BASE / "assets", request.path_params["path"], "public, max-age=86400")


@mcp.custom_route("/sider/{path:path}", methods=["GET"])
async def sider(request: Request):
    return _static(SIDER_DIR, request.path_params["path"], "public, max-age=2592000")


@mcp.custom_route("/api/mark", methods=["POST"])
async def api_mark(request: Request) -> JSONResponse:
    try:
        d = await request.json()
        book_id, page = int(d["book_id"]), int(d["page"])
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    conn = db()
    book = conn.execute("SELECT pages, progress FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    page = max(0, min(page, book["pages"]))
    # 只往前记，不因回翻旧页倒退进度
    if page > book["progress"]:
        conn.execute("UPDATE books SET progress=? WHERE id=?", (page, book_id))
        conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/upload", methods=["POST"])
async def api_upload(request: Request) -> JSONResponse:
    """书架页上传书：pdf 存进书房 → 抽文字入库 → 后台切图（扫描版也收，能翻，只是 AI 搜不了字）；
    epub / txt 抽段落、分好页就能读。"""
    form = await request.form()
    files = [f for f in form.getlist("file") if f is not None and getattr(f, "filename", None)]
    if not files:
        return JSONResponse({"ok": False, "error": "没选文件"}, status_code=400)
    added, errors = [], []
    for f in files:
        name = Path(f.filename or "").name
        if not name.lower().endswith(BOOK_EXT):
            errors.append(f"{name}：只收 pdf / epub / txt")
            continue
        dest = _unique_dest(name)
        try:
            size = await asyncio.to_thread(_save_upload, f.file, dest)
        except Exception as e:
            dest.unlink(missing_ok=True)
            errors.append(f"{name}：没存下来（{str(e)[:60]}）")
            continue
        finally:
            try:
                await f.close()
            except Exception:
                pass
        if size > MAX_UPLOAD:
            dest.unlink(missing_ok=True)
            errors.append(f"{name}：超过 {MAX_UPLOAD // (1024 * 1024)}M 了")
            continue
        if dest.suffix.lower() in TEXT_EXT:
            try:
                book = await asyncio.to_thread(_read_text_book, dest)
            except Exception as e:
                dest.unlink(missing_ok=True)
                errors.append(f"{name}：读不出来（{str(e)[:60]}）")
                continue
            title = (str(form.get("title") or "")).strip()[:120] or book["title"][:120] or dest.stem
            author = (str(form.get("author") or "")).strip()[:120] or book["author"][:120]
            book_id = await asyncio.to_thread(_shelve_text, dest, title, author, book, str(form.get("category") or ""))
            added.append({"id": book_id, "title": title, "pages": len(book["pages"]), "scanned": False})
            continue
        try:
            with dest.open("rb") as fh:
                if fh.read(4) != b"%PDF":
                    raise ValueError("不是 pdf")
            n, texts, empty, meta = await asyncio.to_thread(_extract, dest)
            if not n:
                raise ValueError("一页都没有")
        except Exception as e:
            dest.unlink(missing_ok=True)
            errors.append(f"{name}：这个 pdf 打不开（{str(e)[:60]}）")
            continue
        scanned = empty > n * 0.8
        # 页面上填的书名/作者优先；没填才看 pdf 元数据，再不行用文件名
        title = (str(form.get("title") or "")).strip()[:120] or _guess_title(meta.get("title", ""), dest.stem)
        author = (str(form.get("author") or "")).strip()[:120] or meta.get("author", "")
        book_id = _shelve(dest, title, author, n, texts, str(form.get("category") or ""))
        _render_in_background(book_id, dest, n)
        added.append({"id": book_id, "title": title, "pages": n, "scanned": scanned})
    return JSONResponse({"ok": bool(added), "added": added, "errors": errors},
                        status_code=200 if added else 400)


@mcp.custom_route("/api/cover", methods=["POST"])
async def api_cover(request: Request) -> JSONResponse:
    """给一本书换封面（页面上先缩到 900px 再传）；remove=1 用回原来的。"""
    form = await request.form()
    try:
        book_id = int(form.get("book_id") or 0)
    except ValueError:
        book_id = 0
    conn = db()
    book = conn.execute("SELECT kind FROM books WHERE id=?", (book_id,)).fetchone()
    conn.close()
    if not book:
        return JSONResponse({"ok": False, "error": "没有这本书"}, status_code=404)
    d = SIDER_DIR / str(book_id)
    data, ext = b"", ""
    if not form.get("remove"):
        f = form.get("file")
        data = await f.read() if f is not None and hasattr(f, "read") else b""
        if len(data) > 15 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "图太大了"}, status_code=413)
        ext = _img_ext(data)
        if not ext:
            return JSONResponse({"ok": False, "error": "只收 jpg / png / webp / gif"}, status_code=400)
    d.mkdir(parents=True, exist_ok=True)
    for e in COVER_EXT:
        (d / f"mine{e}").unlink(missing_ok=True)
    if data:
        (d / f"mine{ext}").write_bytes(data)
    return JSONResponse({"ok": True, "cover": _cover_url(book_id, book["kind"])})


@mcp.custom_route("/api/rename", methods=["POST"])
async def api_rename(request: Request) -> JSONResponse:
    """页面上改书名/作者。"""
    try:
        d = await request.json()
        book_id = int(d["book_id"])
        title = str(d.get("title") or "").strip()[:120]
        author = str(d.get("author") or "").strip()[:120]
        category = str(d.get("category") or "")
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    if not title:
        return JSONResponse({"ok": False, "error": "书名不能空"}, status_code=400)
    conn = db()
    book = conn.execute("SELECT kind, category FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    category = _category(category, book["kind"]) if category.strip() else book["category"]
    conn.execute("UPDATE books SET title=?, author=?, category=? WHERE id=?", (title, author, category, book_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---------------- 陪读：书页旁的小窗 ----------------
# 读者划线 / 点「一起读这页」/ 在小窗里说话 → talk 表里记一条（turn_id 空）→ 投递线程攒几秒合成一条，
# 以「[书房]《书名》（书 N）」开头送给 AI（怎么送看 config.json 的 companion.mode）→ AI 的回复存成 reply，
# 阅读器轮询拿回去，贴在那条线旁边、小窗里。翻页不叫 AI；整页原文只有「一起读」才带（api 模式默认每条都带）。
MERGE_QUIET = 3.5    # 连着划：停手 3.5 秒再送，合成一条
MERGE_MAX = 12.0     # 最早那条最多等 12 秒
BUSY_WAIT = 12 * 60  # AI 正忙（409/429/503 之类）最多等这么久
_wake = threading.Event()
MODE = str(COMP.get("mode") or "off").strip()
ATTACH_PAGE = COMP.get("attach_page")
if ATTACH_PAGE is None:
    ATTACH_PAGE = MODE == "api"  # api 模式的 AI 没法自己翻书，每次带上那一页
HINT = MODE != "api" if COMP.get("hint") is None else bool(COMP.get("hint"))  # 每条末尾提醒一句回复会贴在书页旁（AI 那边写好规矩以后可以关；api 模式写在系统提示里了）


def _env_value(v):
    """配置里写 "env:变量名" 就从环境变量取，key 别直接写进 config.json。"""
    return os.environ.get(v[4:], "") if isinstance(v, str) and v.startswith("env:") else v


def _kv(key: str, value=None):
    conn = db()
    if value is None:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else None
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()
    return value


def _send_http(text: str, book) -> tuple:
    """POST 到一个网址。format=anthropic：发 Messages API 格式（跟 claude-code-p-bridge 这类 bridge 对得上）；
    format=simple：发 {"text", "book_id", "book_title"}，回 {"reply": "..."} 或者直接回纯文字。"""
    c = COMP.get("http") or {}
    if not c.get("url"):
        return "fail", "companion.http.url 没填"
    fmt = c.get("format") or "anthropic"
    headers = {"content-type": "application/json", **{k: _env_value(v) for k, v in (c.get("headers") or {}).items()}}
    if fmt == "anthropic":
        body = {"model": c.get("model") or "claude-opus-5", "max_tokens": int(c.get("max_tokens") or 4096),
                "messages": [{"role": "user", "content": text}]}
    else:
        body = {"text": text, "book_id": book["id"], "book_title": book["title"]}
    req = urllib.request.Request(c["url"], data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=float(c.get("timeout") or 900)) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ("busy" if e.code in (409, 429, 503, 529) else "fail"), f"{e.code} {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return "busy", str(e)[:200]
    try:
        j = json.loads(raw)
    except Exception:
        return "ok", raw.strip()
    if fmt == "anthropic":
        return "ok", "\n\n".join(x.get("text", "") for x in (j.get("content") or []) if x.get("type") == "text").strip()
    return "ok", str(j.get(c.get("reply_field") or "reply") or "").strip()


def _send_claude_p(text: str, book) -> tuple:
    """claude -p 接着一个 Claude Code 会话说：session_id 填你平时那个会话，陪读的就是它本人；
    不填就每本书自己开一个会话（第一次 --session-id，以后 --resume），记得这本书聊过什么。"""
    import uuid
    c = COMP.get("claude_p") or {}
    sid, first = (c.get("session_id") or "").strip(), False
    if not sid:
        sid = _kv(f"session:{book['id']}")
        if not sid:
            sid, first = str(uuid.uuid4()), True
    args = [c.get("bin") or "claude", "-p", text, "--output-format", "json"]
    args += ["--session-id", sid] if first else ["--resume", sid]
    if c.get("attach_mcp", True):  # 把书房自己的 MCP 挂上，它能 read_pages 翻书
        name = CONF.get("mcp_name") or "bibliotek"
        args += ["--mcp-config", json.dumps({"mcpServers": {name: {"type": "http", "url": f"http://127.0.0.1:{PORT}/mcp"}}}),
                 "--allowedTools", f"mcp__{name}"]
    args += [str(a) for a in (c.get("args") or [])]
    try:
        r = subprocess.run(args, cwd=c.get("cwd") or None, capture_output=True, text=True,
                           timeout=float(c.get("timeout") or 600))
    except subprocess.TimeoutExpired:
        return "fail", "claude -p 超时了"
    except FileNotFoundError:
        return "fail", "找不到 claude 命令：config 里 companion.claude_p.bin 写完整路径"
    try:
        j = json.loads(r.stdout)
    except Exception:
        return "fail", (r.stderr or r.stdout or f"退出码 {r.returncode}")[-200:]
    if j.get("is_error"):
        return "fail", str(j.get("result") or r.stderr)[-200:]
    if first:
        _kv(f"session:{book['id']}", sid)
    return "ok", str(j.get("result") or "").strip()


def _send_command(text: str, book) -> tuple:
    """跑一个你自己的命令（比如调 Agent SDK 的小脚本）：消息从 stdin 进，回复从 stdout 出。
    退出码 75 表示「正忙，等会儿再来」，别的非 0 算失败。"""
    import shlex
    c = COMP.get("command") or {}
    cmd = c.get("cmd")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if not cmd:
        return "fail", "companion.command.cmd 没填"
    env = {**os.environ, "LES_BOOK_ID": str(book["id"]), "LES_BOOK_TITLE": book["title"]}
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, cwd=BASE, env=env,
                           timeout=float(c.get("timeout") or 600))
    except subprocess.TimeoutExpired:
        return "fail", "命令超时了"
    except FileNotFoundError:
        return "fail", f"找不到命令：{cmd[0]}"
    if r.returncode == 75:
        return "busy", r.stderr[-200:]
    if r.returncode != 0:
        return "fail", (r.stderr or r.stdout or f"退出码 {r.returncode}")[-200:]
    return "ok", r.stdout.strip()


API_SYSTEM = (
    "你在陪{reader}读书。{reader}在书房的网页阅读器里划线、点「一起读这页」或在书页旁的小窗里跟你说话，"
    "消息以「[书房]《书名》」开头，常常带着那一页的原文。你的回复会贴在那句话旁边当批注，也显示在书页旁的小窗里，"
    "所以回得短，像坐在旁边小声说，一两段就够：讲清意思、背景、为什么绕，用{reader}熟悉的事打比方；"
    "别复述原文，别掉书袋。")


def _send_api(text: str, book, turn_id: int) -> tuple:
    """直接调 Claude API（官方 anthropic SDK）。这本书之前聊过的几轮一起带上，所以记得刚才说过什么。"""
    try:
        import anthropic
    except ImportError:
        return "fail", "没装 anthropic：venv/bin/pip install anthropic"
    c = COMP.get("api") or {}
    kw = {}
    if c.get("base_url"):
        kw["base_url"] = c["base_url"]
    if c.get("api_key"):
        kw["api_key"] = _env_value(c["api_key"])
    client = anthropic.Anthropic(**kw)
    conn = db()
    hist = conn.execute(
        "SELECT u.sent, (SELECT text FROM talk WHERE turn_id=u.id AND who='ai' ORDER BY id LIMIT 1) AS reply "
        "FROM turns u WHERE u.book_id=? AND u.status='done' AND u.id<? ORDER BY u.id DESC LIMIT ?",
        (book["id"], turn_id, int(c.get("history_turns") or 20))).fetchall()
    conn.close()
    messages = []
    for h in reversed(hist):
        if h["sent"] and h["reply"]:
            messages += [{"role": "user", "content": h["sent"]}, {"role": "assistant", "content": h["reply"]}]
    messages.append({"role": "user", "content": text})
    system = API_SYSTEM.format(reader=READER)
    if c.get("system_file"):
        try:
            system = (BASE / c["system_file"]).read_text(encoding="utf-8").strip() + "\n\n" + system
        except Exception:
            pass
    req = dict(model=c.get("model") or "claude-opus-5", max_tokens=int(c.get("max_tokens") or 16000),
               system=system, messages=messages, cache_control={"type": "ephemeral"})
    try:
        try:
            if c.get("fallbacks", True):  # 被安全分类器拦下时服务器自己换个模型接着回（官方 API 才有，中转不认就关掉）
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **req)
            else:
                resp = client.messages.create(**req)
        except TypeError:  # SDK 太旧不认这些参数：不带它们再来一次
            req.pop("cache_control", None)
            resp = client.messages.create(**req)
    except anthropic.RateLimitError as e:
        return "busy", str(e)[:200]
    except anthropic.APIStatusError as e:
        return ("busy" if e.status_code >= 500 else "fail"), f"{e.status_code} {str(e)[:200]}"
    except anthropic.APIConnectionError as e:
        return "busy", str(e)[:200]
    if resp.stop_reason == "refusal":
        return "fail", "这一条被模型拒答了"
    return "ok", "\n\n".join(b.text for b in resp.content if b.type == "text").strip()


def _deliver(text: str, book, turn_id: int) -> tuple:
    if MODE == "http":
        return _send_http(text, book)
    if MODE == "claude_p":
        return _send_claude_p(text, book)
    if MODE == "command":
        return _send_command(text, book)
    if MODE == "api":
        return _send_api(text, book, turn_id)
    return "fail", "没接 AI（config.json 里 companion.mode 是 off）"


def _page_blocks(conn, book_id: int, page: int):
    row = conn.execute("SELECT blocks FROM layout WHERE book_id=? AND page_no=?", (book_id, page)).fetchone()
    return json.loads(row["blocks"]) if row else None


def _mark_context(conn, mark) -> str:
    """划的那句前后各带一句，一共不超过百来字。"""
    blocks = _page_blocks(conn, mark["book_id"], mark["page_no"])
    if not blocks or not (0 <= mark["block"] < len(blocks)):
        return ""
    s = blocks[mark["block"]]["s"]
    a, b = mark["start"], mark["end"]
    before = [m.group(0) for m in _SENT.finditer(s[:a]) if m.group(0).strip()]
    after = [m.group(0) for m in _SENT.finditer(s[b:]) if m.group(0).strip()]
    pre = (before[-1] if before else "").strip()[-50:]
    post = (after[0] if after else "").strip()[:50]
    if not pre and not post:
        return ""
    return f"…{pre}【{mark['quote']}】{post}…"


def _page_text(conn, book_id: int, page: int, limit: int = 2600) -> str:
    pg = conn.execute("SELECT text FROM pages WHERE book_id=? AND page_no=?", (book_id, page)).fetchone()
    txt = (pg["text"] if pg else "").strip()
    return txt[:limit] + "…（后面没带）" if len(txt) > limit else txt


def _compose(conn, book, rows) -> str:
    lines = [f"[书房]《{book['title']}》（书 {book['id']}）"]
    shown = set()
    for r in rows:
        p = r["page_no"]
        if r["kind"] == "together":
            lines.append(f"第{p}页 {READER}点了「一起读」，这页原文：\n{_page_text(conn, book['id'], p) or '（这页没有文字）'}")
            shown.add(p)
        elif r["kind"] == "mark":
            mark = conn.execute("SELECT * FROM marks WHERE id=?", (r["ref"],)).fetchone()
            ctx = _mark_context(conn, mark) if mark else ""
            lines.append(f"第{p}页 {READER}划了：「{r['text']}」" + (f"\n  上下文：{ctx}" if ctx else ""))
        else:
            lines.append(f"第{p}页 {READER}说：{r['text']}")
    if ATTACH_PAGE:
        for p in sorted({r["page_no"] for r in rows} - shown):
            if p:
                lines.append(f"（第{p}页原文：\n{_page_text(conn, book['id'], p)}）")
    if HINT:
        lines.append("（这条来自书房：回复会贴在书页旁边，短一点，像坐在旁边小声说。）")
    return "\n".join(lines)


def _next_batch():
    """攒好了就返回 (book, rows)；读者还在连着划就等停手；没东西返回 None。"""
    while True:
        conn = db()
        rows = conn.execute("SELECT * FROM talk WHERE who='her' AND turn_id IS NULL ORDER BY id").fetchall()
        if not rows:
            conn.close()
            return None
        book_id = rows[0]["book_id"]
        rows = [r for r in rows if r["book_id"] == book_id]
        book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        conn.close()
        if not book:  # 书下架了，这几条没处送
            conn = db()
            conn.execute("DELETE FROM talk WHERE who='her' AND turn_id IS NULL AND book_id=?", (book_id,))
            conn.commit()
            conn.close()
            continue
        t = time.time()
        if t - rows[-1]["ts"] < MERGE_QUIET and t - rows[0]["ts"] < MERGE_MAX:
            time.sleep(0.8)
            continue
        return book, rows


def _send_batch(book, rows) -> None:
    conn = db()
    text = _compose(conn, book, rows)
    cur = conn.execute("INSERT INTO turns(book_id, status, sent, created_at) VALUES(?, 'sending', ?, ?)",
                       (book["id"], text, now()))
    turn_id = cur.lastrowid
    conn.executemany("UPDATE talk SET turn_id=? WHERE id=?", [(turn_id, r["id"]) for r in rows])
    conn.commit()
    conn.close()
    t0, tries = time.time(), 0
    while True:
        kind, msg = _deliver(text, book, turn_id)
        if kind == "ok":
            conn = db()
            if msg:
                conn.execute("INSERT INTO talk(book_id, page_no, who, kind, text, turn_id, ts, created_at)"
                             " VALUES(?,?, 'ai', 'reply', ?,?,?,?)",
                             (book["id"], rows[-1]["page_no"], msg, turn_id, time.time(), now()))
            conn.execute("UPDATE turns SET status='done', done_at=? WHERE id=?", (now(), turn_id))
            conn.commit()
            conn.close()
            return
        if kind == "busy" and time.time() - t0 < BUSY_WAIT:
            time.sleep(8)
            continue
        tries += 1
        if kind == "fail" and tries < 2:
            time.sleep(6)
            continue
        print(f"送给 AI 没成功：{msg}", flush=True)
        conn = db()
        conn.execute("UPDATE turns SET status='failed', error=?, done_at=? WHERE id=?", (msg[:300], now(), turn_id))
        conn.commit()
        conn.close()
        return


def _courier() -> None:
    # 重启前送到一半的：可能已经进了对话，不自动重发，标没送到，读者点「重发」再送
    try:
        conn = db()
        conn.execute("UPDATE turns SET status='failed', error='书房重启了，没等到回话', done_at=? "
                     "WHERE status='sending'", (now(),))
        conn.commit()
        conn.close()
    except Exception:
        pass
    while True:
        _wake.wait(timeout=30)
        _wake.clear()
        try:
            while True:
                batch = _next_batch()
                if not batch:
                    break
                _send_batch(*batch)
        except Exception as e:
            print("courier error:", e, flush=True)
            time.sleep(5)


def _talk_rows(conn, book_id: int, after: int = 0, limit: int = 200):
    rows = conn.execute(
        "SELECT t.*, u.status AS turn_status FROM talk t LEFT JOIN turns u ON u.id=t.turn_id "
        "WHERE t.book_id=? AND t.id>? ORDER BY t.id DESC LIMIT ?", (book_id, after, limit)).fetchall()
    out = []
    for r in reversed(rows):
        state = "done" if r["who"] == "ai" else (
            "queued" if r["turn_id"] is None else {"sending": "sending", "failed": "failed"}.get(r["turn_status"], "done"))
        out.append({"id": r["id"], "who": r["who"], "kind": r["kind"], "text": r["text"], "page": r["page_no"],
                    "ref": r["ref"], "turn": r["turn_id"], "state": state, "at": r["created_at"]})
    return out


def _busy(conn, book_id: int) -> bool:
    q = conn.execute("SELECT 1 FROM talk WHERE book_id=? AND who='her' AND turn_id IS NULL LIMIT 1", (book_id,)).fetchone()
    s = conn.execute("SELECT 1 FROM turns WHERE book_id=? AND status='sending' LIMIT 1", (book_id,)).fetchone()
    return bool(q or s)


def _marks_on(conn, book_id: int, page: int):
    marks = conn.execute("SELECT * FROM marks WHERE book_id=? AND page_no=? ORDER BY id", (book_id, page)).fetchall()
    out = []
    for m in marks:
        t = conn.execute("SELECT t.turn_id, u.status FROM talk t LEFT JOIN turns u ON u.id=t.turn_id "
                         "WHERE t.kind='mark' AND t.ref=? ORDER BY t.id DESC LIMIT 1", (m["id"],)).fetchone()
        reply, state = None, "queued"
        if t and t["turn_id"]:
            state = {"sending": "sending", "failed": "failed"}.get(t["status"], "done")
            r = conn.execute("SELECT text FROM talk WHERE turn_id=? AND who='ai' ORDER BY id LIMIT 1",
                             (t["turn_id"],)).fetchone()
            reply = r["text"] if r else None
        out.append({"id": m["id"], "block": m["block"], "start": m["start"], "end": m["end"],
                    "quote": m["quote"], "reply": reply, "state": state, "turn": t["turn_id"] if t else None})
    return out


def _her_says(book_id: int, page: int, kind: str, text: str, ref=None) -> int:
    if MODE == "off":  # 没接 AI：划线照样存，只是不送
        return 0
    conn = db()
    cur = conn.execute("INSERT INTO talk(book_id, page_no, who, kind, text, ref, ts, created_at)"
                       " VALUES(?,?, 'her', ?,?,?,?,?)", (book_id, page, kind, text, ref, time.time(), now()))
    conn.commit()
    conn.close()
    _wake.set()
    return cur.lastrowid


async def _json(request: Request):
    try:
        return await request.json()
    except Exception:
        return None


def _book_or_none(conn, book_id):
    try:
        return conn.execute("SELECT * FROM books WHERE id=?", (int(book_id),)).fetchone()
    except Exception:
        return None


@mcp.tool()
def marks(book_id: int, page: int = 0, limit: int = 30) -> str:
    """看读者在阅读器里划过的线（最近的在前），带 AI 当时回没回。page 填了只看那一页。"""
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return f"书架上没有 [{book_id}]"
    q = "SELECT * FROM marks WHERE book_id=?" + (" AND page_no=?" if page else "") + " ORDER BY id DESC LIMIT ?"
    args = (book_id, page, max(1, min(limit, 100))) if page else (book_id, max(1, min(limit, 100)))
    rows = conn.execute(q, args).fetchall()
    out = [f"《{book['title']}》{READER}划的线："]
    for m in rows:
        info = _marks_on(conn, book_id, m["page_no"])
        mine = next((x for x in info if x["id"] == m["id"]), None)
        tail = "（回过）" if mine and mine["reply"] else "（还没回到）"
        out.append(f"p{m['page_no']} · 「{m['quote'][:120]}」{tail}")
    conn.close()
    return "\n".join(out) if rows else f"《{book['title']}》里{READER}还没划过线"


@mcp.custom_route("/api/les/book", methods=["GET"])
async def api_les_book(request: Request) -> JSONResponse:
    conn = db()
    book = _book_or_none(conn, request.query_params.get("book"))
    if not book:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    b = dict(book)
    b["ready"] = b["pages"] if b["kind"] == "text" else _ready_pages(b["id"])
    b["cover"] = _cover_url(b["id"], b["kind"])
    b["chapters"] = [{"title": r["title"], "page": r["page_no"]} for r in conn.execute(
        "SELECT title, page_no FROM chapters WHERE book_id=? ORDER BY idx", (b["id"],))]
    b["marked_pages"] = [r[0] for r in conn.execute(
        "SELECT DISTINCT page_no FROM marks WHERE book_id=? ORDER BY page_no", (b["id"],))]
    conn.close()
    return JSONResponse({"ok": True, "book": b})


@mcp.custom_route("/api/les/page", methods=["GET"])
async def api_les_page(request: Request) -> JSONResponse:
    conn = db()
    book = _book_or_none(conn, request.query_params.get("book"))
    try:
        page = int(request.query_params.get("p") or 1)
    except ValueError:
        page = 1
    if not book or not (1 <= page <= book["pages"]):
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    out = {"ok": True, "page": page, "kind": book["kind"], "marks": _marks_on(conn, book["id"], page)}
    if book["kind"] == "text":
        out["blocks"] = _page_blocks(conn, book["id"], page) or []
    else:
        out["img"] = f"/sider/{book['id']}/s-{page:03d}.jpg"
    conn.close()
    return JSONResponse(out)


@mcp.custom_route("/api/les/marks", methods=["GET"])
async def api_les_marks(request: Request) -> JSONResponse:
    """好词好句本：划过的所有句子（新的在前），带 AI 当时回的批注；book=N 只看一本。"""
    conn = db()
    try:
        book_id = int(request.query_params.get("book") or 0)
    except ValueError:
        book_id = 0
    books = {r["id"]: r for r in conn.execute("SELECT id, title, author, kind FROM books")}
    q = "SELECT book_id, page_no FROM marks" + (" WHERE book_id=?" if book_id else "") + " GROUP BY book_id, page_no"
    out = []
    for r in conn.execute(q, (book_id,) if book_id else ()).fetchall():
        b = books.get(r["book_id"])
        if not b:
            continue
        for m in _marks_on(conn, r["book_id"], r["page_no"]):
            row = conn.execute("SELECT created_at FROM marks WHERE id=?", (m["id"],)).fetchone()
            out.append({**m, "book": b["id"], "title": b["title"], "author": b["author"], "page": r["page_no"],
                        "at": row["created_at"] if row else "", "cover": _cover_url(b["id"], b["kind"])})
    conn.close()
    out.sort(key=lambda m: m["id"], reverse=True)
    return JSONResponse({"ok": True, "marks": out})


@mcp.custom_route("/api/les/mark", methods=["POST"])
async def api_les_mark(request: Request) -> JSONResponse:
    """划了一句：记下来，送给 AI。"""
    d = await _json(request) or {}
    conn = db()
    book = _book_or_none(conn, d.get("book_id"))
    try:
        page, block, a, b = int(d["page"]), int(d["block"]), int(d["start"]), int(d["end"])
    except Exception:
        conn.close()
        return JSONResponse({"ok": False}, status_code=400)
    blocks = _page_blocks(conn, book["id"], page) if book else None
    if not blocks or not (0 <= block < len(blocks)):
        conn.close()
        return JSONResponse({"ok": False, "error": "这页划不了"}, status_code=400)
    s = blocks[block]["s"]
    quote = str(d.get("quote") or "")
    if not (0 <= a < b <= len(s)) or s[a:b] != quote:  # 前端算的偏移对不上，就在段里找原句
        k = s.find(quote) if quote else -1
        if k < 0:
            conn.close()
            return JSONResponse({"ok": False, "error": "没找到这句"}, status_code=400)
        a, b = k, k + len(quote)
    quote = s[a:b].strip()
    if not quote or len(quote) > 600:
        conn.close()
        return JSONResponse({"ok": False, "error": "划太长了" if quote else "空的"}, status_code=400)
    cur = conn.execute("INSERT INTO marks(book_id, page_no, block, start, end, quote, created_at) VALUES(?,?,?,?,?,?,?)",
                       (book["id"], page, block, a, b, quote, now()))
    mark_id = cur.lastrowid
    conn.commit()
    conn.close()
    _her_says(book["id"], page, "mark", quote, mark_id)
    return JSONResponse({"ok": True, "id": mark_id, "start": a, "end": b})


@mcp.custom_route("/api/les/unmark", methods=["POST"])
async def api_les_unmark(request: Request) -> JSONResponse:
    d = await _json(request) or {}
    try:
        mark_id = int(d["id"])
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    conn = db()
    conn.execute("DELETE FROM marks WHERE id=?", (mark_id,))
    # 还没送出去的那条也撤回，别再叫 AI
    conn.execute("DELETE FROM talk WHERE kind='mark' AND ref=? AND turn_id IS NULL", (mark_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/les/say", methods=["POST"])
async def api_les_say(request: Request) -> JSONResponse:
    if MODE == "off":
        return JSONResponse({"ok": False, "error": "没接 AI"}, status_code=400)
    d = await _json(request) or {}
    conn = db()
    book = _book_or_none(conn, d.get("book_id"))
    conn.close()
    text = str(d.get("text") or "").strip()[:2000]
    try:
        page = int(d.get("page") or 0)
    except ValueError:
        page = 0
    if not book or not text:
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True, "id": _her_says(book["id"], page, "say", text)})


@mcp.custom_route("/api/les/together", methods=["POST"])
async def api_les_together(request: Request) -> JSONResponse:
    if MODE == "off":
        return JSONResponse({"ok": False, "error": "没接 AI"}, status_code=400)
    """点了「一起读」：这页原文跟着送过去。"""
    d = await _json(request) or {}
    conn = db()
    book = _book_or_none(conn, d.get("book_id"))
    conn.close()
    try:
        page = int(d.get("page") or 0)
    except ValueError:
        page = 0
    if not book or not (1 <= page <= book["pages"]):
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True, "id": _her_says(book["id"], page, "together", f"一起读第 {page} 页")})


@mcp.custom_route("/api/les/talk", methods=["GET"])
async def api_les_talk(request: Request) -> JSONResponse:
    conn = db()
    book = _book_or_none(conn, request.query_params.get("book"))
    if not book:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    try:
        after = int(request.query_params.get("after") or 0)
    except ValueError:
        after = 0
    out = {"ok": True, "items": _talk_rows(conn, book["id"], after), "busy": _busy(conn, book["id"])}
    # 在等的那几条状态会变（排队→送出→没送到），带上最近 30 条的状态让页面刷新
    out["states"] = {r["id"]: r["state"] for r in _talk_rows(conn, book["id"], 0, 30)}
    conn.close()
    return JSONResponse(out)


@mcp.custom_route("/api/les/retry", methods=["POST"])
async def api_les_retry(request: Request) -> JSONResponse:
    d = await _json(request) or {}
    try:
        turn_id = int(d["turn"])
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    conn = db()
    t = conn.execute("SELECT status FROM turns WHERE id=?", (turn_id,)).fetchone()
    if not t or t["status"] != "failed":
        conn.close()
        return JSONResponse({"ok": False}, status_code=400)
    conn.execute("UPDATE talk SET turn_id=NULL, ts=? WHERE turn_id=? AND who='her'", (time.time(), turn_id))
    conn.execute("UPDATE turns SET status='retried' WHERE id=?", (turn_id,))
    conn.commit()
    conn.close()
    _wake.set()
    return JSONResponse({"ok": True})


@mcp.custom_route("/shelf", methods=["GET"])
async def shelf_page(request: Request) -> HTMLResponse:
    return _page("shelf.html")


if __name__ == "__main__":
    for d in (DATA, BOOKS_DIR, SIDER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    db().close()
    threading.Thread(target=_courier, daemon=True).start()
    print(f"书房：http://{HOST}:{PORT}/   MCP：http://{HOST}:{PORT}/mcp   陪读：{MODE}", flush=True)
    # 无状态 HTTP：书房重启后 AI 那边的 MCP 不用跟着重连
    mcp.run(transport="http", host=HOST, port=PORT, stateless_http=True)
