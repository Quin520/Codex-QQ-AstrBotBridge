"""Keep dialogue tool guidance compact and aligned with permission-filtered tools."""


def capability_instructions(config: dict, tool_names: set[str]) -> str:
    """Describe only available capabilities; execution guards remain authoritative."""
    notes = []
    if "search_public_web" in tool_names:
        notes.append(
            "近期信息需要核实时用 search_public_web，只传公开主题，不传群聊、成员资料或本机信息；附来源。"
        )
    if "send_voice" in tool_names:
        notes.append(
            "语音请求用 send_voice，普通聊天默认文字，主动发言只在合适的祝福等场合选语音；它是朗读，不是唱歌。"
        )
        if config.get("voice_use_rvc", False):
            notes.append(
                "send_voice 已配置 RVC 角色音色，转声失败不能改发默认声音冒充。"
            )
    if "search_web_image" in tool_names:
        notes.append(
            "普通找图、图片或照片用 search_web_image 搜现成图片并附来源，查询不含隐私；表情包优先用已有表情工具。找不到不能偷偷改成生图。"
        )
    if "generate_image" in tool_names:
        notes.append(
            "明确要求画图、生成、自画像时用 generate_image；参考图需按已看到的视觉内容写描述，只有文字生图，不直接编辑原图或保证精确复制，看不到参考就请补发。"
        )
    elif config.get("image_generation_enabled", False) and tool_names:
        notes.append(
            "生图只在本轮明确创作请求时开放；未提供 generate_image 不代表服务坏了，可请用户明确说‘画一张…’。"
        )
    if "send_douyin_video" in tool_names:
        notes.append(
            "抖音链接原样交 send_douyin_video；只有关键词先搜索真实 douyin.com/video 页面，不能编视频 ID。明确发视频就不改发封面、图片或音频；主动发言不自行发视频。"
        )
    if "find_in_bilibili" in tool_names:
        notes.append(
            "B站用 find_in_bilibili(delivery=video 或 audio) 搜索，再用 deliver_media 发送。明确链接或平台失败不能擅自换片；未指定平台可说明来源后找替代。"
        )
    elif "find_music" in tool_names:
        notes.append(
            "旧版B站点歌用 find_music 搜索，再用 deliver_music 发送；不能把原曲播放说成翻唱。"
        )
    if "play_song_by_name" in tool_names:
        notes.append("普通点歌优先 play_song_by_name；播放原曲不等于自己唱歌。")
    if "find_cover_music" in tool_names or "sing_cover" in tool_names:
        if str(config.get("singing_model", "") or "").strip():
            notes.append(
                "明确要求用你的声音翻唱：先 find_cover_music，后将本轮 search_id、position 交 sing_cover，用固定 RVC 音色合成，默认60秒。确切链接或 BV/AV 原样作 title，不改模糊歌名。不能拿原曲或朗读冒充翻唱，不主动翻唱。"
            )
        else:
            notes.append("翻唱尚未选择角色音色，说明等待主人配置，无需先搜歌。")
    if not notes:
        return ""
    return (
        "\n本轮工具使用：\n"
        + "\n".join(notes)
        + "\n工具确认送达后不再重复文字、确认或等待提示。失败简短如实说明；接口受限不等于音源不存在，同轮不反复重试。"
        "不要求群员提供 Cookie、密码或本机路径，不输出本机文件路径。\n"
    )
