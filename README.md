# astrbot_plugin_jm

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 的 AstrBot QQ 插件, 在聊天中搜索 / 查看 / 下载 18comic (禁漫) 的本子.

> ⚠️ **免责声明**: 本项目仅用于学习与个人备份, 请勿用于商业用途. 使用本插件需遵守当地法律法规与目标站点的使用条款.

## License

MIT © 2026 PolysaCHride - 详见 [LICENSE](LICENSE) 文件.

本仓库源码采用 MIT 协议发布. 请注意:

- 本插件依赖 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python), 其许可与使用条款请参考其仓库
- 任何因使用本插件产生的法律 / 版权问题由使用者自行承担

## 功能

- 🔍 站内搜索 (关键词 / 标签)
- 📖 本子详情 + 封面图
- 📑 章节列表
- 🏆 排行榜 (日 / 周 / 月)
- ⏬ 异步下载, 完成后以合并聊天记录推送漫画图集
- 🌐 支持 HTTP 代理 / 自定义域名 / 客户端实现切换 (html / api)
- 👤 登录后访问收藏夹 / 高清原图

## 安装

1. 在Astrbot控制台中选择Astrbot插件→右下角加号→从文件安装，选择本插件进行安装，依赖会自动安装;或下载源代码zip，手动将本插件目录放到 AstrBot 的插件目录，在插件目录执行 `pip install jmcomic PyYAML`安装依赖。
2. 在 AstrBot WebUI 中启用/重载插件。

## 命令一览

所有命令以 `/jm` 开头.

| 命令                             | 别名 | 说明                  |
| -------------------------------- | ---- | --------------------- |
| `/jm help`                       | `h`  | 显示帮助              |
| `/jm status`                     | `st` | 查看当前配置          |
| `/jm reload`                     | `re` | 重新加载配置 (管理员) |
| `/jm search <关键词>`            | `sc` | 搜索本子              |
| `/jm info <本子ID>`              | `if` | 查看本子详情 (附封面) |
| `/jm cover <本子ID>`             | `cv` | 仅获取本子封面        |
| `/jm episodes <本子ID>`          | `ep` | 列出本子的全部章节    |
| `/jm photo <章节ID>`             | `ph` | 查看章节信息          |
| `/jm download <ID> [选择器]`     | `d`  | 下载本子/章节 (异步)  |
| `/jm ranking [day\|week\|month]` | `rk` | 排行榜                |
| `/jm tags <标签> [页码]`         | `tg` | 按标签查询            |

### 章节选择器 (d)

| 写法           | 含义         |
| -------------- | ------------ |
| `all` / `全部` | 全部章节     |
| `1,3,5`        | 指定章节序号 |
| `1-10`         | 范围         |
| `1,3,5-10,15`  | 混合格式     |

例: `/jm d 350234 1-5` 下载 ID 350234 的前 5 章.

## 配置项

| 字段                 | 默认值                 | 说明                                                                                              |
| -------------------- | ---------------------- | ------------------------------------------------------------------------------------------------- |
| `client_impl`        | `api`                  | 客户端实现: `html` (网页端, 效率高) / `api` (APP 端, 兼容性更好)                                  |
| `use_proxy`          | `false`                | 是否启用代理                                                                                      |
| `proxy`              | 空                     | 代理地址, 例: `http://127.0.0.1:7890`                                                             |
| `max_forward_images` | `30`                   | 合并聊天记录单次最多发送图片数, `0` 表示不限制。建议范围 20-50, 过大易导致 WebSocket API 调用超时 |
| `enable_login`       | `false`                | 是否登录                                                                                          |
| `username`           | 空                     | jmcomic 登录账号                                                                                  |
| `password`           | 空                     | jmcomic 登录密码                                                                                  |
| `retry_times`        | `3`                    | 网络请求重试次数                                                                                  |
| `auto_send_cover`    | `true`                 | 查询本子详情时是否自动发送封面图                                                                  |
| `max_search_results` | `10`                   | 搜索结果最大显示条数                                                                              |
| `custom_data_dir`    | 空                     | 自定义数据目录, 留空使用 AstrBot 默认路径                                                         |
| `custom_domain`      | 空                     | 自定义域名, 多个用英文逗号分隔                                                                    |
| `image_thread_count` | `16`                   | 同时下载图片数 (网页端建议 ≤ 50)                                                                  |
| `photo_thread_count` | `4`                    | 同时下载章节数                                                                                    |
| `image_suffix`       | `.jpg`                 | 图片保存后缀, 留空保持原格式                                                                      |
| `download_subdir`    | `downloads`            | 数据目录下的下载子目录名                                                                          |
| `dir_rule`           | `Bd / Atitle / Ptitle` | 下载目录命名规则 (jmcomic DSL)                                                                    |
| `max_album_images`   | `0`                    | 整本最多下载图片数, `0` 表示不限制                                                                |
| `nested_forward`     | `false`                | 兼容旧配置, 已停用; QQ 平台始终按批发送多条独立合并聊天记录                                       |

## 目录结构

```
astrbot_plugin_jm/
├── main.py              # 插件主代码
├── metadata.yaml        # 插件元数据
├── requirements.txt     # Python 依赖
├── _conf_schema.json    # 插件配置 schema
├── README.md            # 本文件
└── (运行时数据存放在 AstrBot/data/plugin_data/astrbot_plugin_jm/)
    ├── downloads/       # 下载内容
    └── covers/          # 缓存的本子封面
```

## 注意事项

- 第一次执行 `/jm download` 时会初始化 jmcomic option, 可能需要数秒.
- 由于 jmcomic 是同步阻塞库, 所有网络 / 文件 IO 都通过 `asyncio.to_thread` 包装到线程池, 不会阻塞 AstrBot 主事件循环.
- 下载完成后会通过合并聊天记录主动推送漫画图集到原始会话.
- 若一次下载图片过多, 会按 `max_forward_images` 拆成多条 QQ 合并聊天记录分批发送, 图片文件仍保留在下载目录.

## License

MIT - 详见 [LICENSE](LICENSE).
