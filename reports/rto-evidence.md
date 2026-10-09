# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Môi trường drill: bare mode (WSL Ubuntu, uvicorn), `--mock`, `chaos --mode netblock` (SIGSTOP),
`EDGE_TTL_SECONDS=5`, `WARMUP_SECONDS=6`, health check `interval=5s, threshold=3, timeout=2s`,
replicate `--every 30 --backend fs`, ingest `--rate 0.5`.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:34:35` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.3s` (ReadTimeout, 2023.0ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage (16/33 request fail) | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0, `04:35:31`) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +2.0 | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:27` |
| Health check phát hiện | +18.9 | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +27.7 | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +34.3 | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +34.3 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+35.1** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:43` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `35.1s` | 300s (5 phút) | **PASS** (còn dư 264.9s) |
| RPO — Vector DB | `14.0s` / `7` doc | 300s (5 phút) | **PASS** (còn dư 286.0s) |

RPO lấy từ `rpo_seconds` / `docs_lost` của `reports/failover-events.jsonl:2`: snapshot được dùng
là chu kỳ replicate lúc `04:35:43` (`reports/replication.jsonl:2`), trong khi ingest vẫn ghi vào
region A tới lúc restore → 7 doc cuối nằm ngoài snapshot.

## 3. RTO của tôi gồm những gì

Các thành phần cộng lại đúng bằng RTO đo được: 18.9 + 8.7 + 0.1 + 6.6 + 0.8 = **35.1s**.

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 18.9s (floor 15.0s + 3.9s lệch pha/timeout) | `interval_s × threshold` = 5 × 3 trong `reports/health-events.jsonl:2`; phần dư là 2s probe timeout mỗi vòng + kill rơi giữa 2 vòng poll | Hạ interval (vd 2s → floor 6s) hoặc timeout 1s; giữ threshold=3 để chống flapping |
| Runbook xác nhận outage + mở incident | 8.7s | 18.9 → 27.6: runbook poll alert mỗi 1s rồi tự probe lại 3 lần (timeout 2s + gap 1s) — `reports/failover-events.jsonl:1` | Tin alert của health checker, chỉ re-probe 1 lần; hoặc probe song song |
| Snapshot restore | 0.1s | 2_restore → 3_scale (`reports/failover-events.jsonl:2` → `:3`) | Đã nhỏ ở fs backend; với S3/MinIO thật cần pre-stage snapshot ở region phụ |
| GPU pool warm-up | 6.6s | `waited_s=6.55` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) + 0.03s ghi cutover | Giữ region phụ ở `pool_state=warm` có sẵn weights (pilot light → warm standby) |
| DNS/LB TTL cache | 0.8s | t_recovered − t_cutover = 35.1 − 34.3 | Hạ `EDGE_TTL_SECONDS`; lần này cache tình cờ sắp hết hạn nên chỉ mất 0.8s, trường hợp xấu nhất là 5s |
