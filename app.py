import streamlit as st
import datetime
import os
import requests
import pandas as pd
import numpy as np
import io
import csv
import re
# 确保以下自定义模块在同一目录下
from ta_api import TAClient
from data_processor import clean_sql_response
from analysis_lib import AdAnalysis
from data_analyser import DataAnalyser

# ---------------------------------------------------------------------------
# 架构约定（与业务对齐）
# 1）「全量汇总」与「广告计划/广告组/广告创意」是两套独立 SQL，互不混用。
# 2）广告三种共用同一条 cohort 细粒度 SQL：用户按 te_ads_object 归因到计划/组/创意（通常三层有值），
#    无法归因的为「自然量」；侧边栏三种选项只改变下方「应用层合并粒度」，不改变 SQL 文本与 API 返回的原始明细。
# 3）维度穿透视图与原始数据明细 + 筛选 UI 共用；全量下 raw 为 Date×OS 粒度，广告下 raw 为创意级细粒度。
# ---------------------------------------------------------------------------
# 广告 SQL 原始结果 → 仅按展示粒度做 sum（指标可累加至上一层级）
COHORT_SUM_COLS = [
    "Cost", "Plot UV", "ECPM_Null", "ECPM_0_100", "ECPM_100_200", "ECPM_200_300", "ECPM_300_400", "ECPM_400_500", "ECPM_500+",
    "L10 UV", "L20 UV", "L30 UV", "L40 UV", "L50 UV", "L60 UV", "L70 UV", "L80 UV", "L90 UV", "L100 UV",
    "IAP UV", "IAP_UV_D0", "IAP Times", "IAP Revenue", "Ad UV", "Ad Revenue", "total_amount",
]

EXPECTED_SQL_COLS = [
    'Date', 'Dimension Value', 'Media Source', 'OS',
    '维度名称_全部', '维度名称_广告计划', '维度名称_广告组', '维度名称_广告创意',
    'Cost', 'Plot UV',
    'ECPM_Null', 'ECPM_0_100', 'ECPM_100_200', 'ECPM_200_300',
    'ECPM_300_400', 'ECPM_400_500', 'ECPM_500+',
    'L10 UV', 'L20 UV', 'L30 UV', 'L40 UV', 'L50 UV', 'L60 UV', 'L70 UV', 'L80 UV', 'L90 UV', 'L100 UV',
    'IAP UV', 'IAP_UV_D0', 'IAP Times', 'IAP Revenue', 'Ad UV', 'Ad Revenue', 'total_amount', 'group_num_0', 'group_num'
]

_LABEL_COHORT_ALL = "（全部）"


def aggregate_cohort_by_dim_choice(df, dim_choice):
    """
    仅用于「广告计划 / 广告组 / 广告创意」：对 get_cohort_fine_grain_sql 的同一份结果做展示粒度合并。
    不改变 SQL；cohort 归因逻辑（含自然量）均在 SQL 内完成。
    归集在较粗一层时，更细层级列在展示上留空；本层及以上填「（全部）」+ 对应键值。
    """
    if df is None or df.empty:
        return df
    d = df.copy()
    sum_cols = [c for c in COHORT_SUM_COLS if c in d.columns]
    for c in sum_cols:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    col_plan, col_grp, col_cre = "维度名称_广告计划", "维度名称_广告组", "维度名称_广告创意"
    for c in (col_plan, col_grp, col_cre, "维度名称_全部"):
        if c not in d.columns:
            d[c] = ""
    # 全量汇总不得走本函数（由 prepare_absolute_summary_df 处理）
    ad_level = {"广告计划": 1, "广告组": 2, "广告创意": 3}
    if dim_choice not in ad_level:
        return d
    level = ad_level[dim_choice]
    if not sum_cols:
        return d

    def _norm_key(series):
        return series.fillna("").astype(str)

    if level == 1:
        keys = ["Date", "OS", "Media Source", col_plan]
        d = d.copy()
        d["Date"] = _norm_key(d["Date"])
        d["OS"] = _norm_key(d["OS"])
        d["Media Source"] = _norm_key(d["Media Source"])
        d[col_plan] = _norm_key(d[col_plan])
        g = d.groupby(keys, as_index=False)[sum_cols].sum()
        g["维度名称_全部"] = _LABEL_COHORT_ALL
        g[col_grp] = ""
        g[col_cre] = ""
        g["Dimension Value"] = g[col_plan].astype(str)
    elif level == 2:
        keys = ["Date", "OS", "Media Source", col_plan, col_grp]
        d = d.copy()
        d["Date"] = _norm_key(d["Date"])
        d["OS"] = _norm_key(d["OS"])
        d["Media Source"] = _norm_key(d["Media Source"])
        d[col_plan] = _norm_key(d[col_plan])
        d[col_grp] = _norm_key(d[col_grp])
        g = d.groupby(keys, as_index=False)[sum_cols].sum()
        g["维度名称_全部"] = _LABEL_COHORT_ALL
        g[col_cre] = ""
        g["Dimension Value"] = g[col_grp].astype(str)
    else:  # level == 3 广告创意：不合并行，仅补层级展示列
        g = d.copy()
        g["维度名称_全部"] = _LABEL_COHORT_ALL
        g["Dimension Value"] = g[col_cre].astype(str) if col_cre in g.columns else ""

    for c in ("group_num_0", "group_num"):
        if c in g.columns:
            g.drop(columns=[c], inplace=True, errors="ignore")
    return g


def prepare_absolute_summary_df(df):
    """
    全量汇总 SQL 已按 Date+OS 合并消耗与行为，此处只补全层级展示列与筛选用 Dimension Value（不再做 groupby）。
    """
    if df is None or df.empty:
        return df
    g = df.copy()
    col_plan, col_grp, col_cre = "维度名称_广告计划", "维度名称_广告组", "维度名称_广告创意"
    if "Media Source" not in g.columns:
        g["Media Source"] = ""
    for c in (col_plan, col_grp, col_cre, "维度名称_全部"):
        if c not in g.columns:
            g[c] = ""
    g["Media Source"] = g["Media Source"].fillna("").astype(str)
    g["维度名称_全部"] = _LABEL_COHORT_ALL
    g[col_plan] = ""
    g[col_grp] = ""
    g[col_cre] = ""
    g["Dimension Value"] = _LABEL_COHORT_ALL
    for c in ("group_num_0", "group_num"):
        if c in g.columns:
            g.drop(columns=[c], inplace=True, errors="ignore")
    return g


def sort_for_display(df):
    """统一展示排序：Date -> OS -> 广告计划 -> 广告组 -> 广告创意，全部倒序。"""
    if df is None or df.empty:
        return df
    d = df.copy()
    sort_cols = ["Date", "OS", "维度名称_广告计划", "维度名称_广告组", "维度名称_广告创意"]
    use_cols = [c for c in sort_cols if c in d.columns]
    if not use_cols:
        return d
    for c in use_cols:
        d[c] = d[c].fillna("").astype(str)
    return d.sort_values(by=use_cols, ascending=[False] * len(use_cols), kind="mergesort")


# --- AI 解读：从 txt 读取 prompt / 配置，按 OS 与维度汇总后调 SiliconFlow ---
def _load_prompt_txt():
    path = os.path.join(os.path.dirname(__file__), "ai_interpret_prompt.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""

def _load_siliconflow_config():
    path = os.path.join(os.path.dirname(__file__), "siliconflow_config.txt")
    cfg = {"model": "", "base_url": "https://api.siliconflow.cn/v1", "max_tokens": 2000, "temperature": 0.7}
    if not os.path.isfile(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k in cfg:
                    cfg[k] = v if k in ("model", "base_url") else (int(v) if k == "max_tokens" else float(v))
    return cfg

def _build_data_summary(df):
    """基于本次查询全量数据（不按筛选），按总体 / OS / 维度名汇总，供 AI 解读"""
    if df is None or df.empty:
        return "（无数据）"
    sum_cols = [c for c in ["Cost", "Total Revenue", "IAP Revenue", "Plot UV", "IAP UV", "IAP_UV_D0", "L20 UV"] if c in df.columns]
    ecpm_cols = [c for c in ["ECPM_100_200", "ECPM_200_300", "ECPM_300_400", "ECPM_400_500", "ECPM_500+"] if c in df.columns]
    if sum_cols:
        sum_cols = sum_cols + ecpm_cols
    if not sum_cols:
        return "（无可汇总指标）"
    def add_derived(g):
        if "Cost" in g.columns and g["Cost"].sum():
            g = g.copy()
            g["ROI"] = g["Total Revenue"] / g["Cost"] if "Total Revenue" in g.columns else np.nan
            g["CPA_Plot"] = g["Cost"] / g["Plot UV"].replace(0, np.nan) if "Plot UV" in g.columns else np.nan
            if "L20 UV" in g.columns and "Plot UV" in g.columns:
                g["L20_Pass_Rate"] = g["L20 UV"] / g["Plot UV"].replace(0, np.nan)
            if ecpm_cols and "Plot UV" in g.columns:
                g["High_ECPM_Rate"] = g[ecpm_cols].sum(axis=1) / g["Plot UV"].replace(0, np.nan)
        return g
    def row_text(name, r):
        parts = [f"Cost={r.get('Cost', 0):.2f}", f"Total Revenue={r.get('Total Revenue', 0):.2f}"]
        if not pd.isna(r.get("ROI")): parts.append(f"ROI={r['ROI']:.2%}")
        parts.append(f"Plot UV={r.get('Plot UV', 0):.0f}"); parts.append(f"IAP UV={r.get('IAP UV', 0):.0f}")
        if "IAP_UV_D0" in r.index: parts.append(f"IAP_UV_D0={r.get('IAP_UV_D0', 0):.0f}")
        if not pd.isna(r.get("L20_Pass_Rate")): parts.append(f"L20通过率={r['L20_Pass_Rate']:.1%}")
        if not pd.isna(r.get("High_ECPM_Rate")): parts.append(f"高质量占比={r['High_ECPM_Rate']:.1%}")
        return name + "： " + "，".join(parts)
    lines = []
    total = df[sum_cols].sum()
    total["ROI"] = total["Total Revenue"] / total["Cost"] if total.get("Cost") else np.nan
    total["CPA_Plot"] = total["Cost"] / total["Plot UV"] if total.get("Plot UV") else np.nan
    total["L20_Pass_Rate"] = total["L20 UV"] / total["Plot UV"] if total.get("Plot UV") else np.nan
    if ecpm_cols and total.get("Plot UV"):
        total["High_ECPM_Rate"] = sum(total.get(c, 0) for c in ecpm_cols) / total["Plot UV"]
    lines.append("【总体】")
    lines.append(row_text("汇总", total))
    if "OS" in df.columns:
        by_os = df.groupby("OS", dropna=False)[sum_cols].sum()
        by_os = add_derived(by_os)
        lines.append("\n【按 OS】")
        for os_name, r in by_os.iterrows():
            lines.append(row_text(str(os_name), r))
    if "Dimension Value" in df.columns:
        by_dim = df.groupby("Dimension Value", dropna=False)[sum_cols].sum()
        by_dim = add_derived(by_dim)
        lines.append("\n【按维度名称】")
        for dim_name, r in by_dim.iterrows():
            lines.append(row_text(str(dim_name), r))
    return "\n".join(lines)

# 追问时强制限定在数据解读范围的系统说明（会加入每次请求）
_AI_SCOPE_SYSTEM = "你是一位投放数据分析师。你的所有回答（包括首次解读与后续追问）必须严格限定在用户提供的本次查询数据解读范围内，仅基于已给数据作答，不要脱离数据编造或泛化。"

def _call_siliconflow_chat(api_key, config, messages):
    """用 SiliconFlow 多轮对话，messages 为 [{"role":"user"|"assistant"|"system", "content":"..."}, ...]，返回助手回复文本或错误信息"""
    if not api_key or not config.get("model"):
        return None, "未配置 SiliconFlow API Key 或模型名（见 secrets 与 siliconflow_config.txt）"
    base = (config.get("base_url") or "https://api.siliconflow.cn/v1").rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": config["model"],
        "messages": messages,
        "max_tokens": config.get("max_tokens", 2000),
        "temperature": config.get("temperature", 0.7),
    }
    try:
        r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
        if r.status_code != 200:
            return None, f"API 返回 {r.status_code}: {r.text[:200]}"
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None, "API 未返回内容"
        msg = choice.get("message") or {}
        return (msg.get("content") or "").strip(), None
    except Exception as e:
        return None, str(e)

# 1. 页面基础配置
st.set_page_config(
    page_title="ROI 智能分析系统 (Cohort)", 
    layout="wide", 
    initial_sidebar_state="expanded"
)

# --- 登录（临时关闭）---
# if "logged_in" not in st.session_state:
#     st.session_state["logged_in"] = False
#
# def _check_login(username, password):
#     """从 Secrets 读取预期账号密码并校验（未配置则不允许登录）"""
#     try:
#         want_user = st.secrets.get("login_username", "")
#         want_pass = st.secrets.get("login_password", "")
#         if not want_user or not want_pass:
#             return False
#         return username == want_user and password == want_pass
#     except Exception:
#         return False
#
# if not st.session_state["logged_in"]:
#     st.title("ROI 智能分析系统 — 登录")
#     with st.form("login_form"):
#         login_user = st.text_input(
#             "用户名",
#             value=st.session_state.get("last_login_username", ""),
#             key="login_username_input",
#         )
#         login_pass = st.text_input(
#             "密码",
#             type="password",
#             value=st.session_state.get("last_login_password", ""),
#             key="login_password_input",
#         )
#         submitted = st.form_submit_button("登录")
#         if submitted:
#             if _check_login(login_user, login_pass):
#                 st.session_state["logged_in"] = True
#                 st.session_state["last_login_username"] = login_user
#                 st.session_state["last_login_password"] = login_pass
#                 st.rerun()
#             else:
#                 st.error("用户名或密码错误")
#     st.stop()
st.session_state["logged_in"] = True

# --- 移动端适配：小屏下优化侧边栏、字号与表格 ---
MOBILE_CSS = """
<style>
@media (max-width: 768px) {
    /* 主内容区减少左右边距，便于窄屏阅读 */
    [data-testid="stAppViewContainer"] { padding: 0.5rem; }
    /* 侧边栏在窄屏时更容易触控 */
    [data-testid="stSidebar"] { min-width: 260px; }
    [data-testid="stSidebar"] .stButton > button { width: 100%; }
    /* 指标卡片和按钮占满可用宽度 */
    [data-testid="stVerticalBlock"] > div [data-testid="stMetric"] { min-width: 0; }
    [data-testid="column"] { min-width: 0; }
    /* 表格横向滚动，避免撑破布局 */
    [data-testid="stDataFrame"] { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    /* 多选、输入等触控区域稍大 */
    .stMultiSelect, .stTextInput, .stNumberInput { font-size: 16px; }
}
</style>
"""
st.markdown(MOBILE_CSS, unsafe_allow_html=True)

# --- 2. 侧边栏配置 ---
def _get_token():
    """优先从 Streamlit Secrets 读取 Token，否则使用侧边栏输入"""
    try:
        token = st.secrets.get("ta_api_token", "")
        if token:
            return token
        ta = st.secrets.get("ta") or {}
        if isinstance(ta, dict):
            return ta.get("token", "")
    except (FileNotFoundError, AttributeError, TypeError):
        pass
    return ""

with st.sidebar:
    st.header("⚙️ 数据源配置")
    token_from_secrets = _get_token()
    if token_from_secrets:
        st.caption("✅ 已使用已配置的 TA API Token（来自 Secrets）")
        token = token_from_secrets
        token_override = st.text_input("TA API Token（留空则使用上方已配置）", type="password", help="临时覆盖时在此输入")
        if token_override:
            token = token_override
    else:
        token = st.text_input("TA API Token", type="password", help="请输入数数科技 API 调用令牌，或配置 .streamlit/secrets.toml")
    project_id = st.number_input("项目 ID", value=46)
    
    st.markdown("---")
    st.header("🔍 查询维度")
    dim_choice = st.radio(
        "选择统计维度",
        ["全量汇总", "广告计划", "广告组", "广告创意"],
        index=0,
        help="全量汇总 ≠ 广告三种：两套独立 SQL。广告三种共用同一条 cohort 细粒度 SQL，"
        "仅下方面板按所选层级合并展示；未归因用户为自然量（SQL 内已处理）。",
    )
    st.caption(
        "**全量**与**广告**两套查询逻辑；**广告计划/组/创意**共用一条 SQL，只改变表格合并粒度，不改变接口返回的原始明细结构。"
    )

    st.markdown("---")
    st.header("📅 Cohort 周期")
    # 默认选择最近 7 天
    default_start = datetime.date.today() - datetime.timedelta(days=7)
    default_end = datetime.date.today()
    d_range = st.date_input("选择新增批次范围", [default_start, default_end])
    debug_sql_mode = st.checkbox("调试模式（显示 SQL 与解析信息）", value=False)

    st.markdown("---")
    if st.button("退出登录", use_container_width=True):
        st.session_state["logged_in"] = False
        st.rerun()

# --- 3. 核心逻辑：点击查询时覆盖当前结果；筛选仅作用于当前结果 ---
if st.button("🚀 执行 Cohort 深度分析", use_container_width=True):
    if not token:
        st.warning("⚠️ 请先在侧边栏输入 API Token")
        st.stop()
    start_s = d_range[0].strftime('%Y-%m-%d')
    end_s = d_range[1].strftime('%Y-%m-%d') if len(d_range) > 1 else start_s
    client = TAClient("https://ta-open.jackpotlandslots.com", token)
    with st.spinner(f"正在分析 {start_s} 至 {end_s} 的新增批次数据..."):
        # 全量：消耗按 app_id OS 与 cohort 行为分轨再合并；广告：细粒度计划×组×创意（与用户 cohort 对齐）
        if dim_choice == "全量汇总":
            sql = AdAnalysis.get_absolute_summary_sql(project_id, start_s, end_s)
        else:
            sql = AdAnalysis.get_cohort_fine_grain_sql(project_id, start_s, end_s)
        raw_text, error = client.execute_query(sql)
        if error:
            st.error(f"❌ SQL 执行错误: {error}")
            if debug_sql_mode:
                st.code(sql[:4000], language="sql")
            st.stop()
        if debug_sql_mode:
            st.markdown("### 调试信息")
            st.caption("SQL（前 4000 字符）")
            st.code(sql[:4000], language="sql")
            raw_preview = (raw_text or "")
            st.caption(f"raw_text 长度: {len(raw_preview)}")
            if raw_preview:
                preview_lines = "\n".join(raw_preview.splitlines()[:8])
                st.caption("raw_text 前 8 行：")
                st.code(preview_lines)
        df_raw_detail = clean_sql_response(raw_text)
        fallback_valid_rows = 0
        # 兜底：主解析为空时，逐行按 CSV 解析并仅保留 36 列的有效行
        if df_raw_detail.empty and raw_text:
            valid_rows = []
            for ln in (raw_text or "").splitlines():
                line = (ln or "").strip()
                if not line:
                    continue
                try:
                    parsed = next(csv.reader([line]))
                except Exception:
                    continue
                if len(parsed) == len(EXPECTED_SQL_COLS):
                    valid_rows.append(parsed)
            fallback_valid_rows = len(valid_rows)
            if fallback_valid_rows > 0:
                df_raw_detail = pd.DataFrame(valid_rows, columns=EXPECTED_SQL_COLS)
        # 再兜底：从混合文本中提取以日期开头的 CSV 行（兼容返回夹杂日志/提示）
        if df_raw_detail.empty and raw_text:
            extracted_rows = []
            for m in re.finditer(r'^"20\d{2}-\d{2}-\d{2}".*$', raw_text or "", flags=re.MULTILINE):
                line = m.group(0).strip()
                try:
                    parsed = next(csv.reader([line]))
                except Exception:
                    continue
                if len(parsed) >= len(EXPECTED_SQL_COLS):
                    extracted_rows.append(parsed[:len(EXPECTED_SQL_COLS)])
            if extracted_rows:
                fallback_valid_rows = len(extracted_rows)
                df_raw_detail = pd.DataFrame(extracted_rows, columns=EXPECTED_SQL_COLS)
        if debug_sql_mode:
            st.caption(f"clean_sql_response 后 shape: {df_raw_detail.shape}")
            if not df_raw_detail.empty:
                st.caption("解析后前 5 行：")
                st.dataframe(df_raw_detail.head(5), use_container_width=True)
    if df_raw_detail.empty:
        st.info("📭 该范围内暂无新增用户数据")
        if raw_text:
            raw_lines = [ln for ln in (raw_text or "").splitlines() if (ln or "").strip()]
            st.caption(f"诊断：raw_text 非空，行数={len(raw_lines)}；二次兜底有效行数={fallback_valid_rows}")
            st.code("\n".join(raw_lines[:5]))
        st.caption("已自动执行诊断 SQL，帮助定位是否被过滤条件影响。")
        diag_sql = AdAnalysis.get_empty_result_diagnosis_sql(project_id, start_s, end_s)
        diag_raw_text, diag_error = client.execute_query(diag_sql)
        if diag_error:
            st.warning(f"诊断 SQL 执行失败: {diag_error}")
        else:
            try:
                diag_df = pd.read_csv(io.StringIO((diag_raw_text or "").strip()), engine="python")
                if not diag_df.empty:
                    st.markdown("#### 诊断结果")
                    st.dataframe(diag_df, use_container_width=True, hide_index=True)
                else:
                    st.caption("诊断 SQL 返回为空。")
            except Exception:
                st.markdown("#### 诊断结果（原始返回）")
                st.code((diag_raw_text or "")[:2000])
        st.stop()
    if dim_choice == "全量汇总":
        df_agg = prepare_absolute_summary_df(df_raw_detail)
    else:
        df_agg = aggregate_cohort_by_dim_choice(df_raw_detail, dim_choice)
    df_analysed = DataAnalyser.perform_business_analysis(df_agg)
    # 覆盖当前查询结果：全量 raw=Date×OS；广告 raw=创意级细粒度（三种选项 SQL 相同）
    st.session_state["cohort_df_raw"] = df_raw_detail
    st.session_state["cohort_df_analysed"] = df_analysed
    st.session_state["cohort_start_s"] = start_s
    st.session_state["cohort_end_s"] = end_s
    st.session_state["cohort_dim_choice"] = dim_choice
    # 每次重查时清空 AI 对话，避免对话基于旧数据
    if "ai_interpret_conversation" in st.session_state:
        del st.session_state["ai_interpret_conversation"]

if "cohort_df_analysed" in st.session_state:
    df_raw = st.session_state["cohort_df_raw"]
    df_analysed = st.session_state["cohort_df_analysed"].copy()
    start_s = st.session_state["cohort_start_s"]
    end_s = st.session_state["cohort_end_s"]
    dim_choice = st.session_state["cohort_dim_choice"]
    # 展示前兜底：若分析层未产出高质量占比，则用原始列在展示前算一列
    if "High_ECPM_Rate" not in df_analysed.columns:
        ecpm_cols = ['ECPM_100_200', 'ECPM_200_300', 'ECPM_300_400', 'ECPM_400_500', 'ECPM_500+']
        if all(c in df_analysed.columns for c in ecpm_cols) and 'Plot UV' in df_analysed.columns:
            high_sum = sum(df_analysed[c] for c in ecpm_cols)
            df_analysed['High_ECPM_Rate'] = np.where(
                df_analysed['Plot UV'] == 0, np.nan,
                high_sum / df_analysed['Plot UV']
            )
            df_analysed['High_ECPM_Rate'] = df_analysed['High_ECPM_Rate'].replace([np.inf, -np.inf], np.nan)
    metrics = DataAnalyser.get_summary_metrics(df_analysed)

    st.header("📊 业务分析层 (Cohort Based)")
    st.caption(f"分析逻辑：锁定 {start_s} ~ {end_s} 新增用户，统计其从激活至今的累积价值")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总消耗 (Cost)", f"${metrics['总消耗']:,.2f}")
    c2.metric("总营收 (Gross)", f"${metrics['总营收']:,.2f}")
    c3.metric("综合 ROI", f"{metrics['综合 ROI']:.2%}")
    c4.metric("总转化成本(CPA)", f"${metrics['总转化成本']:.2f}")
    c5.metric("IAP UV", f"{int(metrics['IAP UV 总数']):,}")
    c6.metric("IAP 转化成本", f"${metrics['IAP 转化成本']:.2f}")

    # AI 数据解读：基于本次查询全量数据（不考虑筛选）；支持最多 2 次追问，回答限定在数据解读范围
    st.subheader("🤖 AI 数据解读")
    try:
        api_key = st.secrets.get("siliconflow_api_key", "")
    except Exception:
        api_key = ""
    conv = st.session_state.get("ai_interpret_conversation", [])

    if st.button("生成解读", key="ai_interpret_btn"):
        prompt_text = _load_prompt_txt()
        if not prompt_text:
            st.warning("未找到 ai_interpret_prompt.txt，请在本项目根目录放置该文件并写入 prompt。")
        else:
            with st.spinner("正在调用 AI 生成解读…"):
                cfg = _load_siliconflow_config()
                summary = _build_data_summary(df_analysed)
                user_content = prompt_text + "\n\n以下是本次查询的数据汇总：\n" + summary + "\n\n请根据以上数据给出总体解读。"
                messages = [
                    {"role": "system", "content": _AI_SCOPE_SYSTEM},
                    {"role": "user", "content": user_content},
                ]
                content, err = _call_siliconflow_chat(api_key, cfg, messages)
                if err:
                    st.error("解读生成失败：" + err)
                else:
                    st.session_state["ai_interpret_conversation"] = [
                        {"role": "user", "content": user_content},
                        {"role": "assistant", "content": content},
                    ]
        st.rerun()

    # 展示已有对话：首次解读 + 追问与回答（不展示首条长 prompt）
    if conv:
        first_reply_shown = False
        for m in conv:
            if m["role"] == "assistant":
                st.markdown("**解读：**" if not first_reply_shown else "**答：**")
                st.markdown(m["content"])
                first_reply_shown = True
            else:
                if first_reply_shown:
                    st.markdown("**问：** " + m["content"])
        # 追问：每轮查询最多 2 个问题（不含首次“生成解读”）
        user_msgs = [m for m in conv if m["role"] == "user"]
        question_count = len(user_msgs) - 1
        can_ask = question_count < 2
        if can_ask:
            with st.form("ai_followup_form"):
                followup_q = st.text_input("追问（限定在本次数据解读范围内）", key="ai_followup_input")
                submitted = st.form_submit_button("提问")
                if submitted and followup_q.strip():
                    with st.spinner("正在生成回答…"):
                        cfg = _load_siliconflow_config()
                        new_conv = conv + [{"role": "user", "content": followup_q.strip()}]
                        messages = [{"role": "system", "content": _AI_SCOPE_SYSTEM}] + new_conv
                        content, err = _call_siliconflow_chat(api_key, cfg, messages)
                        if err:
                            st.error("追问回答失败：" + err)
                        else:
                            st.session_state["ai_interpret_conversation"] = new_conv + [{"role": "assistant", "content": content}]
                    st.rerun()
        else:
            st.caption("本轮已达 2 次追问上限，重新执行查询后可再次生成解读并追问。")

    st.subheader("维度穿透视图")
    st.caption(
        f"当前归集维度：**{dim_choice}** — 展示「（全部）」及本层以上层级；更细层级留空。"
    )
    if dim_choice == "全量汇总":
        st.caption(
            "全量汇总：消耗按 **appsflyer app_id** 拆 OS，与 cohort 行为（#os）**分轨 UNION 后按 Date+OS 汇总**，"
            "仅有消耗、无激活用户的部分仍会落在 iOS/Android/Unknown，不会出现因拼用户键导致的 OS 空。"
        )
    else:
        st.caption(
            "广告层级：**cohort 用户**按 `te_ads_object` 归因到计划/组/创意（通常三层有值），无归因记为**自然量**；"
            "当前选项仅决定**应用层合并粒度**，与本次查询返回的**原始明细（创意级）**一致。"
        )
    view_cols_wanted = [
        'Date', 'OS', 'Media Source',
        '维度名称_全部', '维度名称_广告计划', '维度名称_广告组', '维度名称_广告创意',
        'Plot UV', 'Cost', 'High_ECPM_Rate', 'HV_Rate', 'Over_ECPM_Rate', 'Total Revenue', 'IAP Revenue',
        'ROI', 'IAP_ROI', 'CPA_Plot', 'IAP UV', 'IAP_UV_D0', 'CPP_Pay', 'L20_Pass_Rate', 'CPA_L20', 'PUR',
    ]
    display_cols = [c for c in view_cols_wanted if c in df_analysed.columns]
    df_view = df_analysed.copy()
    if 'OS' in df_analysed.columns:
        options_os = sorted(df_analysed['OS'].dropna().astype(str).unique().tolist())
        selected_os = st.multiselect("筛选 OS", options=options_os, default=[], key="filter_os")
        if selected_os:
            df_view = df_view[df_view['OS'].astype(str).isin(selected_os)]
    # 广告层级支持按上层维度筛选
    if dim_choice in ("广告组", "广告创意") and '维度名称_广告计划' in df_view.columns:
        options_plan = sorted(df_view['维度名称_广告计划'].dropna().astype(str).unique().tolist())
        selected_plan = st.multiselect("筛选 广告计划", options=options_plan, default=[], key="filter_plan")
        if selected_plan:
            df_view = df_view[df_view['维度名称_广告计划'].astype(str).isin(selected_plan)]
    if dim_choice in ("广告组", "广告创意") and '维度名称_广告组' in df_view.columns:
        # 下层选项基于上层筛选后的 df_view 生成，实现级联控制
        options_group = sorted(df_view['维度名称_广告组'].dropna().astype(str).unique().tolist())
        selected_group = st.multiselect("筛选 广告组", options=options_group, default=[], key="filter_group")
        if selected_group:
            df_view = df_view[df_view['维度名称_广告组'].astype(str).isin(selected_group)]
    if 'Dimension Value' in df_analysed.columns:
        options_dim = sorted(df_analysed['Dimension Value'].dropna().astype(str).unique().tolist())
        selected_dim = st.multiselect("筛选 维度名称", options=options_dim, default=[], key="filter_dim")
        if selected_dim:
            df_view = df_view[df_view['Dimension Value'].astype(str).isin(selected_dim)]
    df_view = sort_for_display(df_view)
    display_cols = [c for c in view_cols_wanted if c in df_view.columns]
    rename_map = {
        'Media Source': '媒体源',
        '维度名称_全部': '全部',
        '维度名称_广告计划': '广告计划',
        '维度名称_广告组': '广告组',
        '维度名称_广告创意': '广告创意',
        'Plot UV': '激活人数',
        'High_ECPM_Rate': '中高质量占比',
        'HV_Rate': '高质量占比',
        'Over_ECPM_Rate': '超过质量占比',
        'IAP_ROI': 'IAP ROI',
        'CPA_Plot': '激活成本',
        'CPP_Pay': '付费成本',
        'L20_Pass_Rate': '20关通过率',
        'CPA_L20': '20关成本',
        'PUR': '付费率',
        'IAP_UV_D0': 'D0首充UV'
    }
    display_df = df_view[display_cols].rename(columns={k: v for k, v in rename_map.items() if k in display_cols})
    format_map = {
        'Cost': '${:,.2f}', '激活人数': '{:,.0f}',
        '中高质量占比': '{:.1%}', '高质量占比': '{:.1%}', '超过质量占比': '{:.1%}',
        'Total Revenue': '${:,.2f}', 'IAP Revenue': '${:,.2f}', 'ROI': '{:.2%}', 'IAP ROI': '{:.2%}',
        '激活成本': '${:.2f}', 'IAP UV': '{:,.0f}', 'D0首充UV': '{:,.0f}', '付费成本': '${:.2f}',
        '20关通过率': '{:.2%}', '20关成本': '${:.2f}', '付费率': '{:.2%}'
    }
    st.dataframe(
        display_df.style.format({k: v for k, v in format_map.items() if k in display_df.columns}, na_rep=''),
        use_container_width=True, hide_index=True
    )

    st.markdown("---")
    with st.expander("🔍 原始数据明细 (对账专用)", expanded=False):
        st.caption("以下筛选 **独立于** 上方「维度穿透视图」；未选时表示不过滤。数据来自本次查询缓存，不会重新请求 API。")
        st.write("此处展示 SQL 返回的原始字段（含 ECPM 分布及 L 等级 UV）")
        df_src = df_raw.copy()
        raw_selected_os, raw_selected_dim = [], []
        if "OS" in df_src.columns:
            raw_options_os = sorted(df_src["OS"].dropna().astype(str).unique().tolist())
            raw_selected_os = st.multiselect("筛选 OS（原始表）", options=raw_options_os, default=[], key="filter_raw_os")
        df_raw_display = df_src.copy()
        if raw_selected_os and "OS" in df_raw_display.columns:
            df_raw_display = df_raw_display[df_raw_display["OS"].astype(str).isin(raw_selected_os)]
        # 原始表同样支持广告上层维度筛选
        if dim_choice in ("广告组", "广告创意") and "维度名称_广告计划" in df_raw_display.columns:
            raw_options_plan = sorted(df_raw_display["维度名称_广告计划"].dropna().astype(str).unique().tolist())
            raw_selected_plan = st.multiselect("筛选 广告计划（原始表）", options=raw_options_plan, default=[], key="filter_raw_plan")
            if raw_selected_plan:
                df_raw_display = df_raw_display[df_raw_display["维度名称_广告计划"].astype(str).isin(raw_selected_plan)]
        if dim_choice in ("广告组", "广告创意") and "维度名称_广告组" in df_raw_display.columns:
            # 原始表同样按上层筛选后的数据生成下层组选项
            raw_options_group = sorted(df_raw_display["维度名称_广告组"].dropna().astype(str).unique().tolist())
            raw_selected_group = st.multiselect("筛选 广告组（原始表）", options=raw_options_group, default=[], key="filter_raw_group")
            if raw_selected_group:
                df_raw_display = df_raw_display[df_raw_display["维度名称_广告组"].astype(str).isin(raw_selected_group)]
        if "Dimension Value" in df_raw_display.columns:
            raw_options_dim = sorted(df_raw_display["Dimension Value"].dropna().astype(str).unique().tolist())
            raw_selected_dim = st.multiselect("筛选 维度名称（原始表）", options=raw_options_dim, default=[], key="filter_raw_dim")
        if raw_selected_dim and "Dimension Value" in df_raw_display.columns:
            df_raw_display = df_raw_display[df_raw_display["Dimension Value"].astype(str).isin(raw_selected_dim)]
        df_raw_display = sort_for_display(df_raw_display)
        st.dataframe(df_raw_display, use_container_width=True)
        csv = df_raw_display.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 下载原始报表 (.csv)",
            data=csv,
            file_name=f"cohort_{dim_choice}_{start_s}_to_{end_s}.csv",
            mime="text/csv",
            use_container_width=True
        )

else:
    st.info("👈 请在左侧侧边栏配置 Token 和 Cohort 周期，然后点击执行分析。")
    with st.expander("📚 指标定义说明"):
        st.write("""
        - **总转化成本 (CPA)**: `总消耗 / Plot UV` (获取每个激活用户的成本)
        - **IAP 转化成本 (CPP)**: `总消耗 / IAP UV` (获取每个付费用户的成本)
        - **ROI**: `(内购净营收 + 广告营收) / 总消耗`
        - **高价值率**: ECPM > 300 的用户占比
        """)
