# Five-minute demo outline

1. **0:00–0:30 Problem and constraints.** 5M TMN, 3 days, spot only, autonomous, final TMN. Show `docs/ARCHITECTURE.md` diagram: AI chooses, deterministic risk decides size, executor owns orders.
2. **0:30–1:15 Reproducibility.** `cat evidence/manifest.json` — git commit, config hash, prompt hash, model id. `pytest -q` (78 tests).
3. **1:15–2:15 One decision end to end.** `curl :8787/decisions` → pick a SELECT row → open its snapshot in `evidence/snapshots/` → AI JSON (regime, confidence, reason, evidence ids) → `risk_json` with every check → order row → fills → position. Show `export/trade_trace.json`.
4. **2:15–2:50 A rejection and a HOLD.** A RISK_REJECTED row (e.g. spread or exposure cap) and a deterministic HOLD with no model call.
5. **2:50–3:30 Failure handling.** Run `pytest tests/test_executor.py -k timeout` and `tests/test_ai.py -k failures`: ambiguous POST reconciled without resubmission; AI timeout → HOLD while exits continue.
6. **3:30–4:15 Evaluation honesty.** `reports/EVALUATION.md`: train/test split, negative held-out result, fee sensitivity, 3-day episode distribution, AI comparison (if run).
7. **4:15–5:00 Terminal liquidation.** `tests/test_engine.py::test_final_liquidation...` and the live `evidence/final_report.json` (TMN balance, dust listed separately).
