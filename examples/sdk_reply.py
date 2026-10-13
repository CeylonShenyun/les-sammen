"""用 Claude Agent SDK 接书房的小脚本（companion.mode = "command" 时用）。

书房把要送的那条消息从 stdin 给你，你把回复 print 到 stdout，书房就贴回书页。
  pip install claude-agent-sdk
config.json：
  "command": {"cmd": ["venv/bin/python", "examples/sdk_reply.py", "<你的会话 ID>"], "timeout": 600}
会话 ID 留空就每次新开（不记得之前聊过什么）。字段出处见 https://code.claude.com/docs/en/agent-sdk
已经有自己的 SDK 程序的话，照这个样子改：读 stdin → 发给你的 agent → 打印最后一句回复。
"""
import asyncio
import os
import sys

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

MCP_URL = os.environ.get("LES_MCP_URL", "http://127.0.0.1:18050/mcp")  # 书房自己的 MCP，AI 能翻书


async def main() -> int:
    prompt = sys.stdin.read()
    session_id = (sys.argv[1] if len(sys.argv) > 1 else "") or None
    options = ClaudeAgentOptions(
        resume=session_id,
        mcp_servers={"bibliotek": {"type": "http", "url": MCP_URL}},
        allowed_tools=["mcp__bibliotek__*"],
        max_turns=8,
    )
    last_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    last_text = block.text
        if isinstance(message, ResultMessage):
            if message.subtype != "success":
                print(f"没成功：{message.subtype}", file=sys.stderr)
                return 1
            print((getattr(message, "result", None) or last_text).strip())
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
