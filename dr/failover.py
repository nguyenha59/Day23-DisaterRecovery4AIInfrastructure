"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str) -> dict:
    return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()


def _abort(out: dict, step: str, reason: str) -> dict:
    emit(step=step, ok=False, aborted=True, reason=reason, target=out["target"])
    out.update(ok=False, aborted_at=step, reason=reason)
    return out


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Trả về dict có `ok`; ok=False = KHÔNG cutover."""
    primary = "a" if target == "b" else "b"
    t_start = time.time()
    out = {"ok": False, "target": target, "primary": primary, "backend": backend}

    # 1. Region đích phải còn sống mới đáng để restore vào.
    try:
        before = state_of(target)
    except Exception as e:
        return _abort(out, "1_verify_target", f"target khong phan hoi /v1/state: {type(e).__name__}")
    emit(step="1_verify_target", target=target, pool_state=before.get("pool_state"),
         weights=before.get("weights"), count=before.get("count"))
    out["target_state_before"] = before

    # 2. Restore vector DB + weights + embed model version, rồi đo RPO thật.
    try:
        meta = snapshot.get(target, backend)
    except SystemExit as e:  # snapshot.get raise SystemExit khi chưa từng có `put`
        return _abort(out, "2_restore_snapshot", str(e))
    rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                       pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", target=target, backend=backend,
         rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"),
         primary_latest_doc_ts=rpo["primary_latest_doc_ts"],
         restored_latest_doc_ts=rpo["restored_latest_doc_ts"])
    out.update(rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
               embed_model_version=meta.get("embed_model_version"))

    # 3. warm -> full. Giữ giá trị cũ để trả lại nếu bước 4 abort.
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    prev_pool = pool.read_text() if pool.exists() else None
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full")
    emit(step="3_scale_pool", target=target, from_state=(prev_pool or "").strip() or None,
         to_state="full")

    # 4. Chờ /readyz 200 (gồm cả GPU warm-up). Timeout -> ABORT, không đụng DNS.
    t_wait, last = time.time(), "chua probe"
    ready = False
    while time.time() - t_wait < wait:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if r.status_code == 200:
                ready = True
                break
            last = ",".join(r.json().get("reasons") or []) or f"http_{r.status_code}"
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.5)
    waited = round(time.time() - t_wait, 2)
    if not ready:
        if prev_pool is None:
            pool.unlink(missing_ok=True)
        else:
            pool.write_text(prev_pool)
        out["waited_s"] = waited
        return _abort(out, "4_wait_ready",
                      f"target khong ready sau {waited}s (last={last}) -> KHONG cutover, "
                      f"pool_state tra ve {(prev_pool or 'cold').strip()}")
    emit(step="4_wait_ready", target=target, ok=True, waited_s=waited)
    out["waited_s"] = waited
    try:
        out["target_state"] = state_of(target)
    except Exception:
        out["target_state"] = None

    # 5. Chỉ bây giờ mới đổi "DNS".
    prev_active = ACTIVE.read_text().strip() if ACTIVE.exists() else None
    ACTIVE.write_text(target)
    emit(step="5_dns_cutover", from_region=prev_active, to_region=target,
         elapsed_s=round(time.time() - t_start, 2))
    out.update(ok=True, active_region=target, elapsed_s=round(time.time() - t_start, 2))
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
