"""联网检索与正文抓取：走 Hyperbrowser 云浏览器，本机不跑浏览器、不占本机内存。

为什么不用官方 SDK（实测数据，本机 Python 3.11 / Node 24）：
    - Python SDK：一 import 就 +35.9MB RSS（httpx 0.28 + pydantic 2.13），磁盘约 7MB；
    - Node SDK：要先有 Node 运行时（本机 nvm 装的那套约 90MB 磁盘），空跑进程 RSS 40.8MB，
      而且每次调用是**另一个进程**，与主应用不共享内存；
    - 本项目只用两个 REST 接口（search / fetch），标准库 urllib 就够：额外内存 0。
    服务端只有 961MB 内存（应用自己占 81MB），所以走 REST，不装 SDK。

接口（与官方 SDK client/managers/*/web 完全一致，路径对齐 _build_url）：
    POST {base}/api/web/search  {"query": "..."}
        → {"jobId":..,"status":..,"data":{"query":..,"results":[{title,url,description}]}}
    POST {base}/api/web/fetch   {"url": "...", "outputs": {"formats": ["markdown"]}}
        → {"jobId":..,"status":..,"data":{"markdown":..,"metadata":..,"links":[..]}}
    认证：请求头 `x-api-key`（与 SDK 的 httpx.Client(headers={"x-api-key": key}) 一致）。

隐私：查询词**永远先过一遍外发策略**（只发通用医学词；姓名/日期/站点专属词一律不发），
      抓回来的公开网页内容只作为「依据」喂给模型，回复里必须带来源 URL。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://api.hyperbrowser.ai"
SETTING_ENABLED = "hb_enabled"          # "1" / "0"
SETTING_BASE = "hb_base_url"
SETTING_LAST = "hb_last_check"          # 最近一次测试结果（页面展示用）
ENV_KEY = "BG_HB_API_KEY"
MAX_MD = 6000                           # 单页正文最多取多少字符
CACHE_TTL = 600                         # 同一查询 10 分钟内不重复花钱


class WebSearchError(RuntimeError):
    pass


# ------------------------------------------------------------------ 密钥（独立文件，600）

def key_file() -> Path:
    import db as dbm
    default = Path(dbm.DB_PATH).parent / "hyperbrowser_key"
    return Path(os.environ.get("BG_HB_KEY_FILE", default))


def save_key(secret: str) -> None:
    """原子写入密钥，权限 600；空字符串表示清除。

    整行粘贴（`HYPERBROWSER_API_KEY=xxx`）会被自动剥掉变量名；剥完还是空就报错，
    不要静默把密钥清掉。
    """
    from llm import normalize_key
    raw = secret or ""
    secret = normalize_key(raw)
    if raw.strip() and not secret:
        raise ValueError("这看起来是变量名而不是密钥，请只粘贴密钥本身")
    p = key_file()
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


def load_key() -> str:
    p = key_file()
    try:
        if p.is_file() and (p.stat().st_mode & 0o077) == 0:
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return os.environ.get(ENV_KEY, "").strip()


def key_last4() -> str:
    k = load_key()
    return ("…" + k[-4:]) if len(k) >= 8 else ("已设置" if k else "")


def clear_key() -> None:
    save_key("")


# ------------------------------------------------------------------ 配置

def _get_setting(key: str, default: str = "") -> str:
    try:
        import db as dbm
        with dbm.db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:   # noqa: BLE001 - 读不到就当默认
        return default


def _set_setting(key: str, value: str) -> None:
    import db as dbm
    with dbm.db() as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def set_config(enabled_flag: bool, base: str = "") -> None:
    """保存开关与接口地址（密钥走 save_key）。"""
    _set_setting(SETTING_ENABLED, "1" if enabled_flag else "0")
    if (base or "").strip():
        _set_setting(SETTING_BASE, base.strip().rstrip("/"))


def base_url() -> str:
    return (_get_setting(SETTING_BASE) or os.environ.get("BG_HB_BASE_URL", "")
            or DEFAULT_BASE).rstrip("/")


def enabled() -> bool:
    """开关打开且配了密钥才算可用。"""
    return _get_setting(SETTING_ENABLED, "0") == "1" and bool(load_key())


def config() -> dict:
    return {"enabled": _get_setting(SETTING_ENABLED, "0") == "1",
            "key_set": bool(load_key()), "key_hint": key_last4(),
            "base_url": base_url(), "last_check": _get_setting(SETTING_LAST, ""),
            "key_path": str(key_file()), "ready": enabled()}


# ------------------------------------------------------------------ HTTP

def _post(path: str, payload: dict, timeout: int = 45) -> dict:
    key = load_key()
    if not key:
        raise WebSearchError("没有配置 Hyperbrowser API Key（在「系统管理 → 联网检索」里填）")
    req = urllib.request.Request(base_url() + path,
                                data=json.dumps(payload).encode("utf-8"),
                                headers={"x-api-key": key, "Content-Type": "application/json",
                                         "Accept": "application/json",
                                         "User-Agent": "health-records/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", "ignore") or "{}")
            detail = str(body.get("message") or body.get("error") or "")[:160]
        except Exception:       # noqa: BLE001
            detail = ""
        if e.code in (401, 403):
            raise WebSearchError(f"密钥无效或没有权限（HTTP {e.code}）{detail}") from e
        if e.code == 402:
            raise WebSearchError("Hyperbrowser 额度用完了（HTTP 402）") from e
        raise WebSearchError(f"Hyperbrowser 返回 HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise WebSearchError(f"连不上 Hyperbrowser：{str(e)[:160]}") from e


_CACHE: dict[str, tuple[float, list]] = {}


def search(query: str, limit: int = 6, timeout: int = 45) -> list[dict]:
    """搜网页，返回 [{title,url,description}]。同一查询 10 分钟内走缓存（省额度）。"""
    q = (query or "").strip()[:200]
    if not q:
        return []
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1][:limit]
    data = _post("/api/web/search", {"query": q}, timeout=timeout)
    if data.get("status") == "failed" or data.get("error"):
        raise WebSearchError(f"检索失败：{str(data.get('error'))[:160]}")
    results = ((data.get("data") or {}).get("results")) or []
    out = [{"title": str(r.get("title") or "")[:200], "url": str(r.get("url") or ""),
            "description": str(r.get("description") or "")[:400]}
           for r in results if r.get("url") and not blocked_host(str(r.get("url") or ""))]
    _CACHE[q] = (time.time(), out)
    return out[:limit]


def _clean_markdown(body: str) -> str:
    """抓回的正文清理：图片全去掉；链接只留文字（来源会单独列）；压缩空行。"""
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", body or "")
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1), body)
    body = "\n".join(ln.rstrip() for ln in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def fetch_markdown(url: str, max_chars: int = MAX_MD, timeout: int = 60) -> tuple[str, str]:
    """抓一个页面并转成 Markdown。返回 (markdown, 说明)。"""
    if not (url or "").startswith(("http://", "https://")):
        raise WebSearchError("只支持 http/https 链接")
    data = _post("/api/web/fetch", {"url": url, "outputs": {"formats": ["markdown"]}},
                 timeout=timeout)
    if data.get("status") == "failed" or data.get("error"):
        raise WebSearchError(f"抓取失败：{str(data.get('error'))[:160]}")
    body = _clean_markdown((data.get("data") or {}).get("markdown") or "")
    if not body:
        return "", "页面没有可读正文（可能是纯 JS 渲染或需要登录）"
    note = f"已抓取 {len(body)} 字"
    return body[:max_chars], note


def health(timeout: int = 40) -> dict:
    """测试用：做一次极小的检索，确认密钥可用。"""
    info = {"ok": False, "error": "", "sample": [], "config": config()}
    if not config()["key_set"]:
        info["error"] = "还没填 API Key"
        return info
    try:
        hits = search("period pain heat therapy", limit=2, timeout=timeout)
        info["ok"] = True
        info["sample"] = [h["title"] for h in hits]
    except WebSearchError as e:
        info["error"] = str(e)[:200]
    try:
        _set_setting(SETTING_LAST, ("成功 " if info["ok"] else "失败 ") + time.strftime("%Y-%m-%d %H:%M"))
    except Exception:   # noqa: BLE001
        pass
    return info


# ------------------------------------------------------------------ 来源可信度

TRUSTED_HOSTS = ("who.int", "nhs.uk", "acog.org", "mayoclinic.org", "msdmanuals.com", "cdc.gov",
                 "nih.gov", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "cochrane.org",
                 "bmj.com", "thelancet.com", "womenshealth.gov", "hopkinsmedicine.org",
                 "nhc.gov.cn", "cma.org.cn", "yiigle.com", "medlive.cn",
                 "wikipedia.org", "mayoclinic.org")


# 用户反馈「百度系结果不太可信」→ 直接从结果里过滤掉（Hyperbrowser 的检索是它自己的索引，
# 换不了搜索引擎，所以这里做来源筛选：可信站点优先、聚合站降权、百度系丢弃）
BLOCKED_HOSTS = ("baidu.com", "baiducontent.com", "bdstatic.com", "baijiahao.baidu.com",
                 "so.com", "360.cn", "360.com", "sogou.com", "toutiao.com", "sm.cn",
                 "chinaso.com", "quark.sm.cn")
DERANK_HOSTS = ("zhihu.com", "csdn.net", "jianshu.com", "xiaohongshu.com", "douban.com",
                "bilibili.com", "weixin.qq.com", "sohu.com", "163.com", "sina.com.cn",
                "baijiahao.baidu.com", "ixigua.com", "jiemian.com")


def _host(url: str) -> str:
    import urllib.parse as _up
    return (_up.urlparse(url or "").hostname or "").lower()


def blocked_host(url: str) -> bool:
    """百度系等不可信来源：直接从结果里扔掉。"""
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in BLOCKED_HOSTS)


def host_rank(url: str) -> int:
    """来源排序权重：2=权威医学站点，1=普通，0=聚合/自媒体（只在没别的结果时才用）。"""
    if trusted_host(url):
        return 2
    h = _host(url)
    if any(h == d or h.endswith("." + d) for d in DERANK_HOSTS):
        return 0
    return 1


def trusted_host(url: str) -> bool:
    """公开医学来源白名单（决定抓取顺序：权威站点优先）。"""
    import urllib.parse as _up
    host = (_up.urlparse(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in TRUSTED_HOSTS)


# ------------------------------------------------------------------ 查询词清洗

_DATE_RE = re.compile(r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?")
_NUM_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:天|日|周|个月|月|岁|次|年)?")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL_RE = re.compile(r"https?://\S+")


def strip_private(text: str) -> str:
    """把查询词里的个人化信息摘掉（日期、邮箱、链接），只留通用医学表述。

    注意：这是**降低耦合**，不是万能；真正的放行判断仍然交给 outbound/privacy_check。
    """
    s = _URL_RE.sub(" ", text or "")
    s = _EMAIL_RE.sub(" ", s)
    s = _DATE_RE.sub(" ", s)
    s = re.sub(r"[（(][^）)]{0,20}(已|记|日|号)[^）)]{0,20}[）)]", " ", s)   # 「（已隐去）」这类
    s = re.sub(r"[，。；！？、,.;!?]", " ", s)
    s = _NUM_RE.sub(" ", s)
    return re.sub(r"\s{2,}", " ", s).strip()[:160]


def pick_query(question: str, conn=None) -> tuple[str, str]:
    """把问题变成可以外发的检索词。返回 (查询词, 拒绝原因)；被拒时查询词为空。

    两步：先摘掉日期/邮箱/链接等个人化信息，再过一遍外发策略（命中即拒绝，不猜）。
    """
    q = strip_private(question)
    if len(q) < 4:
        return "", "问题里没有可检索的通用词"
    if conn is not None:
        import agent as agentm
        ok, cleaned, why = agentm.privacy_check(conn, q)
        if not ok:
            return "", why
        return cleaned, ""
    return q, ""
