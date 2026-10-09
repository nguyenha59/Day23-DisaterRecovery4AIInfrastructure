# Runbook 1 trang — Region chính (A) down → failover sang Region B

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mọi lệnh chạy từ thư mục gốc repo.

**Đường nhanh (khuyến nghị):** một lệnh, bán tự động — tự làm bước 1→7, dừng hỏi `y/N` trước khi cutover:

```bash
python3 dr/runbook.py --primary a --target b --backend fs
```

Log từng bước: `reports/runbook-run.jsonl`; 5 bước con của failover: `reports/failover-events.jsonl`.
Chỉ dùng `--auto` trong drill/CI. Nếu script lỗi giữa chừng → làm tay theo bảng dưới, từ bước đã dừng.

**Vai trò:** On-call SRE (thực thi) · Incident Commander — IC (quyết định cutover + rollback) · Data owner (xác nhận RPO / doc mất).

| # | Bước | Lệnh (copy-paste) | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `tail -n 3 reports/health-events.jsonl` rồi probe tay 3 lần: `for i in 1 2 3; do curl -s -m 2 -o /dev/null -w '%{http_code}\n' localhost:8001/readyz; sleep 1; done` | Health log có `"region": "a", "to": "UNHEALTHY"` **và** cả 3 probe trả `000` (timeout) hoặc `503`. Nếu có 1 probe `200` → **DỪNG**, không phải outage | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `date -u +%FT%TZ; tail -n 2 chaos/chaos-events.jsonl` — tạo kênh incident SEV1, ghi `t_operator_known` | Có incident SEV1 + IC được chỉ định; ts ghi vào `reports/runbook-run.jsonl` (step 2, `notify_delay_s`) | On-call SRE → IC |
| 3 | Restore state ở region phụ | `curl -s localhost:8002/v1/state` (xem trước) rồi `python3 state/snapshot.py get --region b --backend fs` | `curl -s localhost:8002/v1/state` cho `"weights": true` và `count > 0`; ghi lại `rpo_seconds`, `docs_lost`, `embed_model_version` (phải khớp `vi-e5-base@v3`) | On-call SRE · Data owner ký RPO |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state; until [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' localhost:8002/readyz)" = 200 ]; do sleep 0.5; done; echo READY` | In ra `READY` (`/readyz` của b trả 200, thường ~6–7s warm-up). Quá **60s** chưa READY → **ABORT, KHÔNG làm bước 5**, trả `printf warm > state/region-b/pool_state`, escalate | On-call SRE |
| 5 | DNS/LB cutover | IC confirm, rồi: `printf b > edge/active_region; curl -s localhost:8080/edge/state` | `curl -s localhost:8080/edge/state` cho `"active_region":"b"`; chờ thêm ≤ `EDGE_TTL_SECONDS` (5s) cho cache edge hết hạn | On-call SRE (IC duyệt) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -m 3 -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8080/v1/infer; done` | 10/10 trả `200`, p95 < **500ms** (drill đo được 70.7ms), error rate < **1%**, response có `"edge_region":"b"` | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` (outage thật: dùng log request của edge/APM thay cho loadgen) | `rto_verdict` = `PASS` (≠ null), `valid: true`, `warnings: []`; mở `reports/postmortem.md` trong 48h | IC → on-call viết postmortem |

## Rollback / failback về Region A

**Không bao giờ tự động.** §4 Anti-Patterns: failback full-auto không có circuit breaker → hai region flap qua lại, mỗi lần flap lại mất một khoảng RPO.

**Rollback ngay trong lúc failover (abort):** nếu bước 4 không READY trong 60s, hoặc bước 6 có error rate ≥ 1% / p95 ≥ 500ms trên B **và** A đã `/readyz` 200 trở lại → IC quyết định trả `edge/active_region` về `a` (`printf a > edge/active_region`). Nếu cả hai region đều không serve được → giữ nguyên, escalate SEV0.

**Failback có kế hoạch về A** — chỉ khi đủ **tất cả** điều kiện:

1. Region A `HEALTHY` trong `reports/health-events.jsonl` (≥ 3 probe OK liên tiếp) và ổn định liên tục ≥ **30 phút**.
2. Root cause của outage A đã được xác định và khắc phục (không failback vào một region vẫn còn lỗi).
3. Doc ghi vào B trong thời gian outage đã được sync ngược sang A (reverse snapshot `state/snapshot.py put` từ B → `get` vào A) và Data owner xác nhận `count` của A ≥ `count` của B.
4. Làm trong khung giờ traffic thấp, đã thông báo trước.

**Ai có quyền quyết định:** Incident Commander (hoặc Engineering Manager on-call nếu IC vắng mặt). On-call SRE **không** tự failback một mình.

**Lệnh failback:** `python3 dr/failover.py --target a --backend fs` (cùng 5 bước, cùng guard: không cutover nếu A chưa ready) → sau đó lặp lại bước 6 cho A.

**Kill switch cho drill:** `python3 chaos/kill_region.py restore --region a --backend bare` (SIGCONT); nếu dùng `--mode stop` thì chạy lại `bash scripts/up_bare.sh`.
