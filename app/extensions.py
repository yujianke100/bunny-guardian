"""扩展加载器：把「表」之外的功能以包的形式挂在主应用上。

这个仓库只放**表**——健康档案本身。情侣空间与 AI 跑团属于**里**，
代码在另一个仓库（`ex-bunny-guardian`）里，作为扩展叠在表上运行。
好处是两边的代码不重复：表怎么改，里都不用跟着抄一遍。

两种挂载方式（都要重启服务）：

1. 把扩展目录放进 `<仓库根>/extensions/`（可以软链到别处的检出目录）；
2. 用环境变量指向扩展所在目录**列表**（多个目录用 `:` 分隔，Windows 用 `;`）：

```sh
BG_EXTENSIONS=/root/ex-bunny-guardian/extensions
```

每个扩展是一个含 `__init__.py` 的包，必须提供 `register(ctx)`。

## 扩展能做什么

在 `register(ctx)` 里通过 `ctx` 注册：

| 方法 | 作用 |
|---|---|
| `ctx.add_templates(dir)` | 追加模板搜索目录（自己的 HTML 可以 `extends "base.html"`） |
| `ctx.add_static(url, dir)` | 挂一个静态目录（自己的 css/js） |
| `ctx.add_schema(name, sql)` | 建表 SQL，随 `db.init_db()` 一起执行（写 `IF NOT EXISTS` 保持幂等） |
| `ctx.context(fn)` | `fn(request, user) -> dict`，逐页合并进模板上下文 |
| `ctx.app` | FastAPI 实例：直接 `ctx.app.get("/xxx")` 挂路由 |

另外 `ctx` 透出表自己的几个小工具，用法与表内路由完全一致：
`ctx.render(request, user, 模板名, **上下文)`、`ctx.csrf_ok(...)`、`ctx.client_ip(...)`、
`ctx.guard(user, 空间)`、`ctx.audit(user, 动作, 详情, request)`、`ctx.ping`（SSE 心跳字符串）。

## 约定（有意为之）

- **扩展不要 `import main`**（会循环导入，因为 main 在加载扩展时还没执行完）；
  需要什么就从 `ctx` 取，或者 import `db` / `llm` / `auth` / `kb` 这些纯模块。
- **加载失败不能拖垮表**：任何扩展报错只打印一行并跳过，应用照常启动。
  表必须能单跑——这是「两个可以分开搭建」的前提。
- 扩展的模板搜索路径排在表之后：同名模板以表的为准（防止扩展悄悄顶掉表层）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from pathlib import Path

ENV = "BG_EXTENSIONS"
ROOT = Path(__file__).resolve().parent.parent          # 仓库根

_SCHEMAS: list[tuple[str, str]] = []
_TEMPLATE_DIRS: list[Path] = []
_CONTEXT_FNS: list = []
_LOADED: list[str] = []


class Ctx:
    """交给扩展的接口。一次 load 共用一个实例，`name` 指向当前加载的扩展。"""

    def __init__(self, app, render, helpers: dict) -> None:
        self.app = app
        self.name = ""
        self._render = render
        self._helpers = helpers

    # ---------------- 注册 ----------------

    def add_templates(self, directory) -> None:
        d = Path(directory)
        if d.is_dir():
            _TEMPLATE_DIRS.append(d)

    def add_static(self, url: str, directory) -> None:
        from fastapi.staticfiles import StaticFiles
        d = Path(directory)
        if not d.is_dir():
            return
        name = "ext" + url.replace("/", "_")
        if any(getattr(r, "name", "") == name for r in self.app.routes):
            return
        self.app.mount(url, StaticFiles(directory=str(d)), name=name)

    def add_schema(self, name: str, sql: str) -> None:
        _SCHEMAS.append((name, sql))

    def context(self, fn) -> None:
        """fn(request, user) -> dict，逐页合并进模板上下文。"""
        _CONTEXT_FNS.append(fn)

    # ---------------- 表的小工具（透传） ----------------

    def render(self, request, user, name: str, **kw):
        return self._render(request, user, name, **kw)

    def __getattr__(self, item):
        try:
            return self._helpers[item]
        except KeyError:
            raise AttributeError(item) from None


def _dirs() -> list[Path]:
    """要扫描的扩展目录：环境变量里的（按顺序）+ 仓库根的 extensions/。"""
    out: list[Path] = []
    raw = os.environ.get(ENV, "") or ""
    for part in raw.replace(";", os.pathsep).split(os.pathsep):
        part = part.strip()
        if part:
            out.append(Path(part).expanduser())
    out.append(ROOT / "extensions")
    return [d for d in out if d.is_dir()]


def _import_package(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(
        name, path / "__init__.py", submodule_search_locations=[str(path)])
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # 先登记，扩展内部的相对导入才能找到自己
    spec.loader.exec_module(mod)
    return mod


def load(app, render, helpers: dict) -> list[str]:
    """扫描并加载全部扩展，返回成功加载的名字列表。"""
    ctx = Ctx(app, render, helpers)
    for d in _dirs():
        try:
            subs = sorted(p for p in d.iterdir() if (p / "__init__.py").is_file())
        except OSError:
            continue
        for sub in subs:
            modname = "ext_" + "".join(c if c.isalnum() else "_" for c in sub.name)
            try:
                mod = _import_package(sub, modname)
                register = getattr(mod, "register", None)
                if not callable(register):
                    print(f"[ext] {sub.name}: 没有 register(ctx)，跳过", flush=True)
                    continue
                ctx.name = sub.name
                register(ctx)
                _LOADED.append(sub.name)
                print(f"[ext] 已加载：{sub.name} ← {sub}", flush=True)
            except Exception:                      # noqa: BLE001
                print(f"[ext] {sub.name} 加载失败，已跳过（表照常运行）：", flush=True)
                traceback.print_exc()
    if not _LOADED:
        print("[ext] 没有加载任何扩展（表单独运行）", flush=True)
    return list(_LOADED)


def schemas() -> list[tuple[str, str]]:
    """扩展注册的建表 SQL：db.init_db() 会执行它（放在表自己的 SCHEMA 之后）。"""
    return list(_SCHEMAS)


def template_dirs() -> list[Path]:
    return list(_TEMPLATE_DIRS)


def context_for(request, user) -> dict:
    """汇总各扩展的逐页上下文。任一扩展出错都不影响页面渲染。"""
    out: dict = {}
    for fn in _CONTEXT_FNS:
        try:
            extra = fn(request, user) or {}
            if isinstance(extra, dict):
                out.update(extra)
        except Exception:                          # noqa: BLE001
            pass
    return out


def apply_template_dirs(env, base_dirs) -> None:
    """把扩展的模板目录并进 Jinja 搜索路径（表自己的目录优先）。"""
    from jinja2 import ChoiceLoader, FileSystemLoader
    dirs = [str(Path(b)) for b in base_dirs] + [str(d) for d in _TEMPLATE_DIRS]
    env.loader = ChoiceLoader([FileSystemLoader(d) for d in dirs])
