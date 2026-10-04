import requests
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 配置区 ==========
URL_LIST = [
    "https://raw.githubusercontent.com/CCSH/IPTV/refs/heads/main/live_lite.txt",
]
# 关键词映射：分组包含关键词 -> 输出分组名
KEYWORD_GROUP = [
    ("港澳台", "HS港澳台"),
    
]

ENABLE_CHECK = True        # 是否开启源可用性检测
CHECK_TIMEOUT = 4          # 单个源检测超时(秒)
MAX_WORKERS = 10           # 并发检测线程数
# ============================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def is_stream_valid(url: str) -> bool:
    """检测直播源是否可访问，优先HEAD，HEAD失败自动降级GET少量数据"""
    try:
        resp = requests.head(url, timeout=CHECK_TIMEOUT, headers=HEADERS, allow_redirects=True)
        if resp.status_code >= 200 and resp.status_code < 300:
            return True
    except requests.exceptions.RequestException:
        pass
    try:
        resp = requests.get(url, timeout=CHECK_TIMEOUT, headers=HEADERS, allow_redirects=True, stream=True)
        resp.raise_for_status()
        next(resp.iter_content(512))
        return True
    except requests.exceptions.RequestException:
        return False

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
            sp = ln.split(',', 1)
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
    raw_list = []          # [(输出分组名, 频道名, 地址)]
    url_seen = set()       # 只按 URL 去重

    print(f"🔍 开始拉取源列表，共 {len(URL_LIST)} 个远程地址")
    for url in URL_LIST:
        try:
            resp = requests.get(url, timeout=15, headers=HEADERS)
            resp.raise_for_status()
            text = resp.content.decode("utf-8")
            channels = parse_any(text)
            for extinf, play_url in channels:
                ch_name = get_channel_name(extinf)
                ch_group = get_group_title(extinf)
                output_group = None
                for kw, out_name in KEYWORD_GROUP:
                    if kw in ch_group:
                        output_group = out_name
                        break
                if output_group is None:
                    continue
                if play_url in url_seen:
                    continue
                url_seen.add(play_url)
                raw_list.append((output_group, ch_name, play_url))
        except Exception as e:
            print(f"⚠️ 拉取 {url} 失败：{e}")

    total_fetched = len(raw_list)
    print(f"✅ 筛选+去重完成，待检测源总数：{total_fetched}")

    # ---- 可用性检测 ----
    valid_channels = []
    if ENABLE_CHECK and total_fetched > 0:
        print(f"🧪 开始并发检测源可用性，线程数：{MAX_WORKERS}")
        future_map = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for item in raw_list:
                future = executor.submit(is_stream_valid, item[2])
                future_map[future] = item
            ok_cnt = 0
            bad_cnt = 0
            for future in as_completed(future_map):
                group, name, u = future_map[future]
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                if ok:
                    valid_channels.append((group, name, u))
                    ok_cnt += 1
                else:
                    bad_cnt += 1
                    print(f"  ❌ {name} | {u[:60]}")
        print(f"📊 检测结果：有效 {ok_cnt} / 失效 {bad_cnt} / 共 {total_fetched}")
    else:
        valid_channels = raw_list
        print("⚠️ 源检测已关闭，直接使用全部筛选后的频道")

    # ---- 排序：先按分组，再按频道名，同频道多源聚在一起 ----
    valid_channels.sort(key=lambda x: (x[0], x[1]))
    print(f"🔢 已按 (分组, 频道名) 排序，同频道多源已聚合")

    # ---- 输出 m3u8 ----
    output_m3u = ["#EXTM3U"]
    for output_group, cname, curl in valid_channels:
        fake_ext = f'#EXTINF:-1 group-title="{output_group}",{cname}'
        output_m3u.append(fake_ext)
        output_m3u.append(curl)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    m3u8_path = os.path.join(out_dir, "live.m3u8")
    with open(m3u8_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_m3u))

    print(f"\n🎉 完成！输出频道数：{len(valid_channels)}")
    print(f"📁 文件：{m3u8_path}")

if __name__ == "__main__":
    main()
