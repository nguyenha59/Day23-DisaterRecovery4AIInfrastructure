# Postmortem — DR Drill Lab 23 (region-a outage, 2026-10-09)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** 04:35:31 UTC region-a bị netblock (SIGSTOP). Health checker phát hiện sau 18.9s,
runbook bán tự động (`--auto` trong drill) failover sang region-b, user nhận request thành công
đầu tiên từ region-b sau **35.1s**. Mất **7 document** (RPO 14.0s). 16 request user bị lỗi
(ReadTimeout ~2s mỗi request).

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | +s | Sự kiện | Evidence |
|---|---:|---|---|
| 2026-10-09T04:35:31 | 0 | outage bắt đầu (kill region-a, netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T04:35:33 | 2.0 | user đầu tiên bị ảnh hưởng (ReadTimeout 2022ms) | `reports/drill-2-withdr.jsonl:27` |
| 2026-10-09T04:35:50 | 18.9 | health check alert: region-a `UNHEALTHY` (3 lần fail liên tiếp) | `reports/health-events.jsonl:2` |
| 2026-10-09T04:35:59 | 27.6 | runbook xác nhận outage + mở incident SEV1 (notify delay 27.6s) | `reports/runbook-run.jsonl:2` |
| 2026-10-09T04:35:59 | 27.6 | operator confirm cutover → failover bắt đầu (`1_verify_target`) | `reports/failover-events.jsonl:1` |
| 2026-10-09T04:35:59 | 27.7 | snapshot restore xong: RPO 14.0s, 7 doc mất, `embed-model=vi-e5-base@v3` | `reports/failover-events.jsonl:2` |
| 2026-10-09T04:36:05 | 34.3 | region-b `/readyz` 200 sau warm-up 6.55s | `reports/failover-events.jsonl:4` |
| 2026-10-09T04:36:05 | 34.3 | DNS cutover `a → b` | `reports/failover-events.jsonl:5` |
| 2026-10-09T04:36:06 | 34.7 | golden signals region-b: 10/10 OK, p95 70.7ms, error rate 0 | `reports/runbook-run.jsonl:6` |
| 2026-10-09T04:36:06 | 35.1 | resolved — request đầu tiên OK, `served_by:b` | `reports/drill-2-withdr.jsonl:43` |

(`reports/runbook-run.jsonl` là log nội bộ của runbook, bị `.gitignore` bỏ qua; các mốc chính
đều có bản sao trong `reports/failover-events.jsonl`.)

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `35.1s` · gap: `-264.9s` (đạt, còn dư 88% budget)
- RPO mục tiêu: 300s · đo được: `14.0s` (`7` doc bị mất) · gap: `-286.0s` (đạt)
- **Bước tốn nhiều giây nhất:** `health-check detect` — 18.9s = 54% RTO. Vì sao: floor cứng
  `interval × threshold = 5 × 3 = 15s`, cộng thêm mỗi probe netblock phải chờ hết timeout 2s,
  và kill rơi giữa 2 vòng poll. Bước tốn thứ hai là **runbook xác nhận lại outage (8.7s)** — runbook
  tự probe lại 3 lần dù health checker đã khẳng định 3 lần fail; đây là phần trùng lặp, không
  thêm an toàn đáng kể.
- RPO 14.0s là may mắn về thời điểm: replicate chạy mỗi 30s, nên RPO trường hợp xấu nhất ≈ 30s
  (~15 doc ở 0.5 doc/s).

## 3. Root cause (5 whys)

*Nếu đây là outage thật, bước nào trong runbook sẽ thất bại?*

1. **Vì sao user lỗi 35s?** Vì không có region nào nhận traffic cho tới khi DNS cutover ở +34.3s.
2. **Vì sao cutover muộn?** Vì region-b ở dạng *pilot light*: không weights, vector DB rỗng
   (`count=0`, `reports/failover-events.jsonl:1`) → phải restore + scale + warm-up GPU sau khi
   đã phát hiện sự cố, và phát hiện thì bị chặn dưới bởi floor 15s.
3. **Vì sao mất 7 doc?** Vì replication là snapshot định kỳ 30s (async, không phải streaming);
   mọi doc ghi sau snapshot cuối đều nằm ngoài region-b.
4. **Vì sao trong outage thật sẽ tệ hơn?** (a) Trong drill, ingest vẫn ghi được vào SQLite của
   region-a nên `snapshot.rpo()` đọc được primary để đếm `docs_lost`. Khi region chết thật, file
   primary không đọc được → **chỉ biết RPO ≤ 30s, không biết chính xác doc nào mất**.
   (b) Health checker, edge và replica cùng chạy trên một máy → nếu "máy" đó là region chết,
   không ai báo động. (c) Không có reverse replication: doc ghi vào region-b sau cutover sẽ
   mất nếu rollback về region-a mà không đồng bộ ngược.
5. **Vì sao hệ thống cho phép điều đó?** Vì thiết kế hiện tại tối ưu chi phí (region phụ rỗng)
   và chưa có ai sở hữu chỉ số RPO "đo được khi primary mất hẳn" — chỉ có RPO đo trong drill.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Runbook tin alert của health checker, chỉ re-probe 1 lần (thay vì 3 lần × 2s timeout) | SRE on-call lead | 2026-10-16 | RTO −~6s |
| 2 | Health check `interval=2s, threshold=3, timeout=1s` + hysteresis khi lên lại (giữ chống flap) | Platform team | 2026-10-23 | RTO −~10s (floor 15s → 6s) |
| 3 | Chuyển region-b từ pilot light sang warm standby: weights + snapshot luôn pre-staged, chỉ còn scale pool | Infra / ML platform | 2026-11-06 | RTO −~7s (bỏ restore + phần lớn warm-up) |
| 4 | Replicate mỗi 10s (hoặc streaming/CDC cho vector DB) và ghi `last_replicated_doc_ts` vào MANIFEST để tính `docs_lost` không cần đọc primary | Data platform | 2026-11-13 | RPO worst-case 30s → 10s |
| 5 | Chạy health checker ở vị trí độc lập (region thứ 3 / external probe), không cùng host với serving | SRE | 2026-11-20 | Không giảm số giây — loại bỏ trường hợp RTO = ∞ khi checker chết cùng region |
| 6 | Reverse replication b → a + checklist rollback trước khi trả traffic về region-a | Data platform | 2026-11-20 | Tránh mất doc lần 2 khi failback |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   `5s × 3 = 15.0s` → 15.0 / 35.1 = **42.7%** RTO (chỉ riêng floor). Thời gian phát hiện thực
   tế là 18.9s = **53.8%** RTO, vì mỗi probe netblock tốn thêm 2s timeout và kill rơi lệch pha
   với vòng poll. Đây là sàn: không cấu hình nào khác (restore, TTL) có thể làm RTO < 15s.
   Với RTO 5 phút, về lý thuyết được phép interval tới ~(300 − 8.7 − 0.1 − 6.6 − 5) / 3 ≈ 93s,
   nhưng nên giữ ≤ 10s để còn dư cho outage thật (restore lâu hơn nhiều so với fs local).

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   Floor từ 15s → 3s, RTO giảm khoảng **12s** (35.1s → ~23s; thêm ~3s nữa nếu timeout hạ 1s).
   Giá phải trả: (a) probe load gấp 5 lần lên `/readyz` của cả hai region; (b) cửa sổ 3 lần fail
   chỉ còn 3s — một GC pause, một lần deploy rolling, hay network blip 3s là đủ để báo
   `UNHEALTHY` giả → failover không cần thiết, mất RPO (14s dữ liệu) mà không có outage thật;
   (c) nếu failback cũng tự động với ngưỡng ngắn, hai region **flap** qua lại. Cách bù: giữ
   failover bán tự động (confirm), thêm hysteresis/cooldown và circuit breaker cho failback.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` có nghĩa gì
   với khách hàng?** `docs_lost=7` nghĩa là 7 document khách đã upload thành công (đã nhận
   200 OK) **biến mất vĩnh viễn** — RAG sẽ trả lời như thể chúng chưa từng tồn tại, không báo
   lỗi. Với tốc độ ingest 0.5 doc/s, worst case là ~15 doc (30s chu kỳ replicate). Cần:
   thông báo khách hàng bị ảnh hưởng (dựa trên `doc_ingested_at` > `restored_latest_doc_ts`
   trong `reports/failover-events.jsonl:2`), yêu cầu upload lại, và — nếu không chấp nhận được —
   chuyển sang replication đồng bộ cho đường ghi (đổi lấy write latency cao hơn).
