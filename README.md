# JM 漫画下载器 (astrbot_plugin_jm)

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 的 AstrBot 插件, 支持在聊天中搜索、查询和下载 18comic (禁漫) 上的本子。

> v2 重构: 模块化代码, 合并转发图片改为本地路径直传, 打包更快更稳。

## 安装

将本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_jm/`, 在 WebUI 中启用插件即可 (依赖 `jmcomic`, `PyYAML`, `Pillow` 会自动安装)。

## 命令

所有命令以 `/jm` 开头:

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/jm help` | h, 帮助 | 显示帮助 (渲染为 markdown 卡片图片) |
| `/jm status` | st, 状态, 配置 | 查看当前配置 |
| `/jm reload` | re, 重载 | 重载配置 (管理员) |
| `/jm search <关键词>` | sc, 搜索 | 搜索本子 |
| `/jm info <本子ID>` | if, 详情 | 查看本子详情 (自动发封面) |
| `/jm cover <本子ID>` | cv, 封面 | 仅获取封面 |
| `/jm episodes <本子ID>` | ep, 章节 | 列出章节 |
| `/jm photo <章节ID>` | ph, 章节详情 | 查看章节信息 |
| `/jm d <本子ID\|章节ID> [选择器]` | d, 下载 | 后台下载并推送合并转发 |
| `/jm ranking [day\|week\|month]` | rk, 排行榜 | 排行榜 |
| `/jm tags <标签> [页码]` | tg, 标签 | 按标签查询 |

下载选择器语法: `all` / `1,3,5` / `1-10` / `1,3-5`。

## 合并转发图片传输方式 (v2 变更)

v1 把图片注册到 AstrBot 文件服务后以 HTTP URL 交给 NapCat 下载。该方式有两个问题:

1. AstrBot 文件 token 一次性使用, NapCat 下载失败时对应图片节点被静默丢弃, QQ 客户端显示「该类消息类型暂不支持查看」;
2. 每张图多一次网络下载, 整本打包慢。

v2 直接构造 OneBot 转发 payload, 支持两种传输方式 (配置项 `forward_image_transport`):

- `file` (默认): 传 `file:///` 本地路径, NapCat 本机直读零下载, 最快最稳, 适合 NapCat 与 AstrBot 同一台机器 (含 Windows 本机部署);
- `base64`: 内嵌 base64 数据, 适合 NapCat 在其他机器/容器运行、无共享文件系统的场景。

## 配置项

见 `_conf_schema.json`。常用项:

- `max_forward_images`: 每条合并转发最多图片数, 超过自动分批 (0 不限制, 建议 5-10);
- `skip_if_cached`: 下载前按章节核对缓存目录, 已存在章节直接推送;
- `max_album_images`: 整本图片总数上限, 超过拒绝下载防误下长篇 (0 不限制);
- `custom_data_dir`: 自定义数据目录 (容器部署路径不一致时指定真实路径);
- `enable_login` / `username` / `password`: 禁漫账号登录 (收藏夹/高清原图);
- `use_proxy` / `proxy`: 代理设置;
- `dir_rule`: 下载目录命名 DSL (默认 `Bd / Atitle / Ptitle`)。

## 目录结构

```
main.py                 插件入口 (命令注册)
jm_plugin/
  utils.py              纯工具函数 (ID 提取 / 文件名清洗 / 选择器解析)
  config.py             配置 + 数据目录 + jmcomic option 生命周期
  client.py             jmcomic 客户端封装
  scan.py               下载目录扫描与缓存核对
  forward.py            合并转发推送 (file / base64)
  help_card.py          /jm help markdown 卡片
  download.py           后台下载任务编排
tests/                 单元测试
```

## 测试

```bash
cd <插件目录>
python -m pytest tests/ -q
```
