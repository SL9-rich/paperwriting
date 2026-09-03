"""User-configured Chat Completions JSON adapter; no bundled endpoint or key."""

import json
import os
import re
import time
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ModelError(RuntimeError):
    pass


class Provider:
    def __init__(self, key=None, model=None, transport=None, base_url=None):
        self.key = key if key is not None else os.getenv("LLM_API_KEY", "")
        self.model = model if model is not None else os.getenv("LLM_MODEL", "")
        self.base_url = (
            base_url if base_url is not None else os.getenv("LLM_BASE_URL", "")
        ).rstrip("/")
        self.transport = transport
        system_ca = Path("/etc/ssl/cert.pem")
        self.ssl_context = ssl.create_default_context(
            cafile=str(system_ca) if sys.platform == "darwin" and system_ca.exists() else None
        )

    @property
    def configured(self):
        return bool(self.base_url and self.model)

    def json(self, system, user, max_tokens=4000):
        if not self.configured:
            raise ModelError("尚未配置 LLM_BASE_URL 和 LLM_MODEL，请编辑项目 .env 后重启。")
        target = urlsplit(self.base_url)
        if target.username or target.password or target.query or target.fragment:
            raise ModelError("API 地址不能包含用户名、密码、查询参数或片段；密钥请单独配置。")
        if target.scheme not in {"http", "https"} or not target.hostname:
            raise ModelError("API 地址必须是有效的 HTTP(S) 基础地址。")
        if target.scheme != "https" and target.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ModelError("非本机 API 必须使用 HTTPS，以保护论文和密钥。")
        headers = {"Authorization": "Bearer " + self.key} if self.key else {}
        for attempt in range(3):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(120, connect=15),
                    transport=self.transport,
                    verify=self.ssl_context,
                ) as client:
                    response = client.post(
                        self.base_url + "/chat/completions",
                        headers=headers,
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            "response_format": {"type": "json_object"},
                            "max_tokens": max_tokens,
                        },
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(2**attempt)
                        continue
                if not response.is_success:
                    raise ModelError(
                        f"模型服务返回 HTTP {response.status_code}，请检查配置或稍后重试。"
                    )
                data = response.json()
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ModelError("模型输出超过长度限制，请缩小章节范围。")
                content = re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", choice["message"]["content"].strip()
                )
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("Expected object")
                return parsed, int(data.get("usage", {}).get("total_tokens", 0))
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt == 2:
                    raise ModelError("模型连接失败或超时，请检查网络后重试。") from error
                time.sleep(2**attempt)
            except (KeyError, IndexError, ValueError) as error:
                raise ModelError("模型没有返回完整有效的 JSON，请重试。") from error
        raise ModelError("模型服务暂不可用。")
