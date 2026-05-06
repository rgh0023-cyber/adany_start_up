import pandas as pd
import io
import re
import csv

def clean_sql_response(raw_text):
    if not raw_text or len(raw_text.strip()) == 0:
        return pd.DataFrame()

    # --- 强力解码逻辑开始 ---
    # 如果输入是 bytes，先尝试 utf-8，不行再尝试 gbk
    if isinstance(raw_text, bytes):
        try:
            content = raw_text.decode('utf-8')
        except UnicodeDecodeError:
            content = raw_text.decode('gbk', errors='ignore')
    else:
        # 已经是字符串时直接使用，避免二次转码造成中文乱码
        content = raw_text
    # --- 强力解码逻辑结束 ---

    try:
        # 定义物理对齐表头
        expected_cols = [
            'Date', 'Dimension Value', 'Media Source', 'OS',
            '维度名称_全部', '维度名称_广告计划', '维度名称_广告组', '维度名称_广告创意',
            'Cost', 'Plot UV', 
            'ECPM_Null', 'ECPM_0_100', 'ECPM_100_200', 'ECPM_200_300', 
            'ECPM_300_400', 'ECPM_400_500', 'ECPM_500+',
            'L10 UV', 'L20 UV', 'L30 UV', 'L40 UV', 'L50 UV', 'L60 UV', 'L70 UV', 'L80 UV', 'L90 UV', 'L100 UV',
            'IAP UV', 'IAP_UV_D0', 'IAP Times', 'IAP Revenue', 'Ad UV', 'Ad Revenue', 'total_amount', 'group_num_0', 'group_num'
        ]

        def _parse_csv(csv_text):
            return pd.read_csv(
                io.StringIO((csv_text or "").strip()),
                header=None,
                quotechar='"',
                skipinitialspace=True,
                engine='python',
                on_bad_lines='skip'
            )

        # 第一轮：直接解析
        df = _parse_csv(content)

        # 若返回中混入非 CSV 行，做一次兜底清洗后重试
        if df.empty and content:
            lines = [ln for ln in content.splitlines() if ln.count(",") >= 10]
            df = _parse_csv("\n".join(lines))

        # 仍为空时，按「精确列数」逐行提取有效 CSV 记录，避免坏行拖垮整批
        if df.empty and content:
            valid_rows = []
            for ln in content.splitlines():
                line = (ln or "").strip()
                if not line:
                    continue
                try:
                    parsed = next(csv.reader([line]))
                except Exception:
                    continue
                if len(parsed) == len(expected_cols):
                    valid_rows.append(parsed)
            if valid_rows:
                df = pd.DataFrame(valid_rows, columns=expected_cols)
                # 已在此处完成列名对齐，后续对齐逻辑无需再处理

        # 过滤第一行是表头字符的情况
        if df.shape[0] > 0 and ("Date" in str(df.iloc[0, 0]) or "Dimension" in str(df.iloc[0, 0])):
            df = df.iloc[1:].reset_index(drop=True)

        # 强制对齐列名（容错：若 SQL 返回列比预期多，不直接失败，先扩展占位列再截断）
        if list(df.columns) != expected_cols:
            if df.shape[1] <= len(expected_cols):
                df.columns = expected_cols[:df.shape[1]]
            else:
                extra_cols = [f"extra_col_{i}" for i in range(df.shape[1] - len(expected_cols))]
                df.columns = expected_cols + extra_cols
                df = df.iloc[:, :len(expected_cols)]

        # 清洗特殊字符
        def clean_val(x):
            if isinstance(x, str):
                return re.sub(r'["\'\r\n\t]', '', x).strip()
            return x

        df = df.applymap(clean_val)

        # 核心指标转数值
        numeric_cols = ['Cost', 'IAP Revenue', 'Ad Revenue', 'Plot UV', 'IAP_UV_D0']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

        return df
    except Exception as e:
        print(f"解析错误: {e}")
        return pd.DataFrame()
