"""astrbot_plugin_jm v2 核心包。

模块划分:
- utils:     纯工具函数 (无任何依赖, 便于单元测试)
- config:    插件配置 + 数据目录 + jmcomic option 生命周期
- client:    jmcomic 客户端封装 (option 缓存 / 登录 / 阻塞调用)
- scan:      下载目录扫描与缓存核对
- forward:   QQ 合并转发图集推送 (file / base64 传输)
- help_card: /jm help markdown 卡片 (PIL 渲染 + 纯文本回退)
- download:  后台下载任务编排
"""

__version__ = "2.0.0"
