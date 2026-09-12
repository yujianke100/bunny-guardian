---
title: CLAUDE
tags: [claude, agent-guidance]
---

# CLAUDE.md — 系统仓库（兔兔守护者 / Bunny Guardian）开发指引

## 本仓库是什么

**软件**。个人健康数据在**另一个私有仓库**里，通过 `BG_KB_DIR` 挂载。

## 铁律

1. **绝不把个人数据放进本仓库**：姓名、昵称、真实域名、IP、账号、令牌、
   具体的就诊/经期记录，都不许提交。需要示例时用 `demo` / `测试姓名甲` 这类中性词。
2. **站点专属取值走配置**：`deploy/config.sh`（可被不提交的 `deploy/config.local.sh` 覆盖）
   与运行期环境变量（`BG_APP_NAME`、`BG_KB_DIR`、模型端点等）。
   不要在代码里写死任何个人或环境相关的值。
3. **敏感词表不进代码**：`app/outbound.py` 只保留**通用规则**（路径、令牌、IP、邮箱、日期）；
   站点专属词条由 `<数据目录>/scrub_terms.txt` 提供（示例见 `deploy/scrub-terms.example.txt`）。
4. **助手不得获得通用能力**：新增工具前先读 `docs/agent-permissions.md`。
   任何写入都必须是「提案 + 人工批准」，不要开直写通道。

## 提交前自检

```bash
sh scripts/check-desensitization.sh --strict   # 有个人/环境信息即失败
cd app && . .venv/bin/activate && python smoke_test.py   # 需要先起服务，见 app/README.md
```

## 结构

| 路径 | 内容 |
|---|---|
| `app/` | FastAPI 应用（见 `app/README.md` 的模块表） |
| `deploy/config.sh` | 唯一配置源（路径/端口/名称/模型） |
| `deploy/install.sh` | 免 root 安装：目录、依赖、用户单元、健康检查 |
| `deploy/systemd-user/` | 用户单元模板（`@@占位符@@` 由 install.sh 渲染） |
| `deploy/pi/` | 助手：工具白名单扩展、沙箱启动、净化后的工作准则 |
| `deploy/sync.sh` | 只同步**数据仓库**（快进或推送） |
| `templates/kb/` | 起步知识库骨架（安装时若无数据仓库就复制它） |
| `docs/agent-permissions.md` | 助手权限框架、威胁模型、残余风险 |
| `docs/open-source-checklist.md` | 开源前脱敏清单 |
| `scripts/check-desensitization.sh` | 脱敏自检（`--strict` 可用于 CI） |

## 用户级部署要点（踩过的坑）

- systemd **用户单元**下这三个属性会导致 `status=218/CAPABILITIES`，模板里刻意没用：
  `CapabilityBoundingSet=`、`PrivateDevices=yes`、`ProtectKernelModules=yes`。
- 改过账号家目录后，用户管理器会缓存旧 `$HOME` → 单元「does not exist」。
  修法：`loginctl terminate-user <user>` 再 `enable-linger`。
- 目录搬迁后 venv 的 `bin/pip` shebang 会失效（`bin/python` 仍是有效符号链接），
  `install.sh` 用 `pip --version` 实测并自动重建。
- 更多排错见 `deploy/README.md` 末尾。
