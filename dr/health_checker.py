"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except Exception as e:  # ConnectError (stop) hoặc ReadTimeout (netblock)
        return False, type(e).__name__
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = r.json().get("reasons") or []
    except ValueError:
        reasons = []
    return False, f"http_{r.status_code}:" + ",".join(reasons)


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL.

    Mỗi region bắt đầu ở HEALTHY (giả định) và chỉ đổi trạng thái sau `threshold` lần
    probe LIÊN TIẾP cùng chiều — cả khi xuống (UNHEALTHY) lẫn khi lên lại (HEALTHY),
    để 1 probe lẻ không làm trạng thái lật qua lật lại.
    Ngủ đủ `interval` SAU mỗi vòng probe: chu kỳ thật = interval + thời gian probe,
    nên detect không bao giờ nhanh hơn interval × threshold.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    st = {r: {"state": "HEALTHY", "fails": 0, "oks": 0} for r in URL}
    end = time.time() + duration
    with out.open("a", encoding="utf-8") as f:
        while time.time() < end:
            for region, s in st.items():
                ok, reason = probe(region, timeout)
                if ok:
                    s["fails"], s["oks"] = 0, s["oks"] + 1
                else:
                    s["fails"], s["oks"] = s["fails"] + 1, 0
                new = s["state"]
                if s["state"] == "HEALTHY" and s["fails"] >= threshold:
                    new = "UNHEALTHY"
                elif s["state"] == "UNHEALTHY" and s["oks"] >= threshold:
                    new = "HEALTHY"
                if new == s["state"]:
                    continue
                rec = {"ts": time.time(),
                       "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                       "event": "state_change", "region": region,
                       "from": s["state"], "to": new, "reason": reason,
                       "consecutive_fails": s["fails"], "consecutive_oks": s["oks"],
                       "interval_s": interval, "threshold": threshold,
                       "timeout_s": timeout}
                s["state"] = new
                f.write(json.dumps(rec) + "\n")
                f.flush()
                print("HEALTH", json.dumps(rec), flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
