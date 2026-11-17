"""
Agent 2: Data Cleaning & Human-in-the-Loop Execution Agent (Agent Làm sạch & Trình duyệt Kết quả)
Nhiệm vụ:
- Nhận danh sách quy tắc làm sạch (Cleaning Plan) đã được người dùng cấu hình
- Thực thi tuần tự các bước làm sạch trên bản sao dữ liệu
- Ghi lại Nhật ký Biến đổi (Cell-level & Row-level Audit Log) để người dùng đối chiếu Trước vs Sau
- Hỗ trợ thực thi thêm chỉ thị ngôn ngữ tự nhiên của người dùng (Custom Instruction)
- Tính toán lại Data Health Score sau khi làm sạch để người dùng Phê duyệt (Approve)
"""

import re
import pandas as pd
import numpy as np
from typing import Dict, List, Any, Tuple
from agent.profiler_agent import DataProfilerAgent, safe_float


class DataCleaningAgent:
    """Agent thực thi quy trình làm sạch dữ liệu và xuất kết quả trung gian để người dùng phê duyệt."""

    def __init__(self):
        self.profiler = DataProfilerAgent()

    def apply_cleaning_plan(
        self,
        raw_df: pd.DataFrame,
        approved_rules: List[Dict[str, Any]],
        custom_instruction: str = ""
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Thực thi các quy tắc làm sạch dữ liệu và trả về DataFrame đã làm sạch + Báo cáo đối chiếu chi tiết.
        """
        df = raw_df.copy()
        # Chuẩn hóa chuỗi rỗng thành NaN
        for col in df.columns:
            if pd.api.types.is_string_dtype(df[col]) or df[col].dtype == object:
                df[col] = df[col].apply(
                    lambda x: np.nan if (isinstance(x, str) and x.strip() == "") else x
                )

        initial_rows = len(df)
        execution_summary: List[Dict[str, Any]] = []
        cell_changes: List[Dict[str, Any]] = []
        rows_dropped_total = 0

        # Lưu bản chụp trước khi biến đổi ô để đánh dấu trên giao diện
        for rule in approved_rules:
            if not rule.get("enabled", True):
                execution_summary.append({
                    "rule_id": rule.get("id"),
                    "column": rule.get("column_label", rule.get("column")),
                    "issue_badge": rule.get("issue_badge", ""),
                    "strategy": "Bỏ qua (Người dùng tắt)",
                    "status": "skipped",
                    "rows_affected": 0,
                    "detail": "Người dùng chọn giữ nguyên bước này."
                })
                continue

            issue_type = rule.get("issue_type")
            col = rule.get("column")
            strategy = rule.get("strategy", "keep")

            if strategy == "keep":
                execution_summary.append({
                    "rule_id": rule.get("id"),
                    "column": rule.get("column_label", col),
                    "issue_badge": rule.get("issue_badge", ""),
                    "strategy": "Giữ nguyên (Keep)",
                    "status": "skipped",
                    "rows_affected": 0,
                    "detail": "Giữ nguyên dữ liệu gốc theo yêu cầu."
                })
                continue

            # 1. Xử lý bản ghi trùng lặp
            if issue_type == "duplicate_rows" and strategy == "drop_duplicates":
                dup_mask = df.duplicated(keep="first")
                n_dups = int(dup_mask.sum())
                if n_dups > 0:
                    dup_indices = [int(i) + 1 for i in df[dup_mask].index.tolist()]
                    df = df[~dup_mask].reset_index(drop=True)
                    rows_dropped_total += n_dups
                    execution_summary.append({
                        "rule_id": rule.get("id"),
                        "column": "Toàn bộ bảng",
                        "issue_badge": "Trùng lặp bản ghi",
                        "strategy": "Xóa dòng trùng lặp",
                        "status": "applied",
                        "rows_affected": n_dups,
                        "detail": f"Đã loại bỏ {n_dups} dòng trùng lặp hoàn toàn (dòng gốc: {dup_indices[:5]})."
                    })
                continue

            if col not in df.columns:
                continue

            # 2. Xử lý định dạng chuỗi & đồng nhất danh mục
            if issue_type == "text_formatting":
                s = df[col]
                non_null_mask = s.notna()
                old_vals = s.copy()

                if strategy == "strip_only":
                    df.loc[non_null_mask, col] = s[non_null_mask].astype(str).str.strip()
                elif strategy == "title_case_strip":
                    df.loc[non_null_mask, col] = (
                        s[non_null_mask]
                        .astype(str)
                        .str.strip()
                        .apply(lambda x: " ".join(w.capitalize() for w in x.split()))
                    )
                elif strategy == "canonical_clean":
                    stripped = s[non_null_mask].astype(str).str.strip()
                    # Tìm dạng viết chuẩn cho từng nhóm chữ thường (ưu tiên dạng có viết hoa đầu từ đúng chuẩn)
                    def pick_canonical(group: pd.Series) -> str:
                        counts = group.value_counts()
                        # Ưu tiên giá trị không phải toàn chữ thường/toàn chữ hoa nếu có
                        for candidate in counts.index:
                            if not candidate.islower() and not candidate.isupper():
                                return candidate
                        best = counts.index[0]
                        return " ".join(w.capitalize() for w in best.split())

                    mode_map = stripped.groupby(stripped.str.lower()).agg(pick_canonical).to_dict()
                    df.loc[non_null_mask, col] = stripped.str.lower().map(mode_map)

                changed_mask = non_null_mask & (old_vals.astype(str) != df[col].astype(str))
                n_changed = int(changed_mask.sum())
                for idx in df[changed_mask].index[:15]:
                    cell_changes.append({
                        "row_index": int(idx) + 1,
                        "column": col,
                        "before": str(old_vals.loc[idx]),
                        "after": str(df.loc[idx, col]),
                        "reason": "Chuẩn hóa khoảng trắng & chữ Hoa/thường"
                    })
                execution_summary.append({
                    "rule_id": rule.get("id"),
                    "column": col,
                    "issue_badge": "Lỗi định dạng chuỗi",
                    "strategy": strategy,
                    "status": "applied",
                    "rows_affected": n_changed,
                    "detail": f"Đã chuẩn hóa định dạng chuỗi cho {n_changed} ô dữ liệu cột '{col}'."
                })

            # 3. Chuẩn hóa định dạng ngày tháng
            elif issue_type == "date_formatting" and strategy == "iso_date":
                s = df[col]
                non_null_mask = s.notna()
                old_vals = s.copy()
                parsed_dates = pd.to_datetime(s[non_null_mask].astype(str).str.strip().str.replace("/", "-"), errors="coerce")
                valid_parsed = parsed_dates.notna()
                for idx in parsed_dates[valid_parsed].index:
                    iso_str = parsed_dates.loc[idx].strftime("%Y-%m-%d")
                    if str(old_vals.loc[idx]).strip() != iso_str:
                        cell_changes.append({
                            "row_index": int(idx) + 1,
                            "column": col,
                            "before": str(old_vals.loc[idx]),
                            "after": iso_str,
                            "reason": "Chuẩn hóa ngày ISO (YYYY-MM-DD)"
                        })
                    df.loc[idx, col] = iso_str

                n_changed = int((old_vals[non_null_mask].astype(str) != df.loc[non_null_mask, col].astype(str)).sum())
                execution_summary.append({
                    "rule_id": rule.get("id"),
                    "column": col,
                    "issue_badge": "Lệch chuẩn ngày tháng",
                    "strategy": "ISO 8601 (YYYY-MM-DD)",
                    "status": "applied",
                    "rows_affected": n_changed,
                    "detail": f"Đã đồng nhất {n_changed} giá trị ngày tháng về chuẩn YYYY-MM-DD."
                })

            # 4. Xử lý vi phạm miền giá trị logic (Số âm, vượt thang đo)
            elif issue_type == "domain_violation":
                num_s = pd.to_numeric(df[col], errors="coerce")
                dom_check = self.profiler.detect_domain_bound_violation(col, num_s)
                if dom_check.get("has_violation"):
                    v_mask = dom_check["mask"]
                    n_viol = int(v_mask.sum())
                    bounds = dom_check.get("domain_bounds", {})
                    valid_vals = num_s[~v_mask].dropna()
                    med_val = float(valid_vals.median()) if len(valid_vals) > 0 else 0.0
                    is_int_col = (valid_vals % 1 == 0).all() if len(valid_vals) > 0 else False

                    if strategy == "drop_rows":
                        df = df[~v_mask].reset_index(drop=True)
                        rows_dropped_total += n_viol
                        execution_summary.append({
                            "rule_id": rule.get("id"),
                            "column": col,
                            "issue_badge": "Sai miền giá trị logic",
                            "strategy": "Xóa dòng vi phạm",
                            "status": "applied",
                            "rows_affected": n_viol,
                            "detail": f"Đã xóa {n_viol} dòng vi phạm miền giá trị tại cột '{col}'."
                        })
                    else:
                        for idx in df[v_mask].index:
                            old_v = num_s.loc[idx]
                            new_v = old_v
                            if strategy == "abs_or_clip":
                                if old_v < 0:
                                    new_v = abs(old_v)
                                    # Nếu trị tuyệt đối bằng 0 hoặc vẫn bất hợp lý (ví dụ số năm kinh nghiệm âm -> lấy trị tuyệt đối)
                                if "max" in bounds and new_v > bounds["max"]:
                                    new_v = bounds["max"]
                                if "min" in bounds and new_v < bounds["min"]:
                                    new_v = med_val
                            elif strategy == "clip_domain":
                                min_b = bounds.get("min", float(valid_vals.min()) if len(valid_vals) else 0.0)
                                max_b = bounds.get("max", float(valid_vals.max()) if len(valid_vals) else old_v)
                                new_v = min(max(old_v, min_b), max_b)
                            elif strategy == "median_replace":
                                new_v = med_val

                            new_v = int(round(new_v)) if is_int_col else round(float(new_v), 2)
                            df.loc[idx, col] = new_v
                            cell_changes.append({
                                "row_index": int(idx) + 1,
                                "column": col,
                                "before": safe_float(old_v),
                                "after": safe_float(new_v),
                                "reason": f"Hiệu chỉnh giá trị sai miền logic ({strategy})"
                            })

                        execution_summary.append({
                            "rule_id": rule.get("id"),
                            "column": col,
                            "issue_badge": "Sai miền giá trị logic",
                            "strategy": strategy,
                            "status": "applied",
                            "rows_affected": n_viol,
                            "detail": f"Đã hiệu chỉnh {n_viol} giá trị vi phạm miền logic tại cột '{col}'."
                        })

            # 5. Xử lý điểm ngoại lai (Outliers theo IQR)
            elif issue_type == "outliers_iqr":
                num_s = pd.to_numeric(df[col], errors="coerce")
                iqr_bounds = rule.get("iqr_bounds", {})
                valid_s = num_s.dropna()
                if len(valid_s) >= 4:
                    q25 = float(valid_s.quantile(0.25))
                    q75 = float(valid_s.quantile(0.75))
                    iqr = q75 - q25
                    detect_low = iqr_bounds.get("detect_lower", round(q25 - 2.5 * iqr, 2))
                    detect_high = iqr_bounds.get("detect_upper", round(q75 + 2.5 * iqr, 2))
                    lower_f = iqr_bounds.get("lower", round(q25 - 1.5 * iqr, 2))
                    upper_f = iqr_bounds.get("upper", round(q75 + 1.5 * iqr, 2))
                    med_val = iqr_bounds.get("median", round(float(valid_s.median()), 2))
                    out_mask = num_s.notna() & ((num_s < detect_low) | (num_s > detect_high))
                    n_out = int(out_mask.sum())
                    is_int_col = (valid_s % 1 == 0).all()

                    if n_out > 0:
                        if strategy == "drop_rows":
                            df = df[~out_mask].reset_index(drop=True)
                            rows_dropped_total += n_out
                            execution_summary.append({
                                "rule_id": rule.get("id"),
                                "column": col,
                                "issue_badge": "Điểm ngoại lai (Outlier)",
                                "strategy": "Loại bỏ dòng ngoại lai",
                                "status": "applied",
                                "rows_affected": n_out,
                                "detail": f"Đã loại bỏ {n_out} dòng chứa ngoại lai cực đoan ở cột '{col}'."
                            })
                        else:
                            for idx in df[out_mask].index:
                                old_v = float(num_s.loc[idx])
                                if strategy == "iqr_clip":
                                    new_v = min(max(old_v, lower_f), upper_f)
                                else:
                                    new_v = med_val
                                new_v = int(round(new_v)) if is_int_col else round(float(new_v), 2)
                                df.loc[idx, col] = new_v
                                cell_changes.append({
                                    "row_index": int(idx) + 1,
                                    "column": col,
                                    "before": safe_float(old_v),
                                    "after": safe_float(new_v),
                                    "reason": f"Xử lý ngoại lai IQR ({strategy})"
                                })

                            # Nếu cột quantity được điều chỉnh và có cột revenue + unit_price, đồng bộ lại doanh thu hợp lý
                            if col == "quantity" and "unit_price" in df.columns and "revenue" in df.columns:
                                for idx in df[out_mask].index:
                                    up = pd.to_numeric(df.loc[idx, "unit_price"], errors="coerce")
                                    disc = pd.to_numeric(df.loc[idx, "discount_pct"], errors="coerce") if "discount_pct" in df.columns else 0
                                    disc = 0 if pd.isna(disc) else disc
                                    if pd.notna(up):
                                        old_rev = df.loc[idx, "revenue"]
                                        new_rev = int(round(float(df.loc[idx, col]) * float(up) * (1.0 - float(disc) / 100.0)))
                                        df.loc[idx, "revenue"] = new_rev
                                        cell_changes.append({
                                            "row_index": int(idx) + 1,
                                            "column": "revenue",
                                            "before": safe_float(old_rev),
                                            "after": safe_float(new_rev),
                                            "reason": "Tự động đồng bộ lại Doanh thu sau khi xử lý ngoại lai Số lượng"
                                        })

                            execution_summary.append({
                                "rule_id": rule.get("id"),
                                "column": col,
                                "issue_badge": "Điểm ngoại lai (Outlier)",
                                "strategy": strategy,
                                "status": "applied",
                                "rows_affected": n_out,
                                "detail": f"Đã xử lý {n_out} điểm ngoại lai ở cột '{col}' bằng phương pháp {strategy}."
                            })

            # 6. Xử lý dữ liệu bị thiếu (Missing Values)
            elif issue_type == "missing_values":
                na_mask = df[col].isna()
                n_na = int(na_mask.sum())
                if n_na > 0:
                    if strategy == "drop_rows":
                        df = df[~na_mask].reset_index(drop=True)
                        rows_dropped_total += n_na
                        execution_summary.append({
                            "rule_id": rule.get("id"),
                            "column": col,
                            "issue_badge": "Thiếu dữ liệu (Missing)",
                            "strategy": "Xóa dòng thiếu",
                            "status": "applied",
                            "rows_affected": n_na,
                            "detail": f"Đã xóa {n_na} dòng bị trống ở cột '{col}'."
                        })
                    else:
                        sem_type = self.profiler.infer_column_type(df[col], col)
                        if sem_type == "numeric":
                            num_s = pd.to_numeric(df[col], errors="coerce")
                            valid_s = num_s.dropna()
                            is_int_col = (valid_s % 1 == 0).all() if len(valid_s) > 0 else False
                            if strategy == "median":
                                fill_val = float(valid_s.median()) if len(valid_s) > 0 else 0.0
                            elif strategy == "mean":
                                fill_val = float(valid_s.mean()) if len(valid_s) > 0 else 0.0
                            else:
                                fill_val = 0.0

                            fill_val = int(round(fill_val)) if is_int_col else round(fill_val, 2)
                            for idx in df[na_mask].index:
                                actual_fill = fill_val
                                # Suy diễn thông minh nếu thiếu unit_price mà có revenue và quantity
                                if col == "unit_price" and "revenue" in df.columns and "quantity" in df.columns:
                                    rev_v = pd.to_numeric(df.loc[idx, "revenue"], errors="coerce")
                                    qty_v = pd.to_numeric(df.loc[idx, "quantity"], errors="coerce")
                                    disc_v = pd.to_numeric(df.loc[idx, "discount_pct"], errors="coerce") if "discount_pct" in df.columns else 0
                                    disc_v = 0 if pd.isna(disc_v) else disc_v
                                    if pd.notna(rev_v) and pd.notna(qty_v) and qty_v > 0 and (1 - disc_v / 100.0) > 0:
                                        actual_fill = int(round(rev_v / (qty_v * (1.0 - disc_v / 100.0))))

                                df.loc[idx, col] = actual_fill
                                cell_changes.append({
                                    "row_index": int(idx) + 1,
                                    "column": col,
                                    "before": "NULL (Trống)",
                                    "after": safe_float(actual_fill),
                                    "reason": f"Điền khuyết dữ liệu số ({strategy})"
                                })
                        else:
                            non_null_s = df[col].dropna().astype(str).str.strip()
                            if strategy == "mode" and len(non_null_s) > 0:
                                fill_val = non_null_s.mode().iloc[0]
                            else:
                                fill_val = "Chưa xác định"
                            for idx in df[na_mask].index:
                                df.loc[idx, col] = fill_val
                                cell_changes.append({
                                    "row_index": int(idx) + 1,
                                    "column": col,
                                    "before": "NULL (Trống)",
                                    "after": str(fill_val),
                                    "reason": f"Điền khuyết phân loại ({strategy})"
                                })

                        execution_summary.append({
                            "rule_id": rule.get("id"),
                            "column": col,
                            "issue_badge": "Thiếu dữ liệu (Missing)",
                            "strategy": strategy,
                            "status": "applied",
                            "rows_affected": n_na,
                            "detail": f"Đã điền khuyết {n_na} ô trống tại cột '{col}' bằng chiến lược '{strategy}'."
                        })

        # 7. Thực thi chỉ thị ngôn ngữ tự nhiên tùy chỉnh của người dùng (nếu có)
        if custom_instruction and custom_instruction.strip():
            instr = custom_instruction.strip()
            instr_lower = instr.lower()
            rows_before_custom = len(df)
            applied_custom_note = ""

            # Hỗ trợ lọc bỏ dòng theo từ khóa
            if any(k in instr_lower for k in ["xóa", "xoá", "loại bỏ", "lọc bỏ", "bỏ"]):
                for col in df.columns:
                    if df[col].dtype == object:
                        unique_vals = df[col].dropna().unique()
                        for val in unique_vals:
                            if str(val).lower() in instr_lower:
                                mask_drop = df[col].astype(str).str.lower() == str(val).lower()
                                cnt = int(mask_drop.sum())
                                if cnt > 0:
                                    df = df[~mask_drop].reset_index(drop=True)
                                    rows_dropped_total += cnt
                                    applied_custom_note = f"Đã lọc bỏ {cnt} dòng có {col} = '{val}' theo chỉ thị người dùng."
                                    break

            if not applied_custom_note:
                applied_custom_note = f"Đã ghi nhận chỉ thị bổ sung của người dùng: '{instr}' vào ngữ cảnh phân tích."

            execution_summary.append({
                "rule_id": "custom_user_rule",
                "column": "Chỉ thị Người dùng",
                "issue_badge": "Tùy chỉnh (Human-in-the-Loop)",
                "strategy": "Natural Language Rule",
                "status": "applied",
                "rows_affected": rows_before_custom - len(df),
                "detail": applied_custom_note
            })

        # Chuẩn hóa lại kiểu dữ liệu số sau khi làm sạch
        for col in df.columns:
            sem_t = self.profiler.infer_column_type(df[col], col)
            if sem_t == "numeric":
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Đánh giá lại sức khỏe dữ liệu sau khi làm sạch
        post_profile = self.profiler.profile_dataset(df, "cleaned_dataset.csv")
        cleaned_preview = df.head(25).replace({np.nan: None}).to_dict(orient="records")

        cleaning_report = {
            "rows_before": initial_rows,
            "rows_after": len(df),
            "rows_dropped": rows_dropped_total,
            "cells_modified_count": len(cell_changes),
            "health_after": post_profile["health_score"],
            "remaining_issues_count": post_profile["total_issues_detected"],
            "execution_summary": execution_summary,
            "cell_changes": cell_changes[:40],
            "cleaned_preview": cleaned_preview,
            "cleaned_columns_profile": post_profile["columns_profile"],
        }

        return df, cleaning_report
