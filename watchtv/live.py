import requests
import re
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 配置区 ==========
URL_LIST = [
    "https://raw.githubusercontent.com/CCSH/IPTV/refs/heads/main/live_lite.txt",
]
KEYWORD_GROUP = [
    ("港澳台", "HS港澳台"),
    ("纪录片直播", "HS纪录片直播"),
]

ENABLE_CHECK = True          # 是否开启源检测
CHECK_TIMEOUT = 2            # 单个源检测总超时(秒)
MAX_WORKERS = 10             # 并发检测线程数

# ---- 延迟/速度阈值（按需调整）----
MAX_TTFB_MS = 1200           # 首字节超过此毫秒数判定为慢（>1200ms 踢）
MIN_SPEED_KBPS = 120         # 下载速度低于此 KB/s 判定为卡（<120KB/s 踢）
PROBE_BYTES = 512 * 1024     # 探测下载量：512KB，用来算速度
# ==================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def probe_speed(url: str):
    """
    返回 (ok: bool, ttfb_ms: float, speed_kbps: float, reason: str)
    同时测：首字节时间 TTFB + 实际下载速度
    """
    try:
        t0 = time.time()
        resp = requests.get(url, timeout=CHECK_TIMEOUT, headers=HEADERS,
                            allow_redirects=True, stream=True)
        resp.raise_for_status()

        # 第一个字节到达时间（TTFB）
        ttfb = (time.time() - t0) * 1000

        # 拉取一段数据算速度
        downloaded = 0
        data_start = time.time()
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded >= PROBE_BYTES:
                break
            # 下载中途如果总时长已超 timeout，停
            if time.time() - t0 > CHECK_TIMEOUT:
                break
        elapsed = time.time() - data_start
        resp.close()

        if downloaded < 32 * 1024:
            return False, ttfb, 0, "下载数据过少"

        speed_kbps = (downloaded / 1024) / max(elapsed, 0.001)

        if ttfb > MAX_TTFB_MS:
            return False, ttfb, speed_kbps, f"TTFB过高({ttfb:.0f}ms>{MAX_TTFB_MS})"
        if speed_kbps < MIN_SPEED_KBPS:
            return False, ttfb, speed_kbps, f"速度过低({speed_kbps:.0f}KB/s<{MIN_SPEED_KBPS})"
        return True, ttfb, speed_kbps, "OK"

    except requests.exceptions.RequestException as e:
        return False, 0, 0, f"连接失败:{type(e).__name__}"


def probe_m3u8_deep(url: str):
    """
    对 HLS(.m3u8) 源做深检：
    1. 拉 playlist
    2. 取最后一个分片（最接近实时直播的那片）
    3. 下载该片测耗时
    返回 (ok, ttfb_ms, speed_kbps, reason)
    """
    try:
        t0 = time.time()
        r = requests.get(url, timeout=CHECK_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        playlist_ttfb = (time.time() - t0) * 1000

        lines = [l.strip() for l in r.text.splitlines() if l.strip() and not l.startswith("#EXT-X-DISCONTINUATION")]
        # 找最后一个分片地址
        seg_url = None
        for l in reversed(lines):
            if not l.startswith("#"):
                seg_url = l
                break
        if not seg_url:
            return False, playlist_ttfb, 0, "m3u8内无分片"

        # 解析相对路径
        if seg_url.startswith("http"):
            full_url = seg_url
        else:
            from urllib.parse import urljoin
            full_url = urljoin(url, seg_url)

        # 下载该分片
        seg_t0 = time.time()
        sr = requests.get(full_url, timeout=CHECK_TIMEOUT, headers=HEADERS, stream=True)
        sr.raise_for_status()
        seg_ttfb = (time.time() - seg_t0) * 1000

        downloaded = 0
        dstart = time.time()
        for chunk in sr.iter_content(64 * 1024):
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded >= PROBE_BYTES:
                break
            if time.time() - seg_t0 > CHECK_TIMEOUT:
                break
        sr.close()
        elapsed = time.time() - dstart
        speed_kbps = (downloaded / 1024) / max(elapsed, 0.001)

        # 综合判定：playlist TTFB + 分片 TTFB
        total_ttfb = playlist_ttfb + seg_ttfb
        if total_ttfb > MAX_TTFB_MS * 2:
            return False, total_ttfb, speed_kbps, f"起播慢({total_ttfb:.0f}ms)"
        if speed_kbps < MIN_SPEED_KBPS:
            return False, total_ttfb, speed_kbps, f"分片速度低({speed_kbps:.0f}KB/s)"
        return True, total_ttfb, speed_kbps, "OK(m3u8)"

    except Exception as e:
        return False, 0, 0, f"m3u8深检失败:{type(e).__name__}"


def check_stream(url: str):
    """根据URL后缀选择检测策略"""
    u = url.lower().split("?")[0]
    if u.endswith(".m3u8"):
        return probe_m3u8_deep(url)
    return probe_speed(url)


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
    raw_list = []
    url_seen = set()

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

    valid_channels = []
    if ENABLE_CHECK and total_fetched > 0:
        print(f"🧪 开始深度检测（TTFB≤{MAX_TTFB_MS}ms, 速度≥{MIN_SPEED_KBPS}KB/s）")
        future_map = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for item in raw_list:
                future = executor.submit(check_stream, item[2])
                future_map[future] = item
            ok_cnt = bad_cnt = 0
            for future in as_completed(future_map):
                group, name, u = future_map[future]
                try:
                    ok, ttfb, speed, reason = future.result()
                except Exception as e:
                    ok, ttfb, speed, reason = False, 0, 0, str(e)
                if ok:
                    valid_channels.append((group, name, u, ttfb, speed))
                    ok_cnt += 1
                    print(f"  ✅ {name:<20} TTFB={ttfb:5.0f}ms  速度={speed:6.0f}KB/s")
                else:
                    bad_cnt += 1
                    print(f"  ❌ {name:<20} {reason}")
        print(f"\n📊 检测结果：有效 {ok_cnt} / 剔除 {bad_cnt} / 共 {total_fetched}")
    else:
        valid_channels = [(g, n, u, 0, 0) for g, n, u in raw_list]
        print("⚠️ 源检测已关闭")

    # ---- 排序：先按分组，再按频道名，同频道多源聚在一起；同频道内按 TTFB 升序(最快的排前面) ----
    valid_channels.sort(key=lambda x: (x[0], x[1], x[3]))
    print("🔢 已按 (分组, 频道名, TTFB升序) 排序，同频道最快源排最前")

    # ---- 输出 m3u8 ----
    output_m3u = ["#EXTM3U"]
    for group, cname, curl, ttfb, speed in valid_channels:
        # 同频道多源时，在名字后面标注延迟，方便播放器/你识别
        label = cname
        if ENABLE_CHECK:
            label = f"{cname}[{ttfb:.0f}ms]"
        fake_ext = f'#EXTINF:-1 group-title="{group}",{label}'
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
