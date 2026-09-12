#!/usr/bin/env python3
"""把网页里配置的模型信息导出成 pi 的 models.json（供智能体使用）。

    python llm_config.py models-json     # 输出 models.json

密钥从 <数据目录>/llm_api_key 读取（0600），不经过命令行参数，避免出现在进程列表里。
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urlparse

import llm


def models_json() -> str:
    cfg = llm.config()
    key = llm.load_key()
    host = urlparse(cfg["base_url"]).hostname or ""
    private = llm.is_private_host(host)
    model_entry = {
        "id": cfg["model"],
        "name": f"{cfg['model']}（{host}）",
        "reasoning": False,
        "input": ["text"],
        "contextWindow": 131072 if not private else 262144,
        "maxTokens": 16384,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    provider = {
        "name": "健康档案模型",
        "baseUrl": cfg["base_url"],
        "api": "openai-completions",
        "apiKey": key or "none",
        "models": [model_entry],
    }
    if private:
        # 内网 sglang 部署的 Qwen 需要这几个兼容项（公网官方 API 一般不需要，且可能不认这些字段）
        provider["compat"] = {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": False,
            "maxTokensField": "max_tokens",
            "thinkingFormat": "qwen-chat-template",
        }
    return json.dumps({"providers": {"bg": provider}}, ensure_ascii=False, indent=2)


def check_agent_allowed() -> int:
    """检查智能体将要使用的端点，返回需要打印的警告（不拦截启动）。

    用户明确要求：公网端点只给警告，不禁止启动——是否使用由用户决定。
    这里只做「把事实说清楚」，把判断权留给用户。
    """
    cfg = llm.config()
    host = urlparse(cfg["base_url"]).hostname or ""
    private = llm.is_private_host(host)
    warn = ""
    if not cfg.get("db_ok", True):
        warn = ("读不到配置数据库，无法确认端点是否为内网地址（例如 BG_DB 未设置或权限不对），"
                "当前回退到环境变量或内置默认值。请确认这是你想要的端点。")
    elif not private:
        warn = (f"端点 {host} 是公网地址：智能体会把自身上下文（工作准则、工具说明、"
                "对话内容）一并发往该服务。请注意其中不要出现你不想外发的内容。")
    print(json.dumps({"allowed": True, "base_url": cfg["base_url"], "public": not private,
                      "warning": warn}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "models-json":
        print(models_json())
    elif mode == "current-model":
        # 供 deploy/pi/run.sh 解析「网页里配置的模型名」，避免把模型写死在脚本里
        print(llm.config()["model"])
    elif mode in ("check-agent-allowed", "check-agent-endpoint"):
        sys.exit(check_agent_allowed())
    else:
        print("用法：python llm_config.py models-json | current-model | check-agent-endpoint",
              file=sys.stderr)
        sys.exit(2)
