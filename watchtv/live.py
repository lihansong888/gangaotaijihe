import requests
import re
import os
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 【秒播+音画完整｜宁少勿滥】配置区 ==========
URL_LIST = [
    "https://raw.githubusercontent.com/CCSH/IPTV/refs/heads/main/live_lite.txt",
]
KEYWORD_GROUP = [
    ("港澳台", "HS港澳台"),
    ("纪录片直播", "HS纪录片直播"),
]

ENABLE_CHECK = True
HTTP_TIMEOUT = 2            # HTTP粗筛超时
FFMPEG_TIMEOUT = 2          # ffmpeg媒体探测总超时，超过直接丢弃
MAX_WORKERS = 8             # ffmpeg比较吃资源，并发降到8，不要开太高

# HTTP粗筛阈值（前置过滤，减少ffmpeg压力）
MAX_TTFB_MS = 400
MIN_SPEED_KBPS = 100
PROBE_BYTES = 128 * 1024

# ffmpeg媒体校验规则（核心！解决无声问题）
MAX_START_TIME_S = 1.5      # 媒体首帧出画面必须在1.5s内，大于则剔除
REQUIRE_AUDIO = True        # 强制要求必须有音频轨道，无声源直接丢弃
REQUIRE_VIDEO = True        # 强制要求必须有视频轨道
# ==================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def http_pre_check(url: str):
    """HTTP前置粗筛：快速淘汰连接失败、响应过慢源"""
    try:
        t0 = time.time()
        resp = requests.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS, allow_redirects=True, stream=True)
        resp.raise_for_status()
        ttfb = (time.time() - t0) * 1000

        downloaded = 0
        data_start = time.time()
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded >= PROBE_BYTES:
                break
            if time.time() - t0 > HTTP_TIMEOUT:
                break
        elapsed = time.time() - data_start
        resp.close()
        if downloaded < 32 * 1024:
            return False, ttfb, 0, "HTTP：下载数据过少"
        speed_kbps = (downloaded / 1024) / max(elapsed, 0.001)
        if ttfb > MAX_TTFB_MS:
            return False, ttfb, speed_kbps, f"HTTP：TTFB过高({ttfb:.0f}ms)"
        if speed_kbps < MIN_SPEED_KBPS:
            return False, ttfb, speed_kbps, f"HTTP：速度过低({speed_kbps:.0f}KB/s)"
        return True, ttfb, speed_kbps, "HTTP预通过"
    except requests.exceptions.RequestException as e:
        return False, 0, 0, f"HTTP失败:{type(e).__name__}"

def ffmpeg_media_check(url: str):
    """
    使用ffmpeg探测媒体流：
    1. 检测是否存在音轨、视频轨
    2. 检测首帧加载耗时（真实起播时间）
    返回: ok, start_time_s, has_video, has_audio, reason
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v", "error",
        "-ss", "0",
        "-i", url,
        "-t", "1",
        "-f", "null", "-",
    ]
    t_start = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = proc.communicate(timeout=FFMPEG_TIMEOUT)
        load_time = time.time() - t_start

        has_video = "Stream #0:.*Video:" in stderr
        has_audio = "Stream #0:.*Audio:" in stderr

        if REQUIRE_VIDEO and not has_video:
            return False, load_time, has_video, has_audio, "媒体：无视频轨道"
        if REQUIRE_AUDIO and not has_audio:
            return False, load_time, has_video, has_audio, "媒体：无音频轨道"
        if load_time > MAX_START_TIME_S:
            return False, load_time, has_video, has_audio, f"媒体：起播慢{load_time:.2f}s>{MAX_START_TIME_S}s"

        return True, load_time, has_video, has_audio, "媒体校验OK"
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, time.time()-t_start, False, False, "媒体探测超时"
    except Exception as e:
        return False, time.time()-t_start, False, False, f"媒体探测异常:{str(e)}"

def check_stream(url: str):
    """双层校验：先HTTP粗筛，通过再走ffmpeg媒体校验"""
    http_ok, ttfb, speed, http_msg = http_pre_check(url)
    if not http_ok:
        return False, ttfb, speed, 0, False, False, http_msg
    media_ok, media_load, has_video, has_audio, media_msg = ffmpeg_media_check(url)
    if media_ok:
        return True, ttfb, speed, media_load, has_video, has_audio, f"{http_msg} | {media_msg}"
    else:
        return False, ttfb, speed, media_load, has_video, has_audio, media_msg

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
        print(f"🧪 双层检测：HTTP粗筛 + FFmpeg音画校验 | 起播上限{MAX_START_TIME_S}s | 强制音+视频轨")
        future_map = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for item in raw_list:
                future = executor.submit(check_stream, item[2])
                future_map[future] = item
            ok_cnt = bad_cnt = 0
            for future in as_completed(future_map):
                group, name, u = future_map[future]
                try:
                    ok, ttfb, speed, media_t, has_v, has_a, reason = future.result()
                except Exception as e:
                    ok, ttfb, speed, media_t, has_v, has_a, reason = False,0,0,0,False,False,str(e)
                if ok:
                    valid_channels.append((group, name, u, ttfb, media_t))
                    ok_cnt += 1
                    print(f"  ✅ {name:<20} TTFB={ttfb:5.0f}ms 媒体耗时={media_t:.2f}s | {reason}")
                else:
                    bad_cnt += 1
                    print(f"  ❌ {name:<20} {reason}")
        print(f"\n📊 检测结果：音画齐全秒播 {ok_cnt} / 剔除 {bad_cnt} / 共 {total_fetched}")
    else:
        valid_channels = [(g, n, u, 0,0) for g, n, u in raw_list]

    # 排序：分组、频道名、真实媒体起播时间升序（最快在前）
    valid_channels.sort(key=lambda x: (x[0], x[1], x[4]))

    output_m3u = ["#EXTM3U"]
    for group, cname, curl, ttfb, media_t in valid_channels:
        label = f"{cname}[{media_t:.2f}s]"
        fake_ext = f'#EXTINF:-1 group-title="{group}",{label}'
        output_m3u.append(fake_ext)
        output_m3u.append(curl)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    m3u8_path = os.path.join(out_dir, "live.m3u8")
    with open(m3u8_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_m3u))

    print(f"\n🎉 完成！音画齐全秒播频道：{len(valid_channels)}")
    print(f"📁 文件：{m3u8_path}")

if __name__ == "__main__":
    main()
