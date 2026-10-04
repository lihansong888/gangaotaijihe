import requests
import re
import os

# ========== 填写源的地址 ==========
URL_LIST = [
    "https://raw.githubusercontent.com/CCSH/IPTV/refs/heads/main/live_lite.txt",
]

# 关键词映射：分组包含关键词 -> 输出分组名
KEYWORD_GROUP = [
    ("港澳台", "HS港澳台"),
    ("纪录片直播", "HS纪录片直播"),
    
]

def parse_any(text: str):
    res = []
    extinf_line = None
    current_group = None
    for raw_line in text.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        if ln.startswith("#EXTINF:"):
            extinf_line = ln
            continue
        if extinf_line is not None and not ln.startswith("#"):
            res.append((extinf_line, ln))
            extinf_line = None
            continue
        if ',' in ln and not ln.startswith("#"):
            sp = ln.split(',',1)
            name_part = sp[0].strip()
            url_part = sp[1].strip()
            if url_part == "#genre#":
                current_group = name_part
                continue
            if current_group:
                fake_ext = f'#EXTINF:-1 group-title="{current_group}",{name_part}'
            else:
                fake_ext = f'#EXTINF:-1,{name_part}'
            res.append((fake_ext, url_part))
    return res

def get_channel_name(extinf):
    if "," in extinf:
        return extinf.split(",")[-1].strip()
    return ""

def get_group_title(extinf):
    m = re.search(r'group-title="([^"]+)"', extinf)
    if m:
        return m.group(1).strip()
    return ""

def main():
    channel_list = []      # [(输出分组名, 频道名, 地址)]
    seen = set()
    for url in URL_LIST:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            text = resp.content.decode("utf-8")
            channels = parse_any(text)
            for extinf, play_url in channels:
                ch_name = get_channel_name(extinf)
                ch_group = get_group_title(extinf)
                output_group = None
                # 关键词模糊匹配
                for kw, out_name in KEYWORD_GROUP:
                    if kw in ch_group:
                        output_group = out_name
                        break
                if output_group is None:
                    continue
                item_key = (ch_name, play_url)
                if item_key not in seen:
                    seen.add(item_key)
                    channel_list.append((output_group, ch_name, play_url))
        except Exception as e:
            print(f"⚠️ 拉取 {url} 失败：{e}")
    total_cnt = len(channel_list)
    print(f"✅筛选结束，共提取 {total_cnt} 个频道")
    out_dir = os.path.dirname(os.path.abspath(__file__))
    output_m3u = ["#EXTM3U"]
    # 按各自映射后的分组名输出
    for output_group, cname, curl in channel_list:
        fake_ext = f'#EXTINF:-1 group-title="{output_group}",{cname}'
        output_m3u.append(fake_ext)
        output_m3u.append(curl)
    m3u8_path = os.path.join(out_dir, "live.m3u8")
    with open(m3u8_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_m3u))
    print(f"✅已输出 m3u8：{m3u8_path}")

if __name__ == "__main__":
    main()
