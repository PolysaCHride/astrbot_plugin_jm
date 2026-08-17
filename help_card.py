from __future__ import annotations

COMMANDS = [
 ("/jm help", "h / 帮助", "显示帮助卡片"), ("/jm status", "st / 状态", "查看配置"), ("/jm reload", "re / 重载", "管理员重载配置"),
 ("/jm search <关键词>", "sc / 搜索", "搜索本子"), ("/jm info <本子ID>", "if / 详情", "查看详情和封面"),
 ("/jm cover <本子ID>", "cv / 封面", "获取封面"), ("/jm episodes <本子ID>", "ep / 章节", "列出章节"),
 ("/jm photo <章节ID>", "ph / 章节详情", "查看章节信息"), ("/jm download <ID> [选择器]", "d / 下载", "异步下载并分批转发"),
 ("/jm ranking [day|week|month]", "rk / 排行榜", "查看排行"), ("/jm tags <标签> [页码]", "tg / 标签", "按标签查询"),
]

def help_markdown():
    rows = "\n".join(f"| `{cmd}` | {alias} | {desc} |" for cmd, alias, desc in COMMANDS)
    return "# 📚 JM 漫画下载器\n\n基于 JMComic-Crawler-Python 的搜索、查询和下载工具。\n\n| 命令 | 别名 | 说明 |\n|---|---|---|\n" + rows + "\n\n**选择器示例**：`all`、`1,3,5`、`1-10`、`1,3,5-10`\n\n> 图片使用文件服务 URL 分批发送，避免合并转发中的 Base64 显示异常。"

def help_text(): return help_markdown().replace("|", " ").replace("`", "")

async def render_help(plugin):
    try:
        rendered = await plugin.text_to_image(help_markdown(), return_url=False)
        return rendered if rendered else None
    except Exception:
        return None
