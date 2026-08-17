# astrbot_plugin_jm v2

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 的模块化 AstrBot 插件。

## 主要改进

- 配置、客户端、下载缓存、转发和帮助卡片拆分为独立模块。
- 合并转发图片先注册到 AstrBot 文件服务，再通过 OneBot HTTP(S) URL 发送，避免 `base64://` 导致 QQ 显示“该类消息类型暂不支持查看”。
- 同批图片使用并发文件注册，按 `max_forward_images` 分批发送，不创建嵌套转发。
- `/jm help` 使用 AstrBot T2I 渲染 Markdown 帮助卡片，渲染失败时自动降级为文本。
- 下载前按 jmcomic `dir_rule` 检测章节缓存，只补下缺失章节。

## 命令

`/jm help`、`status`、`reload`、`search <关键词>`、`info <本子ID>`、`cover <本子ID>`、`episodes <本子ID>`、`photo <章节ID>`、`download <ID> [选择器]`、`ranking [day|week|month]`、`tags <标签> [页码]`。

下载选择器支持 `all`、`1,3,5`、`1-10` 和混合写法。

## 配置

沿用 v1 的 `_conf_schema.json`。`callback_api_base` 必须是 NapCat/QQ 能访问的 AstrBot 文件服务基础 URL，例如 `http://astrbot:6185`；不要填写 `/api/file` 后缀。它是图片合并转发可查看的必要条件。

## 测试

在插件目录执行 `python -m pytest -q`。测试仅覆盖纯逻辑和转发 payload，不访问 JMComic 网络。
