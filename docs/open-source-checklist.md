---
title: 开源前脱敏检查清单
tags:
  - meta
  - 开源
aliases:
  - open-source-checklist
  - 脱敏
last_updated: 2026-09-11
---

# 开源前脱敏检查清单

本仓库当前是**某个具体人的私有健康档案**（含真实记录与医学知识），
开源时应当只发布「程序 + 部署骨架」，**不发布任何个人数据**。

## 一、必须先剥离的内容

| 内容 | 位置 | 处理 |
|---|---|---|
| 个人健康记录 | `docs/经期记录/`、`docs/疾病档案/`、`docs/就医记录/` 下的数据文件 | **删除**（只保留 `README.md` 说明） |
| 姓名/昵称 | 页面标题、`APP_NAME`、医学知识文档中的举例 | 改为通用名称（如「健康档案」） |
| 部署环境 | 域名、公网 IP、内网 IP、绝对路径、站点专属模型路径 | 全部改为占位符或走 `deploy/config.sh` |
| 令牌与密钥 | `bg_*`、`sk-*`、`.env`、`data/llm_api_key` | 确认从未提交；若曾提交，需重写历史 |
| 其他项目名 | 集群名、内部项目名、工具名 | 删除（它们在提示词里也属于「工程信息」） |
| 截图 | 含真实数据的界面截图 | 换成空库或演示数据 |

自检命令（已纳入本仓库）：

```sh
sh scripts/check-desensitization.sh            # 报告
sh scripts/check-desensitization.sh --strict   # 有命中即失败，适合放进 CI
```

白名单：`deploy/desensitize-allow.txt`（每行一个路径前缀，用于确实需要保留的示例文件）。

## 二、发布前应补齐的内容

1. **LICENSE**：目前未添加。建议 MIT 或 Apache-2.0（若希望保留专利授权条款选后者）。
2. **README**：面向陌生读者重写——它是什么、解决什么问题、三分钟跑起来、架构图、隐私说明。
   现有 `README.md` 是给「档案主人」看的，开源版需要另一个入口（例如 `README.opensource.md`）。
3. **示例配置**：`deploy/config.local.example.sh`（本仓库只提交 `config.sh` 默认值 + 示例）。
4. **医学知识文档的版权**：`docs/医学知识/*` 是逐条标注来源的整理稿，转载他人指南内容需注意
   引用范围与许可（多数指南允许引用摘要，不允许整篇转载）。开源时可考虑仅保留写作模板。
5. **隐私边界说明**：写清「模型端点可配置，但提问摘要会外发」以及本项目做了哪些抹除
   （见 `docs/agent-permissions.md` 第八节）。

## 三、结构上已经为开源做的准备

- **部署不需要 root**：全部落在 `$HOME` 下，systemd 用**用户单元**（`deploy/systemd-user/`）。
- **单一配置源**：`deploy/config.sh`（可用不提交的 `config.local.sh` 覆盖），
  路径/端口/名称/模型都不写死在脚本里。
- **无系统路径依赖**：不引用 `/opt`、`/etc`、`/var/lib`、`/usr/local`。
- **Node 与 pi 也在用户目录**：`deploy/setup-node.sh` 装到 `~/.local/opt/` + `~/.local/bin/pi`。
- **反向代理可选**：`deploy/nginx-vhost.conf.example`，属于使用者的外部基础设施。
- **智能体提示词净化**：`deploy/pi/agent-context/instructions.md` 不含任何环境信息，
  由自检脚本静态校验。

## 四、迁移到用户层后的目录结构

```
$HOME/
├── <slug>/                     代码（git 工作副本）
├── data/                       SQLite、上传附件、API Key（不进 git）
├── agent/                      智能体工作目录（只有净化后的 CLAUDE.md）
├── .config/<slug>/agent.env    智能体令牌（600）
├── .config/systemd/user/*      用户级服务单元
├── .local/opt/node-v22/        用户级 Node
├── .local/bin/pi               包装脚本
└── .local/state/<slug>/        日志与智能体状态
```
