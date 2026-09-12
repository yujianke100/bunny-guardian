# app —— WebUI（FastAPI + SQLite + Jinja2）

个人健康档案的在线查看与录入界面，适配手机与桌面。

## 本地运行

```bash
cd app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
BG_DB=/tmp/bunny.db uvicorn main:app --host 127.0.0.1 --port 9810
# 首次启动会在日志与 $BG_DB 同级目录的 INITIAL_ADMIN.txt 输出初始管理员密码
```

## 冒烟测试（真实 HTTP，无 mock）

⚠️ `BG_KB_DIR` **必须**指到一个一次性的目录（下面例子是 `/tmp/bg-test/kb`）。
它默认等于代码仓库根目录，而测试会往知识库目录里导出文档、`git add -A`、改 origin、
再往自己建的裸仓库推送——指到代码仓库上会把生产/开发用的那份工作副本一起卷进去
（改掉 origin、在代码仓里产生 sync 提交、把个人记录写进代码仓）。跑之前先清干净：

```bash
rm -rf /tmp/bg-test && mkdir -p /tmp/bg-test/kb/docs/经期记录
printf '# 周期记录\n' > /tmp/bg-test/kb/docs/经期记录/周期记录.md   # 需要一篇个人文档
BG_DB=/tmp/bg-test/x.db BG_KB_DIR=/tmp/bg-test/kb \
  BG_SYNC_ON_CHANGE=0 BG_DRY_RUN=1 uvicorn main:app --port 9812 &
BASE=http://127.0.0.1:9812 BG_DB=/tmp/bg-test/x.db BG_KB_DIR=/tmp/bg-test/kb \
  BG_SYNC_ON_CHANGE=0 python smoke_test.py
```

说明：`BG_SYNC_ON_CHANGE=0` 关掉「有改动就自动同步」，本地跑测试时不要真往仓库推。

覆盖：登录/错误密码、未登录跳转、各页面、CSRF 拦截、经期与疾病写入、成员账号权限隔离、
知识库路径穿越防护、导出到知识库、周期环两套视图、智能体权限框架（令牌/隐私围栏/域名白名单/
提案审批/审计）、模型接入配置（密钥 600 与不入库、公网端点需确认、智能体拒绝公网端点）、
图片问诊（压缩/落盘/类型校验）。共 103 项。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BG_DB` | `<repo>/data/bunny.db` | SQLite 路径（部署时由 `deploy/config.sh` 指定到 `$HOME/data`） |
| `BG_DATA` | `<repo>/data` | 附件目录 |
| `BG_LLM_BASE_URL` | 见 `deploy/config.sh` | 模型端点（可在网页「模型接入配置」里改；公网端点需显式确认） |
| `BG_LLM_MODEL` | 见 `deploy/config.sh` | 模型名（可在网页里改，支持从端点拉取候选列表） |
| `BG_LLM_DISABLED` | `0` | 置 1 关闭问答（不调用任何模型） |
| `BG_PUSH_CMD` | 空 | 导出时的推送命令；留空则直接 `git push origin HEAD` |
| `BG_DRY_RUN` | `0` | 置 1 时导出只写文件、不提交 |
| `BG_EXTENSIONS` | 空 | 扩展目录列表（`:` 分隔，Windows 用 `;`）；留空＝只跑表。见下「扩展（里）」 |

## 模块

| 文件 | 职责 |
|---|---|
| `main.py` | 路由：登录、总览、经期、疾病、就医、知识库、问答、账号、管理、导出 |
| `auth.py` | scrypt 密码散列、服务端会话 + CSRF、全局角色 + 按模块（space）三级授权 |
| `db.py` | SQLite 建表与查询；`check_same_thread=False` 适配 FastAPI 线程池 |
| `cycle.py` | 周期统计与阶段推断（中位数、区间、置信度、异常提示） |
| `kb.py` | 扫描 `docs/` 的 Markdown、解析 frontmatter、渲染、关键词检索 |
| `llm.py` | 模型调用（OpenAI 兼容，SSE 流式）；配置来自网页，密钥独立落盘，支持公网端点（需显式确认风险） |
| `outbound.py` | 外发内容策略：身份与工程信息一律抹除，公网端点 fail-closed |
| `extensions.py` | 扩展加载器：把「里」（情侣空间 / AI 跑团）这类附属功能以包的形式挂上来 |
| `media.py` | 图片压缩（Pillow，最长边 1600px / JPEG q82），用于化验单照片问诊 |
| `templates/` | Jinja2 页面：移动端底部导航 + 桌面端侧边栏 |
| `static/` | 自写 CSS/JS（无 CDN、无框架、无构建步骤） |

## 权限模型

- 全局角色：`admin`（全部模块可编辑 + 系统管理）/ `member`。
- 模块（space）：`dashboard` 总览、`cycle` 经期、`conditions` 疾病、`visits` 就医、`knowledge` 医学知识、`qa` 问答。
- 每个成员对每个模块可设为 `none` / `view` / `edit`，在「管理」页调整。
- 所有写操作都要通过 CSRF 校验（会话级 token，双提交）。

## 安全与隐私

- 全部模型调用强制内网地址；个人数据不出内网、不发往任何第三方 API。
- 上传附件仅支持 jpg/png/webp/pdf、≤12MB，文件名重写为随机名；附件不进入 git 仓库。
- 知识库文档路径经过规范化校验，禁止越出 `docs/`。
- 密码使用 scrypt 加盐散列；改密会注销其他设备会话。

## 扩展（里）

本仓库只放**表**（健康档案）。「里」——情侣空间与 AI 跑团——代码在另一个私有仓库
`ex-bunny-guardian`，作为**扩展**叠在这里跑。这样两边的代码不重复。

挂上里（要么放进 `<仓库根>/extensions/`，要么用环境变量指到别处的检出）：

```sh
BG_EXTENSIONS=/root/ex-bunny-guardian/extensions
```

一个扩展就是一个含 `__init__.py` 的包，必须提供 `register(ctx)`：

```python
def register(ctx):
    ctx.add_templates(Path(__file__).parent / "templates")   # 自己的 HTML，可 extends base.html
    ctx.add_static("/ext/play", Path(__file__).parent / "static")   # 自己的 css/js
    ctx.add_schema("play", SCHEMA)          # 建表 SQL，随 db.init_db() 一起跑
    ctx.context(brand)                      # fn(request, user) -> dict，逐页合并进模板上下文
    ctx.app.get("/play")(play_page)         # 直接挂路由；ctx.render/csrf_ok/client_ip/guard/audit/ping 都能用
```

约定：扩展**不要 `import main`**（会循环导入），需要什么从 `ctx` 取，或 import
`db` / `llm` / `auth` 这些纯模块。扩展加载失败只打印一行并跳过——表必须能单跑。

模板搜索路径里表排在扩展前面，所以扩展改不动表的同名模板（防止它悄悄顶掉表层）。
