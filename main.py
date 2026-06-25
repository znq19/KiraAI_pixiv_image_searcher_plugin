{
  "refresh_token": {
    "type": "sensitive",
    "name": "Pixiv Refresh Token",
    "default": "",
    "hint": "从 Pixiv 获取的 refresh_token"
  },
  "proxy": {
    "type": "string",
    "name": "HTTP 代理",
    "default": "",
    "hint": "例如 http://127.0.0.1:7890，留空表示不使用代理"
  },
  "max_count": {
    "type": "integer",
    "name": "最大返回张数",
    "default": 5,
    "minimum": 1,
    "maximum": 20
  },
  "min_bookmarks": {
    "type": "integer",
    "name": "最低收藏数阈值",
    "default": 100,
    "minimum": 0
  },
  "exclude_tags": {
    "type": "list",
    "name": "排除标签",
    "default": ["AI", "R-18", "裸足"]
  },
  "allow_r18_in_dm": {
    "type": "switch",
    "name": "私聊允许发送 R-18",
    "default": false,
    "hint": "开启后，在私聊中搜索图片时将允许发送 R-18 内容（x_restrict=1），并忽略排除标签中的 R-18"
  },
  "allow_r18g_in_dm": {
    "type": "switch",
    "name": "私聊允许发送 R-18G",
    "default": false,
    "hint": "开启后，在私聊中搜索图片时将允许发送 R-18G 内容（x_restrict=2），并忽略排除标签中的 R-18G"
  },
  "r18_whitelist": {
    "type": "list",
    "name": "R-18 会话白名单",
    "default": [],
    "hint": "允许发送 R-18 的会话 ID 列表，格式如 qq:dm:123456、qq:gm:789012。私聊留空=所有私聊都允许（需开启上方开关）；群聊留空=所有群聊都不允许。"
  },
  "r18g_whitelist": {
    "type": "list",
    "name": "R-18G 会话白名单",
    "default": [],
    "hint": "允许发送 R-18G 的会话 ID 列表，格式同上。私聊留空=所有私聊都允许（需开启上方开关）；群聊留空=所有群聊都不允许。"
  },
  "max_search_pages": {
    "type": "integer",
    "name": "最大搜索页数",
    "default": 10,
    "minimum": 1,
    "maximum": 5000
  },
  "keyword_map": {
    "type": "json",
    "name": "关键词映射表",
    "default": {
      "黑丝": "黒タイツ",
      "白丝": "白タイツ",
      "腿": "太もも",
      "脚": "足",
      "萝莉": "ロリ",
      "少女": "少女",
      "御姐": "お姉さん",
      "泳装": "水着",
      "制服": "制服",
      "女仆": "メイド",
      "猫耳": "猫耳",
      "原神": "原神",
      "碧蓝航线": "アズールレーン",
      "明日方舟": "アークナイツ",
      "初音": "初音ミク"
    },
    "hint": "中文关键词到 Pixiv tag 的映射，会覆盖默认映射"
  },
  "dedup_file": {
    "type": "string",
    "name": "去重记录文件路径",
    "default": "data/pixiv_sent_ids.json",
    "hint": "持久化已发送图片 ID 的 JSON 文件路径，相对 data 目录"
  },
  "dedup_expire_hours": {
    "type": "integer",
    "name": "去重过期时间（小时）",
    "default": 24,
    "minimum": 0,
    "maximum": 720,
    "hint": "0 表示永久去重，过期后相同图片可再次发送"
  },
  "image_storage_dir": {
    "type": "string",
    "name": "图片缓存目录",
    "default": "files/pixiv",
    "hint": "下载图片存放目录，相对 data 目录，例如 files/pixiv"
  },
  "download_original": {
    "type": "switch",
    "name": "默认下载原图",
    "default": false,
    "hint": "关闭时下载预览图（较小），开启时下载原图"
  },
  "max_cache_files": {
    "type": "integer",
    "name": "缓存文件数量上限",
    "default": 50,
    "minimum": 1,
    "maximum": 500
  },
  "cleanup_count": {
    "type": "integer",
    "name": "每次清理旧文件数量",
    "default": 20,
    "minimum": 1,
    "maximum": 100
  },
  "send_as_forward": {
    "type": "switch",
    "name": "以合并转发形式发送",
    "default": true,
    "hint": "开启后多张图片以合并转发发送，关闭则逐条发送"
  },
  "enable_title_search": {
    "type": "switch",
    "name": "开启标题和简介搜索",
    "default": true,
    "hint": "关闭后，只搜索标签，不搜索标题和简介"
  },
  "search_priority": {
    "type": "enum",
    "name": "优先搜索项",
    "default": "tags",
    "options": ["tags", "title"],
    "hint": "tags=优先标签，title=优先标题+简介（仅在开启标题搜索时生效）"
  }
}
