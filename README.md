# les-sammen · 一起读的书房

> *Les sammen, litt hver dag.* —— 挪威语「一起读，每天一点」。

一个自己部署的小书房：把 pdf / epub / txt 放上书架，在手机网页里一页一页地读。
读到一句想问的，**选中划线**，你的 AI 就会收到这句话（带前后文），它的回复**贴回那一页、那条线旁边**，页边亮一个小点；
点「**一起读这页**」，它会拿到整页原文陪你读；书页旁边还有个**小窗**，可以直接跟它聊这本书。

同时它是一个 **MCP 服务**：你的 AI 能自己翻书、搜书、看目录、看你划过哪些线。

**不只 Claude**：Claude Code（`claude -p`）、Claude API、Claude Agent SDK、OpenAI 格式的各家模型（GPT、DeepSeek、Gemini、豆包、硅基流动、OpenRouter……）、你自己的 bridge 或脚本，都能接。

| 书架 | 阅读 | 批注 | 小窗 | 好词好句本 |
|---|---|---|---|---|
| ![书架](docs/shelf.webp) | ![阅读](docs/read.webp) | ![批注](docs/note.webp) | ![小窗](docs/chat.webp) | ![好词好句本](docs/sitat.webp) |

## 能做什么

- **书架**：按分类分层（课本 / 阅读 / 自己起的名字），书立在玻璃搁板上，封面可以自己换；最上面一张「接着读」。
- **三种格式**：epub / txt 会按手机一屏重新排版（认章节、小标题、章首引语），**能划线**；pdf 按原样一页页显示（能「一起读」、能聊，但划不了线）。
- **阅读器**：一页正好装满、只翻页不滑动（字号按你的屏幕自动算，系统字体调大了也装得下）；上下两栏平时收起，点屏幕中间叫出来；页眉印书名和章名。
- **陪读**：划线 / 一起读这页 / 小窗说话，攒几秒合成一条送给 AI；回复贴回书页，翻走再翻回来、下次打开都还在。翻页不会叫 AI，不白烧额度。
- **好词好句本**：划过的句子都收在一起，带着 AI 当时的批注。
- **MCP 工具**：`shelf` `read_pages` `toc` `find` `mark` `marks` `import_book` `rename_book` `relayout` `withdraw`，外加可选的生词本 / 错题本 / 练习题。

## 快速开始

需要 Linux + Python 3.10 以上。

```bash
git clone https://github.com/CeylonShenyun/les-sammen.git
cd les-sammen
cp config.example.json config.json   # 先改一下 companion.mode，见下面
sudo ./install.sh                     # 建 venv、装依赖、装 systemd 服务、把 MCP 注册进 Claude Code
```

打开 `http://127.0.0.1:18050/`，点「上架一本书」传一本 epub 试试。

> ⚠️ 书房本身**没有登录**。只在本机用没关系；要从手机访问、放到公网，一定照 `examples/nginx.example.conf` 套一层密码，并且别把 `/mcp` 暴露出去。

改了 `config.json` 以后：`sudo systemctl restart les-sammen`。

## 接上你的 AI

`config.json` 里 `companion.mode` 选一种：

### `claude_p` —— 用 Claude Code（`claude -p`）

最省事，用的是你自己的 Claude Code 登录。

```json
"companion": { "mode": "claude_p",
  "claude_p": { "bin": "claude", "cwd": "/home/你/你平时的项目目录", "session_id": "", "attach_mcp": true, "args": ["--max-turns", "8"] } }
```

- **`session_id` 填你平时聊天的那个会话**，陪你读书的就是它本人，记得你们之前聊过的一切；会话 ID 是 `~/.claude/projects/<目录名>/` 下面那个 `.jsonl` 的文件名，`cwd` 要填那个会话所在的项目目录。
- `session_id` 留空：每本书自己开一个会话，记得这本书里聊过什么。
- 别在终端里同时开着同一个会话聊天，两边会打架。
- `attach_mcp: true` 会把书房的 MCP 挂给它，它能自己 `read_pages` 翻前后文。
- `install.sh` 用哪个用户跑（`sudo` 之前的那个用户），`claude -p` 就用哪个用户的登录；它会顺手把 `claude` 的完整路径写进 `bin`（systemd 里找不到 PATH 上的命令）。

### `api` —— 直接用 Claude API

```json
"companion": { "mode": "api",
  "api": { "model": "claude-opus-5", "api_key": "env:ANTHROPIC_API_KEY", "base_url": "", "fallbacks": true, "history_turns": 20, "system_file": "" } }
```

- key 写在项目目录的 `.env` 里（`ANTHROPIC_API_KEY=sk-ant-...`），config 里用 `"env:变量名"` 引用，别把 key 直接写进 config。
- 书房自己记着这本书聊过的最近 `history_turns` 轮，每次连同那一页原文一起发，所以 AI 记得刚才说过什么、也看得到原文。
- `system_file` 可以指向一个文本文件，写你家 AI 的人设，会加在系统提示最前面。
- 用中转站就填 `base_url`；中转不认 `fallbacks`（官方 API 的拒答自动换模型）报错的话，把它改成 `false`。
- 模型、`max_tokens` 都可以改；要省钱可以换成 `claude-sonnet-5`。

### `openai` —— GPT、DeepSeek、Gemini、豆包……（OpenAI 格式）

只要那家给的是 OpenAI 格式的接口（`/chat/completions`），填网址、key、模型名就能用：

```json
"companion": { "mode": "openai",
  "openai": { "base_url": "https://api.deepseek.com/v1", "api_key": "env:OPENAI_API_KEY", "model": "deepseek-chat", "history_turns": 20 } }
```

| 用哪家 | `base_url` | `model` 举例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | 你账号里能用的模型 |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-…` |
| 豆包（火山方舟） | `https://ark.cn-beijing.volces.com/api/v3` | 你的接入点 ID |
| 硅基流动 | `https://api.siliconflow.cn/v1` | 平台上的模型名 |
| OpenRouter | `https://openrouter.ai/api/v1` | `厂商/模型名` |

- key 同样写在 `.env`（`OPENAI_API_KEY=...`），config 里 `"env:OPENAI_API_KEY"` 引用；变量名随你起。
- 跟 `api` 模式一样：书房记着这本书最近几轮，每条带那一页原文；`system_file` 放人设。
- `max_tokens` 默认不发；要限长度就填，有的新模型要求叫 `max_completion_tokens`，用 `max_tokens_field` 改名。
- `extra` 里的东西会原样并进请求（比如 `{"temperature": 0.7}`）。
- 记得把 `ai_name` 改成你家 AI 的名字，页面上才不会写着 Claude。

### `command` —— 用 SDK（或任何你自己的程序）

书房把要送的那条消息从 **stdin** 给你的程序，你把回复 **print 到 stdout**。退出码 75 表示「正忙，等会儿再来」。什么语言、接哪家模型都行。

```json
"companion": { "mode": "command",
  "command": { "cmd": ["venv/bin/python", "examples/sdk_reply.py", "你的会话ID"], "timeout": 600 } }
```

`examples/sdk_reply.py` 是一个用 **Claude Agent SDK** 的现成脚本（`venv/bin/pip install claude-agent-sdk`）：接着你的会话说话、挂上书房的 MCP、打印最后的回复。已经有自己的 SDK 程序的话，照它的样子改：读 stdin → 发给你的 agent → 打印回复。

### `http` —— 发到一个网址

- `"format": "anthropic"`：按 Messages API 的格式 POST（`claude-code-p-bridge` 这类 bridge 可以直接接），回复取 `content` 里的文字。
- `"format": "simple"`：POST `{"text", "book_id", "book_title"}`，你回 `{"reply": "..."}` 或者直接回纯文字都行。
- `headers` 里的值同样可以写 `"env:变量名"`。409 / 429 / 503 会当成「正忙」，等一会儿再送。

### `off`

不接 AI，只当阅读器用。划线照样存、好词好句本照样有。

## 让 AI 知道怎么陪读

送过去的消息长这样：

```
[书房]《存在与时间》（书 3）
第64页 她划了：「此在总是从它的生存来领会自己。」
  上下文：…前一句【此在总是从它的生存来领会自己。】后一句…
第64页 她说：这句什么意思
（这条来自书房：回复会贴在书页旁边，短一点，像坐在旁边小声说。）
```

最后那行提示是默认加的。想让它陪得更好，把 `examples/CLAUDE.md.snippet` 改成你们的称呼，放进它的 `CLAUDE.md` 或系统提示里，然后把 `companion.hint` 设成 `false`。消息里的「她」可以用 `reader` 改。

## 配置项

| 项 | 默认 | 说明 |
|---|---|---|
| `title` / `subtitle` | 书房 / Bibliotek | 书架页大标题 |
| `ai_name` | Claude | 页面上 AI 叫什么（「划线 · 给 Claude」「Claude 在写」） |
| `reader` | 她 | 送给 AI 的消息里怎么称呼你 |
| `host` / `port` | 127.0.0.1 / 18050 | 监听地址；公网请走 nginx |
| `mcp_name` | bibliotek | MCP 服务名，工具会叫 `mcp__bibliotek__read_pages` 之类 |
| `data_dir` | data | 数据库、原件、页面图都在这下面，备份它就行 |
| `back_url` | 空 | 书架左上角返回按钮去哪；空就不显示 |
| `background` | /assets/bg.webp | 背景图，换成你自己的放进 `assets/` |
| `study_rooms` | false | 显示生词本 / 错题本 / 练习题三间 |
| `alphabet` | a–z | 生词本按首字母筛选用的字母表（学挪威语可以填 `abcdefghijklmnopqrstuvwxyzæøå`） |
| `companion.attach_page` | api / openai 模式开 | 每条消息都带上那一页原文 |
| `companion.hint` | api / openai 以外开 | 每条消息末尾的那行提示 |

## 已知限制

- pdf 是切成图片显示的，**划不了线**；扫描版 pdf 抽不出文字，AI 也搜不了。小说、哲学书建议用 epub / txt。
- 分页是服务器按手机一屏估算的（一行 18 个字左右），这样 AI `read_pages` 的页码和你看到的一致；平板上会留白多一点。
- txt 认章节靠「第X章」这类标记；没有的话认「没标点的短行 + 下一行是……引语……」；还认不出，让 AI 调 `relayout(书号, chapter_titles="章名1 章名2 …")` 按目录重排。

## 致谢

灵感来自 [meowmana/coread](https://github.com/meowmana/coread)（共读室，MIT）。背景图是 AI 画的。

## License

MIT
