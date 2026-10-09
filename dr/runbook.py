"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


from dr import health_checker as hc  # noqa: E402

CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _t_outage(primary: str):
    """ts của lần kill gần nhất vào primary (drill). Outage thật thì không có -> None."""
    kills = [e for e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == primary]
    return kills[-1]["ts"] if kills else None


def _wait_alert(primary: str, since: float, timeout: float):
    """Chờ health checker bắn UNHEALTHY cho primary (alert). Trả về event hoặc None."""
    end = time.time() + timeout
    while time.time() < end:
        ev = [e for e in _jsonl(HEALTH) if e.get("event") == "state_change"
              and e.get("region") == primary and e.get("ts", 0) >= since]
        if ev and ev[-1].get("to") == "UNHEALTHY":
            return ev[-1]
        time.sleep(1.0)
    return None


def _confirm_outage(primary: str, probes: int = 3, gap: float = 1.0) -> list[str]:
    """Probe trực tiếp nhiều lần; outage chỉ khi TẤT CẢ đều fail."""
    reasons = []
    for i in range(probes):
        ok, reason = hc.probe(primary, 2.0)
        if ok:
            return []
        reasons.append(reason)
        if i < probes - 1:
            time.sleep(gap)
    return reasons


def _golden_signals(target: str, n: int = 10) -> dict:
    lat, errors = [], 0
    for _ in range(n):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            if r.status_code != 200:
                errors += 1
        except Exception:
            errors += 1
        lat.append((time.time() - t0) * 1000)
    lat.sort()
    p95 = lat[max(0, -(-len(lat) * 95 // 100) - 1)]  # nearest-rank
    return {"requests": n, "errors": errors, "error_rate": round(errors / n, 3),
            "p95_ms": round(p95, 1), "max_ms": round(lat[-1], 1)}


def run(primary: str, target: str, backend: str, auto: bool,
        alert_timeout: float = 90.0) -> dict:
    """7 bước của runbook §4. Trả về tóm tắt; chi tiết nằm trong LOG."""
    t_start = time.time()
    t_outage = _t_outage(primary)
    result = {"ok": False, "primary": primary, "target": target}

    # 1. Xác nhận outage: chờ alert từ health checker, rồi tự probe lại để chắc chắn.
    alert = _wait_alert(primary, since=t_outage or (t_start - 300), timeout=alert_timeout)
    reasons = _confirm_outage(primary)
    if not reasons:
        step(1, "xac_nhan_outage", region=primary, confirmed=False,
             alert_ts=alert and alert["ts"], note="primary van ready -> KHONG failover")
        result["reason"] = "outage_not_confirmed"
        return result
    step(1, "xac_nhan_outage", region=primary, confirmed=True,
         alert_ts=alert and alert["ts"], alert_source="health_checker" if alert else "direct_probe",
         probe_reasons=reasons)

    # 2. Mở incident, bấm giờ.
    now = time.time()
    step(2, "thong_bao_incident", t_outage=t_outage, t_operator_known=now,
         notify_delay_s=None if t_outage is None else round(now - t_outage, 2),
         severity="SEV1", summary=f"region-{primary} down, failover sang region-{target}")

    if not confirm(auto, f"Failover tu region-{primary} sang region-{target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator tu choi confirm")
        result["reason"] = "operator_declined"
        return result

    # 3. Failover: gọi ĐÚNG 1 lần, nó tự làm 5 bước con.
    fr = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", failover_ok=fr.get("ok"), aborted_at=fr.get("aborted_at"),
         waited_s=fr.get("waited_s"), failover_log=str(fo.LOG))

    # 4. Chỉ đọc lại state replica từ kết quả bước 3.
    ts = fr.get("target_state") or {}
    step(4, "verify_state_replica", region=target, vector_count=ts.get("count"),
         weights=ts.get("weights"), pool_state=ts.get("pool_state"),
         embed_model_version=fr.get("embed_model_version"),
         rpo_seconds=fr.get("rpo_seconds"), docs_lost=fr.get("docs_lost"))

    # 5. Chỉ đọc lại kết quả cutover.
    step(5, "dns_cutover", ok=bool(fr.get("ok")), active_region=fr.get("active_region"),
         reason=fr.get("reason"))

    # 6. Golden signals trên region phụ (chỉ khi đã cutover).
    gs = _golden_signals(target) if fr.get("ok") else None
    step(6, "verify_golden_signals", region=target, skipped=gs is None, **(gs or {}))

    # 7. Tổng kết.
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", elapsed_s=elapsed,
         since_outage_s=None if t_outage is None else round(time.time() - t_outage, 2),
         failover_ok=bool(fr.get("ok")),
         next="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    result.update(ok=bool(fr.get("ok")), elapsed_s=elapsed, failover=fr, golden_signals=gs)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--alert-timeout", type=float, default=90.0,
                   help="giay cho health checker bao UNHEALTHY truoc khi tu probe")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto, a.alert_timeout), indent=2))
