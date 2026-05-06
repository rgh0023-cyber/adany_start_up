import requests
import urllib3

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class TAClient:
    def __init__(self, base_url, token):
        self.api_url = f"{base_url.rstrip('/')}/querySql"
        self.token = token

    def execute_query(self, sql):
        """使用 Body 传参避免 414 错误，并设置长超时"""
        payload = {
            "token": self.token,
            "format": "csv",
            "sql": sql
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        try:
            response = requests.post(
                self.api_url, 
                data=payload, 
                headers=headers,
                timeout=180,  # 归因查询耗时较长，设置为 3 分钟
                verify=False
            )
            
            if response.status_code == 200:
                # 优先按 UTF-8 读取，避免 requests 自动猜编码导致中文乱码
                response.encoding = "utf-8"
                text = response.text
                # 检查是否为 JSON 报错信息
                if text.strip().startswith('{"code"'):
                    return None, response.json()
                return text, None
            else:
                return None, f"HTTP {response.status_code}: {response.reason}"
        except Exception as e:
            return None, str(e)
