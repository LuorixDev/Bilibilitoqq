DEFAULT_TEMPLATES = {
    "dynamic": "{name} 发布了新动态：{text}\n{SHOTPICTURE}\n{url}",
    "video": "{name} 投稿了新视频：{title}\n{SHOTPICTURE}\n{url}",
    "live_start": "{name} 开始直播：{title}\n{SHOTPICTURE}\n{url}",
    "live_hourly": "{name} 正在直播：{title}\n时长：{duration}｜人气：{online}｜峰值人气：{max_online}\n{SHOTPICTURE}\n{url}",
    "live_end": "{name} 直播结束：{title}\n时长：{duration}｜峰值人气：{max_online}\n{url}",
}

PLACEHOLDER_HINT = (
    "可用变量：{name} {text} {title} {url} {online} {duration} {max_online} "
    "{SHOTPICTURE} [atALL]"
)
