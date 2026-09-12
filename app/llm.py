"""大模型调用：配置可来自网页（推荐）或环境变量，密钥单独落盘不入库。

配置优先级：网页设置（settings 表） > 环境变量 > 内置默认（本机）

安全约定
- **API Key 只存在于独立文件**（默认 <数据目录>/llm_api_key，权限 600，属主为服务账号），
  不写入数据库、不写日志、不回显到页面；页面只显示后 4 位用于辨认。
- 端点默认必须是内网地址。要连公网 API 必须在网页上显式勾选「我了解风险」，
  否则拒绝保存——避免健康数据在不知情的情况下发往第三方。
- 只用标准库 urllib，保持零额外依赖与低内存占用。
- 具名端点（ns）：ns="foo" 用 foo_ 前缀的键名与独立密钥文件，**不会**悄悄回落到默认端点。
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8080/v1"   # 中性默认；真实端点由 deploy/config.sh 或网页配置提供
DEFAULT_MODEL = "local-model"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# 网页可写的配置项 → settings 表键名（健康问答，无前缀）
SETTING_KEYS = {
    "base_url": "llm_base_url",
    "model": "llm_model",
    "allow_public": "llm_allow_public",
    "kind": "llm_kind",                 # openai（OpenAI 兼容，默认）
    "updated_at": "llm_updated_at",
    "last_check": "llm_last_check",     # 最近一次连通性测试结果
    "model_list": "llm_model_list",     # 上次拉取到的可用模型列表（JSON 数组）
    "model_list_at": "llm_model_list_at",
}


class LLMError(RuntimeError):
    pass


def setting_key(field: str, ns: str = "") -> str:
    """settings 表键名。ns='foo' → foo_llm_base_url，与默认端点互不覆盖。"""
    base = SETTING_KEYS[field]
    return f"{ns}_{base}" if ns else base


def key_file(ns: str = "") -> Path:
    """密钥文件路径（与数据库同目录，避免与代码仓库混在一起）。"""
    import db as dbm
    fname = f"{ns}_llm_api_key" if ns else "llm_api_key"
    envk = f"BG_{ns.upper()}_LLM_KEY_FILE" if ns else "BG_LLM_KEY_FILE"
    default = Path(dbm.DB_PATH).parent / fname
    return Path(os.environ.get(envk, default))


# ------------------------------------------------------------------ 密钥读写

def normalize_key(raw: str) -> str:
    """容忍整行粘贴：剥掉 `NAME=`、`export `、引号和 `Bearer ` 前缀。

    很多人会把 `.env` 里那行 `HYPERBROWSER_API_KEY=hb_xxx` 整行粘进来，
    结果 401 却看不出原因（实测踩过）。这里只做机械剥离，不校验长度。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.match(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+)$", s)
    if m:
        s = m.group(1).strip()
    s = s.strip().strip('"').strip("'").strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


def save_key(secret: str, ns: str = "") -> None:
    """原子写入密钥，权限 600。空字符串表示清除。"""
    secret = normalize_key(secret)
    p = key_file(ns)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not secret:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.strip().encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def load_key(ns: str = "") -> str:
    p = key_file(ns)
    envk = f"BG_{ns.upper()}_LLM_API_KEY" if ns else "BG_LLM_API_KEY"
    try:
        if p.is_file() and (p.stat().st_mode & 0o077) == 0:
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return os.environ.get(envk, "").strip()


def key_last4(ns: str = "") -> str:
    k = load_key(ns)
    return ("…" + k[-4:]) if len(k) >= 8 else ("已设置" if k else "")


def clear_key(ns: str = "") -> None:
    save_key("", ns)


# ------------------------------------------------------------------ 配置读取

_DB_OK = True          # 最近一次读配置是否成功（读不到时不能猜端点，见 check-agent-allowed）


def db_readable() -> bool:
    """配置数据库是否可读。读不到时一律按「无法确认」处理，不做静默降级。"""
    return _DB_OK


def _from_db(key: str) -> str:
    global _DB_OK
    try:
        import db as dbm
        with dbm.db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        _DB_OK = True
        return row["value"] if row else ""
    except Exception:   # 数据库不可用：只退回环境变量，并标记为「配置不可确认」
        _DB_OK = False
        return ""


def _env(ns: str, name: str, default: str = "") -> str:
    if ns:
        return os.environ.get(f"BG_{ns.upper()}_{name}", default)
    return os.environ.get(f"BG_{name}", default)


def config(ns: str = "") -> dict:
    """返回当前生效的模型配置（不含密钥本体）。

    ns='' 默认端点（缺省回落到内置本机端点）。
    ns='foo' 具名端点：未配置时 base_url/model 为空，**不**借用默认端点。
    """
    db_base = (_from_db(setting_key("base_url", ns)) or _env(ns, "LLM_BASE_URL", "")).rstrip("/")
    db_model = _from_db(setting_key("model", ns)) or _env(ns, "LLM_MODEL", "")
    allow_public = (_from_db(setting_key("allow_public", ns)) == "1"
                    or _env(ns, "LLM_ALLOW_PUBLIC", "0") == "1")
    if ns:
        base = db_base
        model = db_model
        source = ("web" if _from_db(setting_key("base_url", ns))
                  else ("env" if _env(ns, "LLM_BASE_URL") else "unset"))
    else:
        base = db_base or DEFAULT_BASE
        model = db_model or DEFAULT_MODEL
        source = ("web" if _from_db(setting_key("base_url", ns))
                  else ("env" if os.environ.get("BG_LLM_BASE_URL") else "default"))
    return {
        "base_url": base,
        "model": model,
        "allow_public": allow_public,
        "db_ok": db_readable(),
        "source": source,
        "configured": bool(base and model),
        "ns": ns or "qa",
        "key_set": bool(load_key(ns)),
        "key_hint": key_last4(ns),
        "updated_at": _from_db(setting_key("updated_at", ns)),
        "last_check": _from_db(setting_key("last_check", ns)),
    }


def base_url() -> str:
    return config()["base_url"]


def model_name() -> str:
    return config()["model"]


def enabled(ns: str = "") -> bool:
    flag = _env(ns, "LLM_DISABLED", "0")
    return flag not in ("1", "true", "yes")


def is_private_host(host: str) -> bool:
    if host in LOCAL_HOSTS:
        return True
    return bool(re.match(
        r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)", host))


def check_endpoint(url: str, allow_public: bool = False) -> tuple[bool, str]:
    """保存/调用前校验端点。返回 (是否允许, 说明)。"""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return False, "URL 无法解析"
    if u.scheme not in ("http", "https"):
        return False, "只支持 http/https"
    host = u.hostname or ""
    if not host:
        return False, "缺少主机名"
    if not is_private_host(host) and not allow_public:
        return False, (f"{host} 是公网地址。请求内容会发往该服务，"
                       "如确实要用，请在下方勾选「我了解数据外发风险」后再保存。")
    return True, ""


def _assert_private(url: str, ns: str = "") -> None:
    ok, why = check_endpoint(url, allow_public=config(ns)["allow_public"])
    if not ok:
        raise LLMError(why)


# ------------------------------------------------------------------ 调用

def _payload(messages: list[dict], stream: bool, temperature: float, max_tokens: int,
             thinking: bool = False, cfg: dict | None = None) -> bytes:
    cfg = cfg or config()
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
        "chat_template_kwargs": {"enable_thinking": bool(thinking)},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _request(messages, stream, temperature, max_tokens, timeout, ns: str = ""):
    cfg = config(ns)
    if not cfg.get("base_url") or not cfg.get("model"):
        raise LLMError("还没配置模型端点。" if not ns else f"还没配置模型端点（{ns}）。")
    url = f"{cfg['base_url']}/chat/completions"
    _assert_private(url, ns)
    req = urllib.request.Request(url, data=_payload(messages, stream, temperature, max_tokens, cfg=cfg),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    key = load_key(ns)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    return urllib.request.urlopen(req, timeout=timeout)


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1600,
         timeout: int = 180, ns: str = "") -> str:
    """一次性返回完整回答。"""
    if not enabled(ns):
        raise LLMError("问答功能已在服务器配置中关闭。")
    try:
        with _request(messages, False, temperature, max_tokens, timeout, ns=ns) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"].get("content") or "").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise LLMError(f"模型服务返回 {e.code}：{detail}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise LLMError(f"无法连接模型服务（{config(ns)['base_url']}）：{e}") from e
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError(f"模型返回格式异常：{e}") from e


def stream_chat_events(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1600,
                       timeout: int = 300, ns: str = ""):
    """流式返回增量事件：{"t": 正文} 或 {"r": 思考过程}。

    思考过程单独成通道：模型（DeepSeek/GLM/Grok 等）在正文前会先吐 `reasoning_content`，
    界面把它显示成一行滚动过程（可点开看全文），正文开始时收起。
    没有思考通道的模型只会看到 {"t": ...}，行为与以前一致。
    """
    if not enabled(ns):
        raise LLMError("问答功能已在服务器配置中关闭。")
    try:
        resp = _request(messages, True, temperature, max_tokens, timeout, ns=ns)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise LLMError(f"模型服务返回 {e.code}：{detail}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise LLMError(f"无法连接模型服务（{config(ns)['base_url']}）：{e}") from e
    try:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
                delta = obj["choices"][0].get("delta") or {}
            except (KeyError, IndexError, json.JSONDecodeError):
                continue
            think = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if think:
                yield {"r": think}
            text = delta.get("content") or ""
            if text:
                yield {"t": text}
    finally:
        resp.close()


def stream_chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1600,
                timeout: int = 300, ns: str = ""):
    """流式返回文本增量（生成器）。思考通道被丢弃，保持老调用方行为不变。"""
    for ev in stream_chat_events(messages, temperature, max_tokens, timeout, ns=ns):
        if ev.get("t"):
            yield ev["t"]


def cached_models(ns: str = "") -> tuple[list[str], str]:
    """上次从端点拉取到的模型列表（离线缓存，避免每次打开页面都发网络请求）。"""
    raw = _from_db(setting_key("model_list", ns))
    try:
        models = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        models = []
    return ([str(m) for m in models if m][:200], _from_db(setting_key("model_list_at", ns)))


def fetch_models(timeout: int = 8, ns: str = "") -> tuple[bool, list[str], str]:
    """向端点拉取模型列表（OpenAI 兼容的 GET /models）。返回 (成功, 列表, 错误)。"""
    cfg = config(ns)
    if not cfg.get("base_url"):
        return False, [], "还没填接口地址"
    url = f"{cfg['base_url']}/models"
    ok, why = check_endpoint(url, allow_public=cfg["allow_public"])
    if not ok:
        return False, [], why
    try:
        req = urllib.request.Request(url)
        key = load_key(ns)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [str(m.get("id")) for m in data.get("data", []) if m.get("id")]
        return True, ids[:200], ""
    except urllib.error.HTTPError as e:
        return False, [], f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, [], str(e)[:200]


def health(timeout: int = 8, ns: str = "") -> dict:
    """检查模型端点可用性（管理页显示用；不会回显密钥）。"""
    cfg = config(ns)
    host = urllib.parse.urlparse(cfg["base_url"]).hostname or ""
    info = {"url": cfg["base_url"], "model": cfg["model"], "enabled": enabled(ns),
            "ok": False, "error": "", "key_set": cfg["key_set"], "key_hint": cfg["key_hint"],
            "public": bool(host) and not is_private_host(host),
            "source": cfg["source"]}
    if not enabled(ns):
        info["error"] = "已在配置中关闭"
        return info
    if not cfg.get("base_url"):
        info["error"] = "还没配置端点"
        return info
    ok, models, err = fetch_models(timeout=timeout, ns=ns)
    info["ok"] = ok
    info["models"] = models[:5]
    info["all_models"] = models          # 完整列表，供管理页缓存进下拉候选
    info["model_count"] = len(models)
    info["error"] = err
    return info
